#include <drogon/drogon.h>
#include <json/json.h>
#include <librealsense2/rs.hpp>
#include <turbojpeg.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <exception>
#include <functional>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <sstream>
#include <string>
#include <thread>
#include <unordered_map>
#include <utility>
#include <vector>

namespace
{
constexpr const char *kBoundary = "frame";
constexpr int kJpegQuality = 80;
const std::array<std::string, 4> kStreamIds = {"color", "ir-left", "ir-right", "depth"};

const std::unordered_map<std::string, int> kPresetValues = {
    {"default", RS2_RS400_VISUAL_PRESET_DEFAULT},
    {"hand", RS2_RS400_VISUAL_PRESET_HAND},
    {"high-accuracy", RS2_RS400_VISUAL_PRESET_HIGH_ACCURACY},
    {"high-density", RS2_RS400_VISUAL_PRESET_HIGH_DENSITY},
    {"medium-density", RS2_RS400_VISUAL_PRESET_MEDIUM_DENSITY},
};

bool isKnownStream(const std::string &streamId)
{
    return std::find(kStreamIds.begin(), kStreamIds.end(), streamId) != kStreamIds.end();
}

Json::Value jsonError(const std::string &message)
{
    Json::Value root;
    root["error"] = message;
    return root;
}

bool parseIntParam(const drogon::HttpRequestPtr &req, const std::string &name, int fallback,
                   int minValue, int maxValue, int &out, std::string &error)
{
    std::string raw = req->getParameter(name);
    if (raw.empty())
    {
        out = fallback;
        return true;
    }

    try
    {
        size_t parsed = 0;
        int value = std::stoi(raw, &parsed, 10);
        if (parsed != raw.size())
        {
            error = name + " must be an integer";
            return false;
        }
        if (value < minValue || value > maxValue)
        {
            std::ostringstream oss;
            oss << name << " must be between " << minValue << " and " << maxValue;
            error = oss.str();
            return false;
        }
        out = value;
        return true;
    }
    catch (const std::exception &)
    {
        error = name + " must be an integer";
        return false;
    }
}

struct EncodedFrame
{
    std::vector<uint8_t> jpeg;
    uint64_t sequence = 0;
};

struct FrameSnapshot
{
    std::vector<uint8_t> jpeg;
    uint64_t sequence = 0;
};

std::optional<std::vector<uint8_t>> encodeJpeg(tjhandle encoder, const uint8_t *pixels, int width,
                                               int height, int pixelFormat, int subsampling)
{
    if (!encoder || !pixels || width <= 0 || height <= 0)
    {
        return std::nullopt;
    }

    unsigned char *jpegBuffer = nullptr;
    unsigned long jpegSize = 0;
    int rc = tjCompress2(encoder, pixels, width, 0, height, pixelFormat, &jpegBuffer, &jpegSize,
                         subsampling, kJpegQuality, TJFLAG_FASTDCT);

    if (rc != 0 || !jpegBuffer)
    {
        if (jpegBuffer)
        {
            tjFree(jpegBuffer);
        }
        return std::nullopt;
    }

    std::vector<uint8_t> out(jpegBuffer, jpegBuffer + jpegSize);
    tjFree(jpegBuffer);
    return out;
}

std::optional<std::vector<uint8_t>>
encodeVideoFrame(tjhandle encoder, const rs2::video_frame &frame, int pixelFormat, int subsampling)
{
    return encodeJpeg(encoder, static_cast<const uint8_t *>(frame.get_data()), frame.get_width(),
                      frame.get_height(), pixelFormat, subsampling);
}

std::string makeMjpegPart(const std::vector<uint8_t> &jpeg)
{
    std::string part;
    part.reserve(jpeg.size() + 128);
    part += "--";
    part += kBoundary;
    part += "\r\nContent-Type: image/jpeg\r\nContent-Length: ";
    part += std::to_string(jpeg.size());
    part += "\r\n\r\n";
    part.append(reinterpret_cast<const char *>(jpeg.data()), jpeg.size());
    part += "\r\n";
    return part;
}
} // namespace

class RealSensePump
{
  public:
    static RealSensePump &instance()
    {
        static RealSensePump pump;
        return pump;
    }

    ~RealSensePump() { stop(); }

    bool running() const
    {
        std::lock_guard<std::mutex> lock(stateMutex_);
        return running_;
    }

    void start()
    {
        std::lock_guard<std::mutex> lifecycle(lifecycleMutex_);
        reapStoppedWorkerLocked();
        startWorkerLocked();
    }

    void stop()
    {
        std::lock_guard<std::mutex> lifecycle(lifecycleMutex_);
        stopWorkerLocked(true, false);
    }

    bool configure(int width, int height, int fps, const std::string &preset)
    {
        std::lock_guard<std::mutex> lifecycle(lifecycleMutex_);
        reapStoppedWorkerLocked();

        bool restart = false;
        {
            std::lock_guard<std::mutex> lock(stateMutex_);
            restart = running_ && worker_.joinable() &&
                      (width != width_ || height != height_ || fps != fps_);
            width_ = width;
            height_ = height;
            fps_ = fps;
            preset_ = preset;
            pendingPreset_ = preset;
        }

        if (restart)
        {
            stopWorkerLocked(false, true);
            startWorkerLocked();
        }

        return true;
    }

    std::optional<FrameSnapshot> waitForFrame(const std::string &streamId, uint64_t lastSequence,
                                              std::chrono::milliseconds timeout)
    {
        auto deadline = std::chrono::steady_clock::now() + timeout;
        std::unique_lock<std::mutex> lock(stateMutex_);
        for (;;)
        {
            auto it = latest_.find(streamId);
            if (it != latest_.end() && it->second.sequence != lastSequence)
            {
                return FrameSnapshot{it->second.jpeg, it->second.sequence};
            }
            if (!running_)
            {
                return std::nullopt;
            }
            if (cond_.wait_until(lock, deadline) == std::cv_status::timeout)
            {
                return std::nullopt;
            }
        }
    }

    Json::Value healthJson() const
    {
        std::lock_guard<std::mutex> lock(stateMutex_);
        Json::Value root;
        Json::Value streams(Json::arrayValue);
        for (const auto &streamId : kStreamIds)
        {
            streams.append(streamId);
        }
        Json::Value fps(Json::objectValue);
        for (const auto &streamId : kStreamIds)
        {
            fps[streamId] = fpsLatest_.at(streamId);
        }
        root["streams"] = streams;
        root["running"] = running_;
        root["fps"] = fps;
        return root;
    }

  private:
    RealSensePump()
    {
        for (const auto &streamId : kStreamIds)
        {
            fpsCounts_[streamId] = 0;
            fpsLatest_[streamId] = 0.0;
        }
    }

    RealSensePump(const RealSensePump &) = delete;
    RealSensePump &operator=(const RealSensePump &) = delete;

    void reapStoppedWorkerLocked()
    {
        std::thread oldThread;
        {
            std::lock_guard<std::mutex> lock(stateMutex_);
            if (worker_.joinable() && !running_)
            {
                oldThread = std::move(worker_);
            }
        }
        if (oldThread.joinable())
        {
            oldThread.join();
        }
    }

    void startWorkerLocked()
    {
        std::lock_guard<std::mutex> lock(stateMutex_);
        if (worker_.joinable())
        {
            return;
        }
        stopRequested_.store(false);
        pendingPreset_ = preset_;
        running_ = true;
        fpsWindowStart_ = std::chrono::steady_clock::now();
        for (const auto &streamId : kStreamIds)
        {
            fpsCounts_[streamId] = 0;
            fpsLatest_[streamId] = 0.0;
        }
        worker_ = std::thread(&RealSensePump::run, this);
    }

    void stopWorkerLocked(bool clearLatest, bool keepRunning)
    {
        std::thread oldThread;
        {
            std::lock_guard<std::mutex> lock(stateMutex_);
            if (!worker_.joinable())
            {
                if (!keepRunning)
                {
                    running_ = false;
                    cond_.notify_all();
                }
                return;
            }
            stopRequested_.store(true);
            if (!keepRunning)
            {
                running_ = false;
                cond_.notify_all();
            }
            oldThread = std::move(worker_);
        }

        if (oldThread.joinable())
        {
            oldThread.join();
        }

        if (clearLatest)
        {
            std::lock_guard<std::mutex> lock(stateMutex_);
            latest_.clear();
            for (const auto &streamId : kStreamIds)
            {
                fpsCounts_[streamId] = 0;
                fpsLatest_[streamId] = 0.0;
            }
            cond_.notify_all();
        }
    }

    void markStopped()
    {
        std::lock_guard<std::mutex> lock(stateMutex_);
        running_ = false;
        for (const auto &streamId : kStreamIds)
        {
            fpsCounts_[streamId] = 0;
            fpsLatest_[streamId] = 0.0;
        }
        cond_.notify_all();
    }

    void publish(std::map<std::string, std::vector<uint8_t>> updates)
    {
        if (updates.empty())
        {
            return;
        }

        std::lock_guard<std::mutex> lock(stateMutex_);
        for (auto &entry : updates)
        {
            auto &slot = latest_[entry.first];
            slot.jpeg = std::move(entry.second);
            slot.sequence += 1;
            fpsCounts_[entry.first] += 1;
        }

        auto now = std::chrono::steady_clock::now();
        double elapsed = std::chrono::duration<double>(now - fpsWindowStart_).count();
        if (elapsed >= 1.0)
        {
            for (const auto &streamId : kStreamIds)
            {
                fpsLatest_[streamId] = std::round((fpsCounts_[streamId] / elapsed) * 10.0) / 10.0;
                fpsCounts_[streamId] = 0;
            }
            fpsWindowStart_ = now;
        }
        cond_.notify_all();
    }

    std::optional<std::string> takePendingPreset()
    {
        std::lock_guard<std::mutex> lock(stateMutex_);
        auto preset = pendingPreset_;
        pendingPreset_.reset();
        return preset;
    }

    void applyPendingPreset(const std::optional<rs2::sensor> &depthSensor)
    {
        auto preset = takePendingPreset();
        if (!preset || !depthSensor)
        {
            return;
        }

        auto it = kPresetValues.find(*preset);
        if (it == kPresetValues.end())
        {
            LOG_WARN << "Unknown RealSense visual preset: " << *preset;
            return;
        }

        try
        {
            if (depthSensor->supports(RS2_OPTION_VISUAL_PRESET))
            {
                depthSensor->set_option(RS2_OPTION_VISUAL_PRESET, static_cast<float>(it->second));
                LOG_INFO << "Applied RealSense visual preset: " << *preset;
            }
        }
        catch (const rs2::error &e)
        {
            LOG_ERROR << "Failed to apply RealSense visual preset " << *preset << ": " << e.what();
        }
    }

    void run()
    {
        int width = 640;
        int height = 480;
        int fps = 30;
        {
            std::lock_guard<std::mutex> lock(stateMutex_);
            width = width_;
            height = height_;
            fps = fps_;
        }

        rs2::pipeline pipeline;
        rs2::config config;
        config.enable_stream(RS2_STREAM_COLOR, width, height, RS2_FORMAT_BGR8, fps);
        config.enable_stream(RS2_STREAM_DEPTH, width, height, RS2_FORMAT_Z16, fps);
        config.enable_stream(RS2_STREAM_INFRARED, 1, width, height, RS2_FORMAT_Y8, fps);
        config.enable_stream(RS2_STREAM_INFRARED, 2, width, height, RS2_FORMAT_Y8, fps);

        std::optional<rs2::pipeline_profile> profile;
        for (int attempt = 1; attempt <= 3 && !stopRequested_.load(); ++attempt)
        {
            try
            {
                profile.emplace(pipeline.start(config));
                break;
            }
            catch (const rs2::error &e)
            {
                LOG_WARN << "pipeline.start attempt " << attempt << "/3 failed at " << width << "x"
                         << height << " @" << fps << "fps: " << e.what();
                std::this_thread::sleep_for(std::chrono::milliseconds(500));
            }
        }

        if (!profile)
        {
            LOG_ERROR << "Failed to start RealSense pipeline";
            markStopped();
            return;
        }

        std::optional<rs2::sensor> depthSensor;
        try
        {
            for (auto &&sensor : profile->get_device().query_sensors())
            {
                if (sensor.supports(RS2_OPTION_VISUAL_PRESET))
                {
                    depthSensor.emplace(sensor);
                    break;
                }
            }
            if (!depthSensor)
            {
                LOG_WARN << "No RealSense sensor supports visual presets";
            }
        }
        catch (const rs2::error &e)
        {
            LOG_WARN << "Could not inspect RealSense sensors for presets: " << e.what();
        }

        tjhandle encoder = tjInitCompress();
        if (!encoder)
        {
            LOG_ERROR << "Failed to initialize TurboJPEG encoder";
            try
            {
                pipeline.stop();
            }
            catch (const rs2::error &)
            {
            }
            markStopped();
            return;
        }

        rs2::colorizer colorizer;
        LOG_INFO << "RealSense pipeline started at " << width << "x" << height << " @" << fps
                 << "fps";

        try
        {
            while (!stopRequested_.load())
            {
                applyPendingPreset(depthSensor);

                rs2::frameset frames;
                try
                {
                    frames = pipeline.wait_for_frames(1000);
                }
                catch (const rs2::error &)
                {
                    continue;
                }

                std::map<std::string, std::vector<uint8_t>> updates;

                if (auto color = frames.get_color_frame())
                {
                    auto encoded = encodeVideoFrame(encoder, color, TJPF_BGR, TJSAMP_420);
                    if (encoded)
                    {
                        updates["color"] = std::move(*encoded);
                    }
                }

                if (auto irLeft = frames.get_infrared_frame(1))
                {
                    auto encoded = encodeVideoFrame(encoder, irLeft, TJPF_GRAY, TJSAMP_GRAY);
                    if (encoded)
                    {
                        updates["ir-left"] = std::move(*encoded);
                    }
                }

                if (auto irRight = frames.get_infrared_frame(2))
                {
                    auto encoded = encodeVideoFrame(encoder, irRight, TJPF_GRAY, TJSAMP_GRAY);
                    if (encoded)
                    {
                        updates["ir-right"] = std::move(*encoded);
                    }
                }

                if (auto depth = frames.get_depth_frame())
                {
                    rs2::frame colorized = colorizer.colorize(depth);
                    auto video = colorized.as<rs2::video_frame>();
                    auto encoded = encodeVideoFrame(encoder, video, TJPF_RGB, TJSAMP_420);
                    if (encoded)
                    {
                        updates["depth"] = std::move(*encoded);
                    }
                }

                publish(std::move(updates));
            }
        }
        catch (const rs2::error &e)
        {
            LOG_ERROR << "RealSense worker error: " << e.what();
            markStopped();
        }

        tjDestroy(encoder);
        try
        {
            pipeline.stop();
        }
        catch (const rs2::error &)
        {
        }

        if (!stopRequested_.load())
        {
            markStopped();
        }
        LOG_INFO << "RealSense pipeline stopped";
    }

    mutable std::mutex stateMutex_;
    std::mutex lifecycleMutex_;
    std::condition_variable cond_;
    std::thread worker_;
    std::atomic<bool> stopRequested_{false};
    bool running_ = false;

    int width_ = 640;
    int height_ = 480;
    int fps_ = 30;
    std::string preset_ = "default";
    std::optional<std::string> pendingPreset_;

    std::unordered_map<std::string, EncodedFrame> latest_;
    std::unordered_map<std::string, int> fpsCounts_;
    std::unordered_map<std::string, double> fpsLatest_;
    std::chrono::steady_clock::time_point fpsWindowStart_ = std::chrono::steady_clock::now();
};

int main()
{
    const char *hostname = std::getenv("WENDY_HOSTNAME");
    if (hostname)
    {
        LOG_INFO << "WENDY_HOSTNAME: " << hostname;
    }

    drogon::app().registerHandler(
        "/start",
        [](const drogon::HttpRequestPtr &,
           std::function<void(const drogon::HttpResponsePtr &)> &&callback)
        {
            RealSensePump::instance().start();
            Json::Value root;
            root["running"] = RealSensePump::instance().running();
            callback(drogon::HttpResponse::newHttpJsonResponse(root));
        },
        {drogon::Post});

    drogon::app().registerHandler(
        "/stop",
        [](const drogon::HttpRequestPtr &,
           std::function<void(const drogon::HttpResponsePtr &)> &&callback)
        {
            RealSensePump::instance().stop();
            Json::Value root;
            root["running"] = RealSensePump::instance().running();
            callback(drogon::HttpResponse::newHttpJsonResponse(root));
        },
        {drogon::Post});

    drogon::app().registerHandler(
        "/config",
        [](const drogon::HttpRequestPtr &req,
           std::function<void(const drogon::HttpResponsePtr &)> &&callback)
        {
            int width = 640;
            int height = 480;
            int fps = 30;
            std::string error;
            if (!parseIntParam(req, "width", width, 1, 8192, width, error) ||
                !parseIntParam(req, "height", height, 1, 8192, height, error) ||
                !parseIntParam(req, "fps", fps, 1, 300, fps, error))
            {
                auto resp = drogon::HttpResponse::newHttpJsonResponse(jsonError(error));
                resp->setStatusCode(drogon::k400BadRequest);
                callback(resp);
                return;
            }

            std::string preset = req->getParameter("preset");
            if (preset.empty())
            {
                preset = "default";
            }
            if (kPresetValues.find(preset) == kPresetValues.end())
            {
                auto resp = drogon::HttpResponse::newHttpJsonResponse(
                    jsonError("Unknown preset: " + preset));
                resp->setStatusCode(drogon::k400BadRequest);
                callback(resp);
                return;
            }

            RealSensePump::instance().configure(width, height, fps, preset);

            Json::Value root;
            root["width"] = width;
            root["height"] = height;
            root["fps"] = fps;
            root["preset"] = preset;
            callback(drogon::HttpResponse::newHttpJsonResponse(root));
        },
        {drogon::Post});

    drogon::app().registerHandler(
        "/health",
        [](const drogon::HttpRequestPtr &,
           std::function<void(const drogon::HttpResponsePtr &)> &&callback)
        {
            callback(
                drogon::HttpResponse::newHttpJsonResponse(RealSensePump::instance().healthJson()));
        },
        {drogon::Get});

    drogon::app().registerHandler(
        "/stream/{1}",
        [](const drogon::HttpRequestPtr &req,
           std::function<void(const drogon::HttpResponsePtr &)> &&callback,
           const std::string &streamId)
        {
            if (!isKnownStream(streamId))
            {
                auto resp = drogon::HttpResponse::newHttpJsonResponse(
                    jsonError("Unknown stream: " + streamId));
                resp->setStatusCode(drogon::k404NotFound);
                callback(resp);
                return;
            }

            struct StreamState
            {
                std::string streamId;
                uint64_t lastSequence = 0;
                std::string pending;
                size_t offset = 0;
            };
            auto state = std::make_shared<StreamState>();
            state->streamId = streamId;

            auto resp = drogon::HttpResponse::newStreamResponse(
                [state](char *buffer, std::size_t bufferSize) -> std::size_t
                {
                    if (buffer == nullptr || bufferSize == 0)
                    {
                        state->pending.clear();
                        return 0;
                    }

                    if (state->offset >= state->pending.size())
                    {
                        auto frame = RealSensePump::instance().waitForFrame(
                            state->streamId, state->lastSequence, std::chrono::milliseconds(5000));
                        if (!frame)
                        {
                            return 0;
                        }
                        state->lastSequence = frame->sequence;
                        state->pending = makeMjpegPart(frame->jpeg);
                        state->offset = 0;
                    }

                    size_t available = state->pending.size() - state->offset;
                    size_t n = std::min(bufferSize, available);
                    std::memcpy(buffer, state->pending.data() + state->offset, n);
                    state->offset += n;
                    return n;
                },
                "", drogon::CT_CUSTOM,
                std::string("multipart/x-mixed-replace; boundary=") + kBoundary, req);
            resp->addHeader("Cache-Control", "no-store");
            callback(resp);
        },
        {drogon::Get});

    drogon::app().setDocumentRoot("./static");
    drogon::app().setThreadNum(8);

    LOG_INFO << "Starting RealSense C++ server on 0.0.0.0:{{.PORT}}";
    drogon::app().addListener("0.0.0.0", {{.PORT}}).run();
    RealSensePump::instance().stop();
    return 0;
}
