#include <drogon/drogon.h>
#include <drogon/WebSocketController.h>
#include <gst/gst.h>
#include <gst/app/gstappsink.h>

#include <algorithm>
#include <atomic>
#include <cctype>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <dirent.h>
#include <fstream>
#include <iostream>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

using namespace drogon;

static std::string trim(const std::string& value) {
    const auto start = value.find_first_not_of(" \t\r\n");
    if (start == std::string::npos) return "";
    const auto end = value.find_last_not_of(" \t\r\n");
    return value.substr(start, end - start + 1);
}

static std::string gstQuote(const std::string& value) {
    std::string escaped;
    escaped.reserve(value.size());
    for (char ch : value) {
        if (ch == '\\' || ch == '"') escaped.push_back('\\');
        escaped.push_back(ch);
    }
    return "\"" + escaped + "\"";
}

static std::string shellQuote(const std::string& value) {
    std::string quoted = "'";
    for (char ch : value) {
        if (ch == '\'') {
            quoted += "'\\''";
        } else {
            quoted.push_back(ch);
        }
    }
    quoted.push_back('\'');
    return quoted;
}

static std::string displayName(const std::string& file) {
    std::string name = file;
    if (name.size() > 4 && name.substr(name.size() - 4) == ".wav") {
        name.resize(name.size() - 4);
    }

    bool capitalize_next = true;
    for (char& ch : name) {
        if (ch == '-' || ch == '_') {
            ch = ' ';
            capitalize_next = true;
        } else if (capitalize_next) {
            ch = static_cast<char>(std::toupper(static_cast<unsigned char>(ch)));
            capitalize_next = false;
        }
    }
    return name;
}

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

        std::string device;
        {
            std::lock_guard<std::mutex> lock(deviceMutex_);
            device = current_device_;
        }
        const std::string source =
            device.empty() ? "autoaudiosrc" : "alsasrc device=" + gstQuote(device);
        const std::string pipeline_desc =
            source + " ! audioconvert ! audioresample ! "
            "audio/x-raw,format=S16LE,channels=1,rate=16000 ! "
            "appsink name=sink emit-signals=true max-buffers=4 drop=true sync=false";

        pipeline_ = gst_parse_launch(pipeline_desc.c_str(), nullptr);

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

    void switchMicrophone(const std::string& device) {
        {
            std::lock_guard<std::mutex> lock(deviceMutex_);
            current_device_ = device;
        }
        stop();
        start();
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
    std::mutex deviceMutex_;
    std::vector<uint8_t> buffer_;
    std::string current_device_;
};

// ---------------------------------------------------------------------------
// WebSocket controller — broadcasts PCM at ~60 fps
// ---------------------------------------------------------------------------
class StreamWs : public WebSocketController<StreamWs> {
public:
    void handleNewMessage(const WebSocketConnectionPtr&,
                          std::string&& message,
                          const WebSocketMessageType&) override {
        Json::Value root;
        Json::CharReaderBuilder builder;
        std::string errors;
        std::istringstream stream(message);
        if (!Json::parseFromStream(builder, stream, &root, &errors)) return;

        if (root.isMember("switch_microphone") && root["switch_microphone"].isString()) {
            AudioCapture::instance().switchMicrophone(root["switch_microphone"].asString());
        }
    }

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
    WS_PATH_ADD("/stream");
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
            item["name"] = displayName(name);
            item["file"] = name;
            arr.append(item);
        }
    }
    closedir(dir);
    return arr;
}

static Json::Value listAudioDevices(const char* command) {
    Json::Value arr(Json::arrayValue);
    FILE* pipe = popen(command, "r");
    if (!pipe) return arr;

    char* line = nullptr;
    size_t len = 0;
    while (getline(&line, &len, pipe) != -1) {
        std::string value(line);
        if (value.rfind("card ", 0) != 0) continue;

        const auto colon = value.find(':');
        if (colon == std::string::npos) continue;

        std::istringstream card_stream(value.substr(0, colon));
        std::string card_word;
        std::string card_num;
        card_stream >> card_word >> card_num;
        if (card_num.empty()) continue;
        if (card_num.back() == ':') card_num.pop_back();

        std::string name = value.substr(colon + 1);
        const auto bracket = name.find('[');
        if (bracket != std::string::npos) name = name.substr(0, bracket);
        name = trim(name);
        if (name.empty()) name = "Card " + card_num;

        Json::Value item;
        item["id"] = "hw:" + card_num + ",0";
        item["name"] = name;
        arr.append(item);
    }

    free(line);
    pclose(pipe);
    return arr;
}

static bool resolveSoundPath(const std::string& filename, std::string& out) {
    if (filename.find('/') != std::string::npos ||
        filename.find('\\') != std::string::npos ||
        filename.size() <= 4 ||
        filename.substr(filename.size() - 4) != ".wav") {
        return false;
    }

    out = "./assets/" + filename;
    std::ifstream file(out);
    return file.good();
}

static std::mutex speakerMutex;
static std::string currentSpeaker;

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

    app().registerHandler(
        "/microphones",
        [](const HttpRequestPtr&, std::function<void(const HttpResponsePtr&)>&& callback) {
            auto resp = HttpResponse::newHttpJsonResponse(listAudioDevices("arecord -l 2>/dev/null"));
            callback(resp);
        },
        {Get});

    app().registerHandler(
        "/speakers",
        [](const HttpRequestPtr&, std::function<void(const HttpResponsePtr&)>&& callback) {
            auto resp = HttpResponse::newHttpJsonResponse(listAudioDevices("aplay -l 2>/dev/null"));
            callback(resp);
        },
        {Get});

    app().registerHandler(
        "/speaker/{1}",
        [](const HttpRequestPtr&,
           std::function<void(const HttpResponsePtr&)>&& callback,
           const std::string& device_id) {
            {
                std::lock_guard<std::mutex> lock(speakerMutex);
                currentSpeaker = device_id;
            }
            Json::Value json;
            json["status"] = "ok";
            json["speaker"] = device_id;
            callback(HttpResponse::newHttpJsonResponse(json));
        },
        {Post});

    app().registerHandler(
        "/play/{1}",
        [](const HttpRequestPtr&,
           std::function<void(const HttpResponsePtr&)>&& callback,
           const std::string& filename) {
            std::string filepath;
            if (!resolveSoundPath(filename, filepath)) {
                Json::Value json;
                json["error"] = "not found";
                auto resp = HttpResponse::newHttpJsonResponse(json);
                resp->setStatusCode(k404NotFound);
                callback(resp);
                return;
            }

            std::string speaker;
            {
                std::lock_guard<std::mutex> lock(speakerMutex);
                speaker = currentSpeaker;
            }

            std::string sink = speaker.empty()
                ? "autoaudiosink"
                : "alsasink device=" + shellQuote(speaker);
            std::string command =
                "gst-launch-1.0 -q filesrc location=" + shellQuote(filepath) +
                " ! wavparse ! audioconvert ! audioresample ! " + sink;

            std::thread([command]() {
                std::system(command.c_str());
            }).detach();

            Json::Value json;
            json["status"] = "playing";
            json["file"] = filename;
            callback(HttpResponse::newHttpJsonResponse(json));
        },
        {Post});

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
