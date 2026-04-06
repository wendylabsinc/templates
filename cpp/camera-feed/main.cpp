#include <drogon/drogon.h>
#include <drogon/WebSocketController.h>
#include <gst/gst.h>
#include <gst/app/gstappsink.h>

#include <atomic>
#include <cstdlib>
#include <mutex>
#include <set>
#include <string>
#include <thread>
#include <vector>

// ---------------------------------------------------------------------------
// MJPEGCamera – singleton managing a GStreamer v4l2src -> MJPEG -> appsink
// ---------------------------------------------------------------------------
class MJPEGCamera
{
  public:
    static MJPEGCamera &instance()
    {
        static MJPEGCamera cam;
        return cam;
    }

    // Returns the most recent JPEG frame (may be empty if none captured yet).
    std::vector<uint8_t> latestFrame() const
    {
        std::lock_guard<std::mutex> lock(frameMutex_);
        return latestFrame_;
    }

    // Called when a WebSocket client connects. Starts the pipeline on the
    // first client.
    void addClient(const drogon::WebSocketConnectionPtr &conn)
    {
        std::lock_guard<std::mutex> lock(clientsMutex_);
        clients_.insert(conn);
        if (clients_.size() == 1)
        {
            startPipeline();
        }
    }

    // Called when a WebSocket client disconnects. Stops the pipeline when
    // the last client leaves.
    void removeClient(const drogon::WebSocketConnectionPtr &conn)
    {
        std::lock_guard<std::mutex> lock(clientsMutex_);
        clients_.erase(conn);
        if (clients_.empty())
        {
            stopPipeline();
        }
    }

    // Restart the pipeline with a different video device (e.g. /dev/video2).
    void switchDevice(const std::string &device)
    {
        std::lock_guard<std::mutex> lock(clientsMutex_);
        device_ = device;
        if (!clients_.empty())
        {
            stopPipeline();
            startPipeline();
        }
    }

    // Broadcast the latest frame to every connected WebSocket client.
    void broadcastFrame()
    {
        auto frame = latestFrame();
        if (frame.empty())
            return;

        std::lock_guard<std::mutex> lock(clientsMutex_);
        for (auto &conn : clients_)
        {
            if (conn->connected())
            {
                conn->send(reinterpret_cast<const char *>(frame.data()),
                           frame.size(),
                           drogon::WebSocketMessageType::Binary);
            }
        }
    }

  private:
    MJPEGCamera() = default;
    ~MJPEGCamera()
    {
        stopPipeline();
    }

    MJPEGCamera(const MJPEGCamera &) = delete;
    MJPEGCamera &operator=(const MJPEGCamera &) = delete;

    // -- GStreamer callback (static so it can be used with g_signal_connect) --
    static GstFlowReturn onNewSample(GstElement *sink, gpointer userData)
    {
        auto *self = static_cast<MJPEGCamera *>(userData);

        GstSample *sample = nullptr;
        g_signal_emit_by_name(sink, "pull-sample", &sample);
        if (!sample)
            return GST_FLOW_ERROR;

        GstBuffer *buffer = gst_sample_get_buffer(sample);
        if (buffer)
        {
            GstMapInfo map;
            if (gst_buffer_map(buffer, &map, GST_MAP_READ))
            {
                std::lock_guard<std::mutex> lock(self->frameMutex_);
                self->latestFrame_.assign(map.data, map.data + map.size);
                gst_buffer_unmap(buffer, &map);
            }
        }
        gst_sample_unref(sample);
        return GST_FLOW_OK;
    }

    void startPipeline()
    {
        if (pipeline_)
            return;

        std::string desc =
            "v4l2src device=" + device_ +
            " ! image/jpeg"
            " ! jpegdec"
            " ! jpegenc quality=85"
            " ! appsink name=sink emit-signals=true max-buffers=2 drop=true sync=false";

        GError *err = nullptr;
        pipeline_ = gst_parse_launch(desc.c_str(), &err);
        if (err)
        {
            LOG_ERROR << "GStreamer pipeline error: " << err->message;
            g_error_free(err);
            pipeline_ = nullptr;
            return;
        }

        GstElement *sink = gst_bin_get_by_name(GST_BIN(pipeline_), "sink");
        if (!sink)
        {
            LOG_ERROR << "Could not find appsink element";
            gst_object_unref(pipeline_);
            pipeline_ = nullptr;
            return;
        }

        g_signal_connect(sink, "new-sample",
                         G_CALLBACK(&MJPEGCamera::onNewSample), this);
        gst_object_unref(sink);

        gst_element_set_state(pipeline_, GST_STATE_PLAYING);
        LOG_INFO << "GStreamer pipeline started on " << device_;
    }

    void stopPipeline()
    {
        if (!pipeline_)
            return;

        gst_element_set_state(pipeline_, GST_STATE_NULL);
        gst_object_unref(pipeline_);
        pipeline_ = nullptr;
        LOG_INFO << "GStreamer pipeline stopped";
    }

    // Latest captured JPEG frame
    mutable std::mutex frameMutex_;
    std::vector<uint8_t> latestFrame_;

    // Connected WebSocket clients
    std::mutex clientsMutex_;
    std::set<drogon::WebSocketConnectionPtr> clients_;

    // Pipeline state
    GstElement *pipeline_ = nullptr;
    std::string device_ = "/dev/video0";
};

// ---------------------------------------------------------------------------
// WebSocket controller at /stream
// ---------------------------------------------------------------------------
class StreamWs : public drogon::WebSocketController<StreamWs>
{
  public:
    void handleNewConnection(const drogon::HttpRequestPtr &req,
                             const drogon::WebSocketConnectionPtr &conn) override
    {
        LOG_INFO << "WebSocket client connected";
        MJPEGCamera::instance().addClient(conn);
    }

    void handleNewMessage(const drogon::WebSocketConnectionPtr &conn,
                          std::string &&message,
                          const drogon::WebSocketMessageType &type) override
    {
        if (type != drogon::WebSocketMessageType::Text)
            return;

        // Expect JSON: {"switch_camera": "/dev/video2"}
        Json::Value json;
        Json::CharReaderBuilder builder;
        std::string errs;
        std::istringstream stream(message);
        if (Json::parseFromStream(builder, stream, &json, &errs))
        {
            if (json.isMember("switch_camera"))
            {
                std::string dev = json["switch_camera"].asString();
                LOG_INFO << "Switching camera to " << dev;
                MJPEGCamera::instance().switchDevice(dev);
            }
        }
    }

    void handleConnectionClosed(const drogon::WebSocketConnectionPtr &conn) override
    {
        LOG_INFO << "WebSocket client disconnected";
        MJPEGCamera::instance().removeClient(conn);
    }

    WS_PATH_LIST_BEGIN
    WS_PATH_ADD("/stream");
    WS_PATH_LIST_END
};

// ---------------------------------------------------------------------------
// GLib main loop thread – pumps the default context so GStreamer signals fire
// ---------------------------------------------------------------------------
static std::atomic<bool> g_running{true};

static void glibMainLoopThread()
{
    GMainLoop *loop = g_main_loop_new(nullptr, FALSE);
    while (g_running.load())
    {
        g_main_context_iteration(nullptr, FALSE);
        std::this_thread::sleep_for(std::chrono::milliseconds(2));
    }
    g_main_loop_unref(loop);
}

// ---------------------------------------------------------------------------
// main
// ---------------------------------------------------------------------------
int main()
{
    // Initialise GStreamer
    gst_init(nullptr, nullptr);

    // Start GLib main loop in a background thread
    std::thread glibThread(glibMainLoopThread);
    glibThread.detach();

    const char *hostname = std::getenv("WENDY_HOSTNAME");
    if (hostname)
    {
        LOG_INFO << "WENDY_HOSTNAME: " << hostname;
    }

    // GET /cameras – list V4L2 devices as JSON
    drogon::app().registerHandler(
        "/cameras",
        [](const drogon::HttpRequestPtr &req,
           std::function<void(const drogon::HttpResponsePtr &)> &&callback) {
            FILE *pipe = popen("v4l2-ctl --list-devices 2>&1", "r");
            std::string output;
            if (pipe)
            {
                char buf[256];
                while (fgets(buf, sizeof(buf), pipe))
                    output += buf;
                pclose(pipe);
            }

            // Parse v4l2-ctl output into a JSON array of {name, id} objects.
            // Each device block looks like:
            //   Some Camera Name (usb-...):
            //       /dev/video0
            //       /dev/video1
            Json::Value arr(Json::arrayValue);
            std::istringstream lines(output);
            std::string line;
            std::string currentName;
            while (std::getline(lines, line))
            {
                if (line.empty())
                    continue;
                if (line[0] != '\t' && line[0] != ' ')
                {
                    // Device header line – strip trailing colon / bus info
                    auto paren = line.find('(');
                    currentName = (paren != std::string::npos)
                                      ? line.substr(0, paren)
                                      : line;
                    // Trim trailing whitespace and colon
                    while (!currentName.empty() &&
                           (currentName.back() == ' ' || currentName.back() == ':'))
                        currentName.pop_back();
                }
                else
                {
                    // Device path line
                    std::string dev = line;
                    // Trim leading whitespace
                    auto start = dev.find_first_not_of(" \t");
                    if (start != std::string::npos)
                        dev = dev.substr(start);
                    if (dev.rfind("/dev/video", 0) == 0)
                    {
                        Json::Value entry;
                        entry["id"] = dev;
                        entry["name"] = currentName.empty() ? dev : currentName;
                        arr.append(entry);
                    }
                }
            }

            auto resp = drogon::HttpResponse::newHttpJsonResponse(arr);
            callback(resp);
        },
        {drogon::Get});

    // Serve index.html at / and static assets from ./assets
    drogon::app().setDocumentRoot(".");

    // Timer: broadcast latest frame to all WebSocket clients at ~30 fps
    drogon::app().getLoop()->runEvery(std::chrono::milliseconds(33), []() {
        MJPEGCamera::instance().broadcastFrame();
    });

    LOG_INFO << "Starting server on 0.0.0.0:{{.PORT}}";

    drogon::app()
        .addListener("0.0.0.0", {{.PORT}})
        .run();

    g_running.store(false);
    return 0;
}
