#include <drogon/drogon.h>
#include <drogon/WebSocketController.h>
#include <gst/gst.h>
#include <gst/app/gstappsink.h>

#include <atomic>
#include <cstdlib>
#include <cstring>
#include <dirent.h>
#include <iostream>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

using namespace drogon;

// ---------------------------------------------------------------------------
// AudioCapture singleton — GStreamer pipeline with appsink
// ---------------------------------------------------------------------------
class AudioCapture {
public:
    static AudioCapture& instance() {
        static AudioCapture ac;
        return ac;
    }

    void start() {
        if (running_.exchange(true)) return;

        pipeline_ = gst_parse_launch(
            "autoaudiosrc ! audioconvert ! "
            "audio/x-raw,format=S16LE,channels=1,rate=16000 ! "
            "appsink name=sink emit-signals=true max-buffers=4 drop=true sync=false",
            nullptr);

        if (!pipeline_) {
            std::cerr << "Failed to create GStreamer pipeline" << std::endl;
            running_ = false;
            return;
        }

        GstElement* sink = gst_bin_get_by_name(GST_BIN(pipeline_), "sink");
        g_signal_connect(sink, "new-sample",
                         G_CALLBACK(+[](GstAppSink* appsink, gpointer) -> GstFlowReturn {
                             GstSample* sample = gst_app_sink_pull_sample(appsink);
                             if (!sample) return GST_FLOW_OK;

                             GstBuffer* buf = gst_sample_get_buffer(sample);
                             GstMapInfo map;
                             if (gst_buffer_map(buf, &map, GST_MAP_READ)) {
                                 auto& ac = AudioCapture::instance();
                                 std::lock_guard<std::mutex> lock(ac.mutex_);
                                 ac.buffer_.assign(map.data, map.data + map.size);
                                 gst_buffer_unmap(buf, &map);
                             }

                             gst_sample_unref(sample);
                             return GST_FLOW_OK;
                         }),
                         nullptr);
        gst_object_unref(sink);

        gst_element_set_state(pipeline_, GST_STATE_PLAYING);

        // Run a GLib main loop so GStreamer bus messages are dispatched
        loop_thread_ = std::thread([this]() {
            loop_ = g_main_loop_new(nullptr, FALSE);
            g_main_loop_run(loop_);
        });

        std::cout << "AudioCapture started" << std::endl;
    }

    void stop() {
        if (!running_.exchange(false)) return;
        if (pipeline_) {
            gst_element_set_state(pipeline_, GST_STATE_NULL);
            gst_object_unref(pipeline_);
            pipeline_ = nullptr;
        }
        if (loop_) {
            g_main_loop_quit(loop_);
            if (loop_thread_.joinable()) loop_thread_.join();
            g_main_loop_unref(loop_);
            loop_ = nullptr;
        }
    }

    std::vector<uint8_t> latestChunk() {
        std::lock_guard<std::mutex> lock(mutex_);
        return buffer_;
    }

private:
    AudioCapture() = default;
    ~AudioCapture() { stop(); }
    AudioCapture(const AudioCapture&) = delete;
    AudioCapture& operator=(const AudioCapture&) = delete;

    GstElement* pipeline_ = nullptr;
    GMainLoop* loop_ = nullptr;
    std::thread loop_thread_;
    std::atomic<bool> running_{false};
    std::mutex mutex_;
    std::vector<uint8_t> buffer_;
};

// ---------------------------------------------------------------------------
// WebSocket controller — broadcasts PCM at ~60 fps
// ---------------------------------------------------------------------------
class StreamWs : public WebSocketController<StreamWs> {
public:
    void handleNewMessage(const WebSocketConnectionPtr&,
                          std::string&&,
                          const WebSocketMessageType&) override {}

    void handleNewConnection(const HttpRequestPtr&,
                             const WebSocketConnectionPtr& conn) override {
        std::lock_guard<std::mutex> lock(connsMutex_);
        conns_.push_back(conn);
    }

    void handleConnectionClosed(const WebSocketConnectionPtr& conn) override {
        std::lock_guard<std::mutex> lock(connsMutex_);
        conns_.erase(
            std::remove_if(conns_.begin(), conns_.end(),
                           [&](const WebSocketConnectionPtr& c) {
                               return c == conn;
                           }),
            conns_.end());
    }

    static void broadcast() {
        auto chunk = AudioCapture::instance().latestChunk();
        if (chunk.empty()) return;

        std::lock_guard<std::mutex> lock(connsMutex_);
        for (auto it = conns_.begin(); it != conns_.end();) {
            if ((*it)->connected()) {
                (*it)->send(reinterpret_cast<const char*>(chunk.data()),
                            chunk.size(),
                            WebSocketMessageType::Binary);
                ++it;
            } else {
                it = conns_.erase(it);
            }
        }
    }

    WS_PATH_LIST_BEGIN
    WS_PATH_ADD("/stream")
    WS_PATH_LIST_END

private:
    static inline std::mutex connsMutex_;
    static inline std::vector<WebSocketConnectionPtr> conns_;
};

// ---------------------------------------------------------------------------
// Helper: list .wav files in ./assets
// ---------------------------------------------------------------------------
static Json::Value listWavFiles() {
    Json::Value arr(Json::arrayValue);
    DIR* dir = opendir("./assets");
    if (!dir) return arr;

    struct dirent* entry;
    while ((entry = readdir(dir)) != nullptr) {
        std::string name(entry->d_name);
        if (name.size() > 4 && name.substr(name.size() - 4) == ".wav") {
            Json::Value item;
            // Friendly name: strip extension
            item["name"] = name.substr(0, name.size() - 4);
            item["file"] = name;
            arr.append(item);
        }
    }
    closedir(dir);
    return arr;
}

// ---------------------------------------------------------------------------
// main
// ---------------------------------------------------------------------------
int main(int argc, char* argv[]) {
    gst_init(&argc, &argv);

    AudioCapture::instance().start();

    const char* env_hostname = std::getenv("WENDY_HOSTNAME");
    std::string hostname = env_hostname ? env_hostname : "0.0.0.0";

    // GET /sounds — list .wav files in ./assets
    app().registerHandler(
        "/sounds",
        [](const HttpRequestPtr&, std::function<void(const HttpResponsePtr&)>&& callback) {
            auto resp = HttpResponse::newHttpJsonResponse(listWavFiles());
            callback(resp);
        },
        {Get});

    // GET /health
    app().registerHandler(
        "/health",
        [](const HttpRequestPtr&, std::function<void(const HttpResponsePtr&)>&& callback) {
            Json::Value json;
            json["status"] = "ok";
            callback(HttpResponse::newHttpJsonResponse(json));
        },
        {Get});

    // Serve index.html and /assets/* from current directory
    app().setDocumentRoot(".");

    // ~60 fps broadcast timer (every 16 ms)
    app().getLoop()->runEvery(0.016, []() {
        StreamWs::broadcast();
    });

    std::cout << "Server running on http://" << hostname << ":{{.PORT}}" << std::endl;

    app().addListener("0.0.0.0", {{.PORT}});
    app().run();

    AudioCapture::instance().stop();
    gst_deinit();
    return 0;
}
