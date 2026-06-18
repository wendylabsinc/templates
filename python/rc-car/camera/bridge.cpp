// rc-car camera bridge: Angstrong (Nuwa-HP60C) SDK -> MJPEG over HTTP.
//
// The HP60C is a structured-light depth camera that cannot be read as a plain
// UVC/V4L2 device — it must be driven by the Angstrong SDK (libAngstrongCameraSdk),
// which initializes the sensor and delivers an already-converted BGR888 RGB
// frame via a stream callback. This program opens the camera with its config
// file, starts the RGB stream, JPEG-encodes each frame, and serves it as a
// multipart MJPEG stream at /stream/color — the same shape the rc teleop UI
// consumes via a plain <img>.
//
// Adapted from the SDK usage in ascamera_node (YahboomTechnology / Angstrong),
// with ROS2 stripped out.
#include "as_camera_sdk_api.h"
#include "as_camera_sdk_def.h"
#include <turbojpeg.h>

#include <atomic>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <list>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include <arpa/inet.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <signal.h>
#include <sys/socket.h>
#include <unistd.h>

static std::mutex g_mtx;
static std::vector<unsigned char> g_jpeg;     // latest encoded frame
static std::atomic<unsigned long> g_frames{0};
static std::atomic<int> g_w{0}, g_h{0};
static tjhandle g_tj = nullptr;
static int g_quality = 80;

static void encode_bgr(const unsigned char *bgr, int w, int h)
{
    unsigned char *out = nullptr;
    unsigned long sz = 0;
    if (tjCompress2(g_tj, bgr, w, 0, h, TJPF_BGR, &out, &sz,
                    TJSAMP_420, g_quality, TJFLAG_FASTDCT) == 0) {
        std::lock_guard<std::mutex> lk(g_mtx);
        g_jpeg.assign(out, out + sz);
        g_w = w; g_h = h;
        g_frames++;
    }
    if (out) tjFree(out);
}

// SDK stream callback — fires for every frame set.
static void on_frame(AS_CAM_PTR /*cam*/, const AS_SDK_Data_s *d, void * /*priv*/)
{
    if (!d) return;
    if (d->rgbImg.size > 0 && d->rgbImg.data) {
        // SDK delivers RGB as BGR888 (size == width*height*3).
        encode_bgr((const unsigned char *)d->rgbImg.data, d->rgbImg.width, d->rgbImg.height);
    } else if (d->mjpegImg.size > 0 && d->mjpegImg.data) {
        // Some models deliver JPEG directly — pass through.
        std::lock_guard<std::mutex> lk(g_mtx);
        g_jpeg.assign((unsigned char *)d->mjpegImg.data,
                      (unsigned char *)d->mjpegImg.data + d->mjpegImg.size);
        g_frames++;
    }
}

static bool send_all(int fd, const char *buf, size_t n)
{
    while (n) {
        ssize_t k = send(fd, buf, n, MSG_NOSIGNAL);
        if (k <= 0) return false;
        buf += k; n -= (size_t)k;
    }
    return true;
}

static void handle_client(int fd)
{
    int one = 1;
    setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));

    char req[1024] = {0};
    recv(fd, req, sizeof(req) - 1, 0);
    std::string r(req);
    std::string path = "/";
    {
        size_t s = r.find(' ');
        if (s != std::string::npos) {
            size_t e = r.find(' ', s + 1);
            if (e != std::string::npos) path = r.substr(s + 1, e - s - 1);
        }
    }

    if (path.rfind("/health", 0) == 0 || path.rfind("/info", 0) == 0) {
        char body[256];
        int n = snprintf(body, sizeof(body),
                         "{\"ok\":true,\"backend\":\"angstrong-sdk\",\"frames\":%lu,\"width\":%d,\"height\":%d}",
                         g_frames.load(), g_w.load(), g_h.load());
        char hdr[256];
        int hn = snprintf(hdr, sizeof(hdr),
                          "HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n"
                          "Access-Control-Allow-Origin: *\r\nContent-Length: %d\r\n\r\n", n);
        send_all(fd, hdr, hn);
        send_all(fd, body, n);
        close(fd);
        return;
    }

    // Default: multipart MJPEG stream (/stream/color and anything else).
    const char *hdr =
        "HTTP/1.0 200 OK\r\n"
        "Access-Control-Allow-Origin: *\r\n"
        "Cache-Control: no-cache\r\n"
        "Connection: close\r\n"
        "Content-Type: multipart/x-mixed-replace; boundary=frame\r\n\r\n";
    if (!send_all(fd, hdr, strlen(hdr))) { close(fd); return; }

    for (;;) {
        std::vector<unsigned char> frame;
        {
            std::lock_guard<std::mutex> lk(g_mtx);
            frame = g_jpeg;
        }
        if (!frame.empty()) {
            char part[128];
            int pn = snprintf(part, sizeof(part),
                              "--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %zu\r\n\r\n",
                              frame.size());
            if (!send_all(fd, part, pn)) break;
            if (!send_all(fd, (const char *)frame.data(), frame.size())) break;
            if (!send_all(fd, "\r\n", 2)) break;
        }
        usleep(33000);  // ~30 fps cap
    }
    close(fd);
}

static void run_http(int port)
{
    int srv = socket(AF_INET, SOCK_STREAM, 0);
    int one = 1;
    setsockopt(srv, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = INADDR_ANY;
    addr.sin_port = htons(port);
    if (bind(srv, (sockaddr *)&addr, sizeof(addr)) < 0) { perror("bind"); exit(1); }
    listen(srv, 8);
    fprintf(stderr, "[camera] http serving on :%d\n", port);
    for (;;) {
        int fd = accept(srv, nullptr, nullptr);
        if (fd < 0) continue;
        std::thread(handle_client, fd).detach();
    }
}

int main()
{
    signal(SIGPIPE, SIG_IGN);
    const char *cfg = getenv("ASCAM_CONFIG");
    if (!cfg) cfg = "/opt/ascam/config/hp60c_v2_00_20230704_configEncrypt.json";
    int port = getenv("PORT") ? atoi(getenv("PORT")) : 8000;
    g_quality = getenv("JPEG_QUALITY") ? atoi(getenv("JPEG_QUALITY")) : 80;

    g_tj = tjInitCompress();
    if (!g_tj) { fprintf(stderr, "tjInitCompress failed\n"); return 1; }

    // Serve HTTP immediately (reports frames:0 until the camera comes up).
    std::thread(run_http, port).detach();

    if (AS_SDK_Init() != 0) { fprintf(stderr, "[camera] AS_SDK_Init failed\n"); return 1; }

    AS_CAM_PTR cam = nullptr;
    for (int attempt = 0;; attempt++) {
        std::list<AS_CAM_PTR> devs;
        AS_SDK_GetCameraList(devs);
        if (!devs.empty()) { cam = devs.front(); break; }
        if (attempt % 10 == 0) fprintf(stderr, "[camera] no camera yet, waiting...\n");
        sleep(1);
    }

    AS_SDK_CAM_MODEL_E model;
    if (AS_SDK_GetCameraModel(cam, model) == 0)
        fprintf(stderr, "[camera] model=%d\n", (int)model);

    if (AS_SDK_OpenCamera(cam, cfg) < 0) {
        fprintf(stderr, "[camera] OpenCamera failed (cfg=%s)\n", cfg);
        return 1;
    }
    AS_CAM_Stream_Cb_s cb;
    cb.callback = on_frame;
    cb.privateData = nullptr;
    if (AS_SDK_RegisterStreamCallback(cam, &cb) != 0)
        fprintf(stderr, "[camera] RegisterStreamCallback failed\n");

    if (AS_SDK_StartStream(cam, RGB_IMG_FLG) < 0) {
        fprintf(stderr, "[camera] StartStream(RGB) failed\n");
        return 1;
    }
    fprintf(stderr, "[camera] RGB stream started\n");

    for (;;) pause();  // SDK delivers frames on its own threads
    return 0;
}
