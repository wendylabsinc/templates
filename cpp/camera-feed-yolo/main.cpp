#include <drogon/drogon.h>
#include <drogon/WebSocketController.h>
#include <gst/gst.h>
#include <gst/app/gstappsink.h>
#include <onnxruntime_cxx_api.h>
#include <turbojpeg.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <mutex>
#include <set>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

namespace
{
constexpr int kInputSize = 640;

constexpr std::array<const char *, 80> kCocoNames = {
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat",
    "traffic light", "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat", "dog",
    "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella",
    "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball", "kite",
    "baseball bat", "baseball glove", "skateboard", "surfboard", "tennis racket", "bottle",
    "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple", "sandwich",
    "orange", "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
    "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse", "remote",
    "keyboard", "cell phone", "microwave", "oven", "toaster", "sink", "refrigerator", "book",
    "clock", "vase", "scissors", "teddy bear", "hair drier", "toothbrush"};

bool envTruthy(const char *name)
{
    const char *v = std::getenv(name);
    if (!v)
        return false;
    std::string s = v;
    std::transform(s.begin(), s.end(), s.begin(), ::tolower);
    return s == "true" || s == "1" || s == "yes";
}

bool isRpi()
{
    const char *dev = std::getenv("WENDY_DEVICE_TYPE");
    if (dev && std::string(dev).rfind("raspberrypi", 0) == 0)
        return true;
    if (dev && *dev)
        return false;
    FILE *f = std::fopen("/proc/device-tree/model", "r");
    if (!f)
        return false;
    char buf[256];
    size_t n = std::fread(buf, 1, sizeof(buf) - 1, f);
    std::fclose(f);
    buf[n] = '\0';
    return std::strstr(buf, "Raspberry Pi") != nullptr;
}

struct Detection
{
    float x1, y1, x2, y2;
    float conf;
    int cls;
};

float iou(const Detection &a, const Detection &b)
{
    float ix1 = std::max(a.x1, b.x1);
    float iy1 = std::max(a.y1, b.y1);
    float ix2 = std::min(a.x2, b.x2);
    float iy2 = std::min(a.y2, b.y2);
    float iw = std::max(0.0f, ix2 - ix1);
    float ih = std::max(0.0f, iy2 - iy1);
    float inter = iw * ih;
    float aa = std::max(0.0f, a.x2 - a.x1) * std::max(0.0f, a.y2 - a.y1);
    float bb = std::max(0.0f, b.x2 - b.x1) * std::max(0.0f, b.y2 - b.y1);
    float u = aa + bb - inter;
    return u > 0.0f ? inter / u : 0.0f;
}
} // namespace

// ---------------------------------------------------------------------------
// YoloEngine — wraps an ONNX Runtime session.
// ---------------------------------------------------------------------------
class YoloEngine
{
  public:
    YoloEngine(const std::string &modelPath, bool useGpu)
        : env_(ORT_LOGGING_LEVEL_WARNING, "yolo")
    {
        Ort::SessionOptions opts;
        opts.SetGraphOptimizationLevel(GraphOptimizationLevel::ORT_ENABLE_ALL);
        opts.SetIntraOpNumThreads(2);
        if (useGpu)
        {
            try
            {
                OrtCUDAProviderOptions cuda{};
                opts.AppendExecutionProvider_CUDA(cuda);
                LOG_INFO << "[yolo] CUDA execution provider enabled";
            }
            catch (const Ort::Exception &e)
            {
                LOG_WARN << "[yolo] CUDA EP append failed (" << e.what() << ") — falling back to CPU";
            }
        }
        else
        {
            LOG_INFO << "[yolo] using CPU execution provider";
        }
        session_ = std::make_unique<Ort::Session>(env_, modelPath.c_str(), opts);

        Ort::AllocatorWithDefaultOptions alloc;
        inputName_ = session_->GetInputNameAllocated(0, alloc).get();
        outputName_ = session_->GetOutputNameAllocated(0, alloc).get();
        decoder_ = tjInitDecompress();
    }

    ~YoloEngine()
    {
        if (decoder_)
            tjDestroy(decoder_);
    }

    bool infer(const std::vector<uint8_t> &jpeg, float confThreshold,
               std::vector<Detection> &out, int &origW, int &origH)
    {
        out.clear();
        origW = origH = 0;
        if (!decoder_)
            return false;

        int w = 0, h = 0, subsamp = 0, colorspace = 0;
        if (tjDecompressHeader3(decoder_, jpeg.data(), jpeg.size(), &w, &h, &subsamp, &colorspace) != 0)
        {
            return false;
        }
        std::vector<uint8_t> rgb(static_cast<size_t>(w) * h * 3);
        if (tjDecompress2(decoder_, jpeg.data(), jpeg.size(), rgb.data(), w, 0, h, TJPF_RGB, 0) != 0)
        {
            return false;
        }
        origW = w;
        origH = h;

        // Letterbox into kInputSize x kInputSize.
        float scale = std::min(static_cast<float>(kInputSize) / w, static_cast<float>(kInputSize) / h);
        int newW = static_cast<int>(std::round(w * scale));
        int newH = static_cast<int>(std::round(h * scale));
        int padX = (kInputSize - newW) / 2;
        int padY = (kInputSize - newH) / 2;

        std::vector<float> input(3 * kInputSize * kInputSize, 114.0f / 255.0f);
        const size_t plane = static_cast<size_t>(kInputSize) * kInputSize;
        for (int y = 0; y < newH; ++y)
        {
            int srcY = std::min(static_cast<int>((y + 0.5f) / scale), h - 1);
            const uint8_t *row = rgb.data() + static_cast<size_t>(srcY) * w * 3;
            int dstY = padY + y;
            for (int x = 0; x < newW; ++x)
            {
                int srcX = std::min(static_cast<int>((x + 0.5f) / scale), w - 1);
                const uint8_t *px = row + srcX * 3;
                int dstX = padX + x;
                size_t idx = static_cast<size_t>(dstY) * kInputSize + dstX;
                input[0 * plane + idx] = px[0] / 255.0f;
                input[1 * plane + idx] = px[1] / 255.0f;
                input[2 * plane + idx] = px[2] / 255.0f;
            }
        }

        std::array<int64_t, 4> inputShape{1, 3, kInputSize, kInputSize};
        Ort::MemoryInfo memInfo = Ort::MemoryInfo::CreateCpu(OrtArenaAllocator, OrtMemTypeDefault);
        Ort::Value inputTensor = Ort::Value::CreateTensor<float>(
            memInfo, input.data(), input.size(), inputShape.data(), inputShape.size());

        const char *inNames[] = {inputName_.c_str()};
        const char *outNames[] = {outputName_.c_str()};
        auto outputs = session_->Run(Ort::RunOptions{nullptr}, inNames, &inputTensor, 1, outNames, 1);
        if (outputs.empty())
            return false;

        const float *preds = outputs[0].GetTensorData<float>();
        auto info = outputs[0].GetTensorTypeAndShapeInfo();
        auto shape = info.GetShape();
        // Expected (1, 84, N).
        if (shape.size() != 3 || shape[1] < 84)
            return false;
        const int64_t numAnchors = shape[2];

        std::vector<Detection> candidates;
        candidates.reserve(256);
        for (int64_t i = 0; i < numAnchors; ++i)
        {
            int bestCls = 0;
            float bestScore = 0.0f;
            for (int c = 0; c < 80; ++c)
            {
                float s = preds[(4 + c) * numAnchors + i];
                if (s > bestScore)
                {
                    bestScore = s;
                    bestCls = c;
                }
            }
            if (bestScore < confThreshold)
                continue;
            float cx = preds[0 * numAnchors + i];
            float cy = preds[1 * numAnchors + i];
            float bw = preds[2 * numAnchors + i];
            float bh = preds[3 * numAnchors + i];
            float x1 = (cx - bw * 0.5f - padX) / scale;
            float y1 = (cy - bh * 0.5f - padY) / scale;
            float x2 = (cx + bw * 0.5f - padX) / scale;
            float y2 = (cy + bh * 0.5f - padY) / scale;
            x1 = std::clamp(x1, 0.0f, static_cast<float>(w - 1));
            y1 = std::clamp(y1, 0.0f, static_cast<float>(h - 1));
            x2 = std::clamp(x2, 0.0f, static_cast<float>(w - 1));
            y2 = std::clamp(y2, 0.0f, static_cast<float>(h - 1));
            candidates.push_back({x1, y1, x2, y2, bestScore, bestCls});
        }

        std::sort(candidates.begin(), candidates.end(),
                  [](const Detection &a, const Detection &b) { return a.conf > b.conf; });
        for (const auto &c : candidates)
        {
            bool drop = false;
            for (const auto &k : out)
            {
                if (k.cls == c.cls && iou(k, c) > 0.45f)
                {
                    drop = true;
                    break;
                }
            }
            if (!drop)
                out.push_back(c);
            if (out.size() >= 100)
                break;
        }
        return true;
    }

  private:
    Ort::Env env_;
    std::unique_ptr<Ort::Session> session_;
    std::string inputName_;
    std::string outputName_;
    tjhandle decoder_ = nullptr;
};

// ---------------------------------------------------------------------------
// YoloCamera — owns the GStreamer pipeline, the latest-frame slot, and the
// inference thread. WebSocket clients are tracked here too.
// ---------------------------------------------------------------------------
class YoloCamera
{
  public:
    static YoloCamera &instance()
    {
        static YoloCamera cam;
        return cam;
    }

    void init(bool useGpu, bool usePassthrough)
    {
        useGpu_ = useGpu;
        usePassthrough_ = usePassthrough;
        engine_ = std::make_unique<YoloEngine>("yolov8n.onnx", useGpu_);
        inferenceThread_ = std::thread(&YoloCamera::inferenceLoop, this);
    }

    void shutdown()
    {
        running_ = false;
        cv_.notify_all();
        if (inferenceThread_.joinable())
            inferenceThread_.join();
        std::lock_guard<std::mutex> pipelineLock(pipelineMutex_);
        stopPipelineLocked();
    }

    void setConfidence(float c) { confidence_.store(std::clamp(c, 0.05f, 0.95f)); }

    void addClient(const drogon::WebSocketConnectionPtr &conn)
    {
        bool firstClient = false;
        {
            std::lock_guard<std::mutex> lock(clientsMutex_);
            clients_.insert(conn);
            firstClient = clients_.size() == 1;
        }
        if (firstClient)
        {
            std::lock_guard<std::mutex> pipelineLock(pipelineMutex_);
            retryDelayMs_ = 1000;
            nextRetryAt_ = {};
            startPipelineLocked();
            if (pipeline_ == nullptr)
                bumpRetryLocked();
        }
    }

    void removeClient(const drogon::WebSocketConnectionPtr &conn)
    {
        bool lastClient = false;
        {
            std::lock_guard<std::mutex> lock(clientsMutex_);
            clients_.erase(conn);
            lastClient = clients_.empty();
        }
        if (lastClient)
        {
            std::lock_guard<std::mutex> pipelineLock(pipelineMutex_);
            stopPipelineLocked();
        }
    }

    void switchDevice(const std::string &device)
    {
        bool hadClients;
        {
            std::lock_guard<std::mutex> lock(clientsMutex_);
            hadClients = !clients_.empty();
        }
        std::lock_guard<std::mutex> pipelineLock(pipelineMutex_);
        device_ = device;
        if (hadClients)
        {
            stopPipelineLocked();
            retryDelayMs_ = 1000;
            nextRetryAt_ = {};
            startPipelineLocked();
            if (pipeline_ == nullptr)
                bumpRetryLocked();
        }
    }

    void broadcast()
    {
        std::vector<drogon::WebSocketConnectionPtr> targets;
        {
            std::lock_guard<std::mutex> lock(clientsMutex_);
            if (clients_.empty())
                return;
            targets.reserve(clients_.size());
            for (auto &conn : clients_)
                if (conn->connected())
                    targets.push_back(conn);
        }
        if (targets.empty())
            return;

        watchdogTick();

        std::vector<uint8_t> frame;
        std::string meta;
        {
            std::lock_guard<std::mutex> lock(frameMutex_);
            frame = latestFrame_;
            meta = latestMeta_;
        }
        if (frame.empty())
            return;

        for (auto &conn : targets)
        {
            conn->send(meta, drogon::WebSocketMessageType::Text);
            conn->send(reinterpret_cast<const char *>(frame.data()), frame.size(),
                       drogon::WebSocketMessageType::Binary);
        }
    }

    void watchdogTick()
    {
        using clock = std::chrono::steady_clock;
        const auto now = clock::now();
        const auto stallTimeout = std::chrono::seconds(2);

        clock::time_point lastFrame;
        {
            std::lock_guard<std::mutex> lock(frameMutex_);
            lastFrame = lastFrameTime_;
        }

        std::lock_guard<std::mutex> pipelineLock(pipelineMutex_);

        if (pipeline_ != nullptr && lastFrame.time_since_epoch().count() != 0 &&
            now - lastFrame > stallTimeout)
        {
            LOG_WARN << "[gst] pipeline stalled (no frames for "
                     << std::chrono::duration_cast<std::chrono::milliseconds>(now - lastFrame).count()
                     << "ms) — restarting";
            stopPipelineLocked();
        }

        if (pipeline_ == nullptr && now >= nextRetryAt_)
        {
            startPipelineLocked();
            if (pipeline_ == nullptr)
                bumpRetryLocked();
        }
    }

    void bumpRetryLocked()
    {
        LOG_INFO << "[gst] retry in " << retryDelayMs_ << "ms";
        nextRetryAt_ = std::chrono::steady_clock::now() +
                       std::chrono::milliseconds(retryDelayMs_);
        retryDelayMs_ = std::min<int64_t>(
            (retryDelayMs_ * 3) / 2, 5000);
    }

  private:
    YoloCamera()
        : latestMeta_(R"({"detections":0,"inference_ms":0,"classes":{},"boxes":[],"frame_w":0,"frame_h":0})")
    {
    }

    ~YoloCamera() { shutdown(); }

    static GstFlowReturn onNewSample(GstElement *sink, gpointer userData)
    {
        auto *self = static_cast<YoloCamera *>(userData);
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
                {
                    std::lock_guard<std::mutex> lock(self->frameMutex_);
                    self->latestFrame_.assign(map.data, map.data + map.size);
                    self->latestForInference_.assign(map.data, map.data + map.size);
                    self->haveNewFrame_ = true;
                    self->lastFrameTime_ = std::chrono::steady_clock::now();
                }
                self->cv_.notify_one();
                gst_buffer_unmap(buffer, &map);
            }
        }
        gst_sample_unref(sample);
        return GST_FLOW_OK;
    }

    // Caller must hold pipelineMutex_.
    void startPipelineLocked()
    {
        if (pipeline_)
            return;

        // Passthrough on RPi/CPU avoids a 30fps decode/re-encode brown-out under
        // GStreamer + inference load. Jetson keeps the decode/encode for quality
        // since it has hardware JPEG codecs.
        std::string inner = usePassthrough_
                                ? "image/jpeg ! appsink name=sink emit-signals=true max-buffers=2 drop=true sync=false"
                                : "image/jpeg ! jpegdec ! jpegenc quality=85 ! appsink name=sink emit-signals=true max-buffers=2 drop=true sync=false";
        std::string desc = "v4l2src device=" + device_ + " ! " + inner;

        GError *err = nullptr;
        GstElement *pipeline = gst_parse_launch(desc.c_str(), &err);
        if (err)
        {
            LOG_ERROR << "GStreamer pipeline error: " << err->message;
            g_error_free(err);
            return;
        }
        GstElement *sink = gst_bin_get_by_name(GST_BIN(pipeline), "sink");
        if (!sink)
        {
            LOG_ERROR << "appsink missing";
            gst_object_unref(pipeline);
            return;
        }
        g_signal_connect(sink, "new-sample", G_CALLBACK(&YoloCamera::onNewSample), this);
        gst_object_unref(sink);
        GstStateChangeReturn ret = gst_element_set_state(pipeline, GST_STATE_PLAYING);
        if (ret == GST_STATE_CHANGE_FAILURE)
        {
            LOG_ERROR << "GStreamer set_state(PLAYING) failed";
            gst_element_set_state(pipeline, GST_STATE_NULL);
            gst_object_unref(pipeline);
            return;
        }
        pipeline_ = pipeline;
        retryDelayMs_ = 1000;
        nextRetryAt_ = std::chrono::steady_clock::time_point{};
        {
            std::lock_guard<std::mutex> lock(frameMutex_);
            lastFrameTime_ = std::chrono::steady_clock::now();
        }
        LOG_INFO << "GStreamer pipeline started on " << device_
                 << " (passthrough=" << usePassthrough_ << ")";
    }

    // Caller must hold pipelineMutex_.
    void stopPipelineLocked()
    {
        if (!pipeline_)
            return;
        gst_element_set_state(pipeline_, GST_STATE_NULL);
        gst_object_unref(pipeline_);
        pipeline_ = nullptr;
        std::lock_guard<std::mutex> lock(frameMutex_);
        latestFrame_.clear();
        latestForInference_.clear();
        haveNewFrame_ = false;
        lastFrameTime_ = {};
    }

    void inferenceLoop()
    {
        using clock = std::chrono::steady_clock;
        auto minInterval = std::chrono::milliseconds(useGpu_ ? 1000 / 15 : 1000 / 3);
        auto lastRun = clock::now() - minInterval;

        while (running_)
        {
            {
                std::unique_lock<std::mutex> lock(frameMutex_);
                cv_.wait(lock, [this] { return !running_ || haveNewFrame_; });
                if (!running_)
                    return;
            }

            auto sinceLast = clock::now() - lastRun;
            if (sinceLast < minInterval)
                std::this_thread::sleep_for(minInterval - sinceLast);

            std::vector<uint8_t> jpeg;
            {
                std::lock_guard<std::mutex> lock(frameMutex_);
                jpeg = std::move(latestForInference_);
                haveNewFrame_ = false;
            }
            if (!engine_ || jpeg.empty())
                continue;

            std::vector<Detection> dets;
            int w = 0, h = 0;
            auto t0 = clock::now();
            bool ok = false;
            try
            {
                ok = engine_->infer(jpeg, confidence_.load(), dets, w, h);
            }
            catch (const std::exception &e)
            {
                LOG_ERROR << "[yolo] inference error: " << e.what();
                lastRun = clock::now();
                continue;
            }
            auto t1 = clock::now();
            lastRun = t1;
            double inferMs = std::chrono::duration<double, std::milli>(t1 - t0).count();
            if (!ok)
                continue;

            Json::Value json;
            json["detections"] = static_cast<Json::Int64>(dets.size());
            json["inference_ms"] = std::round(inferMs * 10.0) / 10.0;
            json["frame_w"] = w;
            json["frame_h"] = h;
            Json::Value boxes(Json::arrayValue);
            Json::Value classes(Json::objectValue);
            for (const auto &d : dets)
            {
                Json::Value b;
                b["x1"] = d.x1;
                b["y1"] = d.y1;
                b["x2"] = d.x2;
                b["y2"] = d.y2;
                b["conf"] = d.conf;
                b["cls"] = d.cls;
                b["name"] = kCocoNames[d.cls];
                boxes.append(b);
                const char *name = kCocoNames[d.cls];
                classes[name] = classes.get(name, 0).asInt() + 1;
            }
            json["boxes"] = boxes;
            json["classes"] = classes;

            Json::StreamWriterBuilder writer;
            writer["indentation"] = "";
            std::string serialized = Json::writeString(writer, json);
            {
                std::lock_guard<std::mutex> lock(frameMutex_);
                latestMeta_ = std::move(serialized);
            }
        }
    }

    bool useGpu_ = false;
    bool usePassthrough_ = false;
    std::atomic<bool> running_{true};
    std::atomic<float> confidence_{0.25f};

    std::unique_ptr<YoloEngine> engine_;
    std::thread inferenceThread_;

    mutable std::mutex frameMutex_;
    std::vector<uint8_t> latestFrame_;
    std::vector<uint8_t> latestForInference_;
    std::string latestMeta_;
    bool haveNewFrame_ = false;
    std::chrono::steady_clock::time_point lastFrameTime_{};
    std::condition_variable cv_;

    std::mutex clientsMutex_;
    std::set<drogon::WebSocketConnectionPtr> clients_;

    // pipelineMutex_ guards: pipeline_, device_, retryDelayMs_, nextRetryAt_.
    std::mutex pipelineMutex_;
    GstElement *pipeline_ = nullptr;
    std::string device_ = "/dev/video0";
    int64_t retryDelayMs_ = 1000;
    std::chrono::steady_clock::time_point nextRetryAt_{};
};

// ---------------------------------------------------------------------------
// WebSocket controller at /stream
// ---------------------------------------------------------------------------
class StreamWs : public drogon::WebSocketController<StreamWs>
{
  public:
    void handleNewConnection(const drogon::HttpRequestPtr &,
                             const drogon::WebSocketConnectionPtr &conn) override
    {
        LOG_INFO << "WebSocket client connected";
        YoloCamera::instance().addClient(conn);
    }

    void handleNewMessage(const drogon::WebSocketConnectionPtr &,
                          std::string &&message,
                          const drogon::WebSocketMessageType &type) override
    {
        if (type != drogon::WebSocketMessageType::Text)
            return;
        Json::Value json;
        Json::CharReaderBuilder builder;
        std::string errs;
        std::istringstream stream(message);
        if (!Json::parseFromStream(builder, stream, &json, &errs))
            return;
        if (json.isMember("switch_camera"))
        {
            YoloCamera::instance().switchDevice(json["switch_camera"].asString());
        }
        if (json.isMember("confidence"))
        {
            YoloCamera::instance().setConfidence(json["confidence"].asFloat());
        }
    }

    void handleConnectionClosed(const drogon::WebSocketConnectionPtr &conn) override
    {
        LOG_INFO << "WebSocket client disconnected";
        YoloCamera::instance().removeClient(conn);
    }

    WS_PATH_LIST_BEGIN
    WS_PATH_ADD("/stream");
    WS_PATH_LIST_END
};

// ---------------------------------------------------------------------------
// GLib main loop pump (so GStreamer signals fire)
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
    gst_init(nullptr, nullptr);

    bool useGpu = envTruthy("WENDY_HAS_GPU");
    bool rpi = isRpi();
    bool usePassthrough = !useGpu || rpi;
    LOG_INFO << "Startup: has_gpu=" << useGpu << " is_rpi=" << rpi
             << " capture=" << (usePassthrough ? "passthrough" : "decode-encode");

    try
    {
        YoloCamera::instance().init(useGpu, usePassthrough);
    }
    catch (const std::exception &e)
    {
        LOG_ERROR << "[yolo] failed to initialize: " << e.what();
        return 1;
    }

    std::thread glibThread(glibMainLoopThread);
    glibThread.detach();

    drogon::app().registerHandler(
        "/cameras",
        [](const drogon::HttpRequestPtr &,
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
                    auto paren = line.find('(');
                    currentName = paren != std::string::npos ? line.substr(0, paren) : line;
                    while (!currentName.empty() &&
                           (currentName.back() == ' ' || currentName.back() == ':'))
                        currentName.pop_back();
                }
                else
                {
                    std::string dev = line;
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

    drogon::app().setDocumentRoot(".");

    // Broadcast at ~30fps regardless of inference rate (display is camera rate;
    // box overlay is rendered client-side from the latest meta).
    drogon::app().getLoop()->runEvery(std::chrono::milliseconds(33), []() {
        YoloCamera::instance().broadcast();
    });

    LOG_INFO << "Starting server on 0.0.0.0:{{.PORT}}";
    drogon::app()
        .addListener("0.0.0.0", {{.PORT}})
        .run();

    g_running.store(false);
    YoloCamera::instance().shutdown();
    return 0;
}
