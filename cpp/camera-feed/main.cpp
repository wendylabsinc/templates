#include <drogon/drogon.h>
#include <drogon/WebSocketController.h>
#include <gst/gst.h>
#include <gst/app/gstappsink.h>

#include <atomic>
#include <cstdlib>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

// ---------------------------------------------------------------------------
// Shared frame buffer
// ---------------------------------------------------------------------------
static std::mutex g_frameMutex;
static std::vector<uint8_t> g_latestFrame;

// GStreamer pipeline handle
static GstElement *g_pipeline = nullptr;
static std::atomic<bool> g_running{true};

// ---------------------------------------------------------------------------
// GStreamer appsink callback – called on every new JPEG frame
// ---------------------------------------------------------------------------
static GstFlowReturn onNewSample(GstAppSink *sink, gpointer /*userData*/)
{
    GstSample *sample = gst_app_sink_pull_sample(sink);
    if (!sample)
        return GST_FLOW_ERROR;

    GstBuffer *buffer = gst_sample_get_buffer(sample);
    if (buffer) {
        GstMapInfo map;
        if (gst_buffer_map(buffer, &map, GST_MAP_READ)) {
            std::lock_guard<std::mutex> lock(g_frameMutex);
            g_latestFrame.assign(map.data, map.data + map.size);
            gst_buffer_unmap(buffer, &map);
        }
    }
    gst_sample_unref(sample);
    return GST_FLOW_OK;
}

// ---------------------------------------------------------------------------
// Start GStreamer capture pipeline in its own thread
// ---------------------------------------------------------------------------
static void gstreamerThread()
{
    gst_init(nullptr, nullptr);

    const char *pipelineDesc =
        "v4l2src device=/dev/video0 ! image/jpeg ! "
        "appsink name=sink emit-signals=true max-buffers=2 drop=true";

    GError *err = nullptr;
    g_pipeline = gst_parse_launch(pipelineDesc, &err);
    if (err) {
        LOG_ERROR << "GStreamer pipeline error: " << err->message;
        g_error_free(err);
        return;
    }

    GstElement *sink = gst_bin_get_by_name(GST_BIN(g_pipeline), "sink");
    if (!sink) {
        LOG_ERROR << "Could not find appsink element";
        return;
    }

    GstAppSinkCallbacks callbacks{};
    callbacks.new_sample = onNewSample;
    gst_app_sink_set_callbacks(GST_APP_SINK(sink), &callbacks, nullptr, nullptr);
    gst_object_unref(sink);

    gst_element_set_state(g_pipeline, GST_STATE_PLAYING);
    LOG_INFO << "GStreamer pipeline started";

    // Run the GLib main loop so signals are dispatched
    GMainLoop *loop = g_main_loop_new(nullptr, FALSE);
    while (g_running.load()) {
        // Pump the default context briefly so callbacks fire
        g_main_context_iteration(nullptr, FALSE);
        std::this_thread::sleep_for(std::chrono::milliseconds(5));
    }
    g_main_loop_unref(loop);

    gst_element_set_state(g_pipeline, GST_STATE_NULL);
    gst_object_unref(g_pipeline);
    g_pipeline = nullptr;
}

// ---------------------------------------------------------------------------
// WebSocket controller – streams MJPEG frames to connected clients
// ---------------------------------------------------------------------------
class StreamWs : public drogon::WebSocketController<StreamWs>
{
  public:
    void handleNewMessage(const drogon::WebSocketConnectionPtr &conn,
                          std::string &&message,
                          const drogon::WebSocketMessageType &type) override
    {
        // No inbound messages expected
    }

    void handleNewConnection(const drogon::HttpRequestPtr &req,
                             const drogon::WebSocketConnectionPtr &conn) override
    {
        LOG_INFO << "WebSocket client connected";
        // Launch a coroutine-style loop that pushes frames
        std::thread([conn]() {
            while (conn->connected()) {
                std::vector<uint8_t> frame;
                {
                    std::lock_guard<std::mutex> lock(g_frameMutex);
                    frame = g_latestFrame;
                }
                if (!frame.empty()) {
                    conn->send(reinterpret_cast<const char *>(frame.data()),
                               frame.size(),
                               drogon::WebSocketMessageType::Binary);
                }
                std::this_thread::sleep_for(std::chrono::milliseconds(33)); // ~30 fps
            }
        }).detach();
    }

    void handleConnectionClosed(const drogon::WebSocketConnectionPtr &conn) override
    {
        LOG_INFO << "WebSocket client disconnected";
    }

    WS_PATH_LIST_BEGIN
    WS_PATH_ADD("/stream");
    WS_PATH_LIST_END
};

// ---------------------------------------------------------------------------
// main
// ---------------------------------------------------------------------------
int main()
{
    // Start GStreamer in a background thread
    std::thread gstThread(gstreamerThread);
    gstThread.detach();

    // GET /cameras – return JSON from v4l2-ctl
    drogon::app().registerHandler(
        "/cameras",
        [](const drogon::HttpRequestPtr &req,
           std::function<void(const drogon::HttpResponsePtr &)> &&callback) {
            FILE *pipe = popen("v4l2-ctl --list-devices 2>&1", "r");
            std::string output;
            if (pipe) {
                char buf[256];
                while (fgets(buf, sizeof(buf), pipe))
                    output += buf;
                pclose(pipe);
            }
            Json::Value json;
            json["raw"] = output;
            auto resp = drogon::HttpResponse::newHttpJsonResponse(json);
            callback(resp);
        },
        {drogon::Get});

    // GET / – serve index.html via document root
    drogon::app().setDocumentRoot("./static");

    drogon::app().addListener("0.0.0.0", {{.PORT}});
    LOG_INFO << "Starting server on 0.0.0.0:{{.PORT}}";
    drogon::app().run();

    g_running.store(false);
    return 0;
}
