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

#include <algorithm>
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

struct Stream {
    std::mutex mtx;
    std::vector<unsigned char> jpeg;       // latest encoded frame
    std::atomic<unsigned long> frames{0};
    std::atomic<int> w{0}, h{0};
};
static Stream g_color;
static Stream g_depth;
static tjhandle g_tj = nullptr;            // guarded by g_tj_mtx (single encoder)
static std::mutex g_tj_mtx;
static int g_quality = 80;
static int g_depth_min = 200;              // mm -> colormap range
static int g_depth_max = 4000;

static void encode_bgr(Stream &s, const unsigned char *bgr, int w, int h)
{
    unsigned char *out = nullptr;
    unsigned long sz = 0;
    int rc;
    {
        std::lock_guard<std::mutex> lk(g_tj_mtx);
        rc = tjCompress2(g_tj, bgr, w, 0, h, TJPF_BGR, &out, &sz,
                         TJSAMP_420, g_quality, TJFLAG_FASTDCT);
    }
    if (rc == 0) {
        std::lock_guard<std::mutex> lk(s.mtx);
        s.jpeg.assign(out, out + sz);
        s.w = w; s.h = h;
        s.frames++;
    }
    if (out) tjFree(out);
}

// Colorize a 16-bit depth frame (millimetres) into a BGR jet image, then encode.
static void encode_depth(const unsigned short *depth, int w, int h)
{
    std::vector<unsigned char> bgr((size_t)w * h * 3);
    const float lo = (float)g_depth_min, hi = (float)g_depth_max;
    for (int i = 0; i < w * h; i++) {
        unsigned short d = depth[i];
        unsigned char *p = &bgr[(size_t)i * 3];
        if (d == 0) { p[0] = p[1] = p[2] = 0; continue; }  // invalid -> black
        float t = (d - lo) / (hi - lo);
        if (t < 0) t = 0; if (t > 1) t = 1;
        // jet colormap
        auto cl = [](float v) { return (unsigned char)(255.f * (v < 0 ? 0 : v > 1 ? 1 : v)); };
        float r = std::min(4 * t - 1.5f, -4 * t + 4.5f);
        float g = std::min(4 * t - 0.5f, -4 * t + 3.5f);
        float b = std::min(4 * t + 0.5f, -4 * t + 2.5f);
        p[0] = cl(b); p[1] = cl(g); p[2] = cl(r);  // BGR
    }
    encode_bgr(g_depth, bgr.data(), w, h);
}

// SDK stream callback — fires for every frame set.
static void on_frame(AS_CAM_PTR /*cam*/, const AS_SDK_Data_s *d, void * /*priv*/)
{
    if (!d) return;
    if (d->rgbImg.size > 0 && d->rgbImg.data) {
        // SDK delivers RGB as BGR888 (size == width*height*3).
        encode_bgr(g_color, (const unsigned char *)d->rgbImg.data, d->rgbImg.width, d->rgbImg.height);
    } else if (d->mjpegImg.size > 0 && d->mjpegImg.data) {
        // Some models deliver JPEG directly — pass through.
        std::lock_guard<std::mutex> lk(g_color.mtx);
        g_color.jpeg.assign((unsigned char *)d->mjpegImg.data,
                            (unsigned char *)d->mjpegImg.data + d->mjpegImg.size);
        g_color.frames++;
    }
    // Depth: 16-bit millimetres (size == w*h*2).
    if (d->depthImg.size > 0 && d->depthImg.data &&
        d->depthImg.size == d->depthImg.width * d->depthImg.height * 2) {
        encode_depth((const unsigned short *)d->depthImg.data, d->depthImg.width, d->depthImg.height);
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
        char body[320];
        int n = snprintf(body, sizeof(body),
                         "{\"ok\":true,\"backend\":\"angstrong-sdk\","
                         "\"color\":{\"frames\":%lu,\"width\":%d,\"height\":%d},"
                         "\"depth\":{\"frames\":%lu,\"width\":%d,\"height\":%d}}",
                         g_color.frames.load(), g_color.w.load(), g_color.h.load(),
                         g_depth.frames.load(), g_depth.w.load(), g_depth.h.load());
        char hdr[256];
        int hn = snprintf(hdr, sizeof(hdr),
                          "HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n"
                          "Access-Control-Allow-Origin: *\r\nContent-Length: %d\r\n\r\n", n);
        send_all(fd, hdr, hn);
        send_all(fd, body, n);
        close(fd);
        return;
    }

    // Select stream by path: /stream/depth -> depth, everything else -> color.
    Stream &stream = (path.find("depth") != std::string::npos) ? g_depth : g_color;

    // Default: multipart MJPEG stream.
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
            std::lock_guard<std::mutex> lk(stream.mtx);
            frame = stream.jpeg;
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
    if (getenv("DEPTH_MIN_MM")) g_depth_min = atoi(getenv("DEPTH_MIN_MM"));
    if (getenv("DEPTH_MAX_MM")) g_depth_max = atoi(getenv("DEPTH_MAX_MM"));

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

    if (AS_SDK_StartStream(cam, RGB_IMG_FLG | DEPTH_IMG_FLG) < 0) {
        fprintf(stderr, "[camera] StartStream(RGB|DEPTH) failed\n");
        return 1;
    }
    fprintf(stderr, "[camera] RGB+depth stream started\n");

    for (;;) pause();  // SDK delivers frames on its own threads
    return 0;
}
