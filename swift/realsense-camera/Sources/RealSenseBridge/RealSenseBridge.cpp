#include "RealSenseBridge.h"

#include <librealsense2/rs.hpp>
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
#include <exception>
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

bool isKnownPreset(const std::string &preset)
{
    return kPresetValues.find(preset) != kPresetValues.end();
}

void setError(char *error, size_t errorLength, const std::string &message)
{
    if (!error || errorLength == 0)
    {
        return;
    }
    std::snprintf(error, errorLength, "%s", message.c_str());
}

char *copyString(const std::string &value)
{
    auto *out = static_cast<char *>(std::malloc(value.size() + 1));
    if (!out)
    {
        return nullptr;
    }
    std::memcpy(out, value.c_str(), value.size() + 1);
    return out;
}

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

std::string jsonEscape(const std::string &value)
{
    std::ostringstream out;
    for (char ch : value)
    {
        switch (ch)
        {
        case '"':
            out << "\\\"";
            break;
        case '\\':
            out << "\\\\";
            break;
        case '\n':
            out << "\\n";
            break;
        case '\r':
            out << "\\r";
            break;
        case '\t':
            out << "\\t";
            break;
        default:
            out << ch;
            break;
        }
    }
    return out.str();
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

class RealSenseBridge
{
  public:
    RealSenseBridge()
    {
        for (const auto &streamId : kStreamIds)
        {
            fpsCounts_[streamId] = 0;
            fpsLatest_[streamId] = 0.0;
        }
    }

    ~RealSenseBridge() { stop(); }

    RealSenseBridge(const RealSenseBridge &) = delete;
    RealSenseBridge &operator=(const RealSenseBridge &) = delete;

    bool running() const
    {
        std::lock_guard<std::mutex> lock(stateMutex_);
        return running_;
    }

    bool start(std::string &error)
    {
        std::lock_guard<std::mutex> lifecycle(lifecycleMutex_);
        reapStoppedWorkerLocked();
        if (running_)
        {
            return true;
        }
        return startWorkerLocked(error);
    }

    void stop()
    {
        std::lock_guard<std::mutex> lifecycle(lifecycleMutex_);
        stopWorkerLocked(true, false);
    }

    bool configure(int width, int height, int fps, const std::string &preset, std::string &error)
    {
        if (width < 1 || width > 8192)
        {
            error = "width must be between 1 and 8192";
            return false;
        }
        if (height < 1 || height > 8192)
        {
            error = "height must be between 1 and 8192";
            return false;
        }
        if (fps < 1 || fps > 300)
        {
            error = "fps must be between 1 and 300";
            return false;
        }
        if (!isKnownPreset(preset))
        {
            error = "Unknown preset: " + preset;
            return false;
        }

        bool restart = false;
        {
            std::lock_guard<std::mutex> lock(stateMutex_);
            restart = worker_.joinable() && (width != width_ || height != height_ || fps != fps_);
            width_ = width;
            height_ = height;
            fps_ = fps;
            preset_ = preset;
            pendingPreset_ = preset;
        }

        if (restart)
        {
            std::lock_guard<std::mutex> lifecycle(lifecycleMutex_);
            stopWorkerLocked(true, true);
            return startWorkerLocked(error);
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

    std::string healthJson() const
    {
        std::lock_guard<std::mutex> lock(stateMutex_);
        std::ostringstream out;
        out << "{\"streams\":[";
        for (size_t i = 0; i < kStreamIds.size(); ++i)
        {
            if (i > 0)
            {
                out << ",";
            }
            out << "\"" << kStreamIds[i] << "\"";
        }
        out << "],\"running\":" << (running_ ? "true" : "false") << ",\"fps\":{";
        for (size_t i = 0; i < kStreamIds.size(); ++i)
        {
            if (i > 0)
            {
                out << ",";
            }
            const auto &streamId = kStreamIds[i];
            out << "\"" << streamId << "\":" << fpsLatest_.at(streamId);
        }
        out << "},\"error\":";
        if (lastError_)
        {
            out << "\"" << jsonEscape(*lastError_) << "\"";
        }
        else
        {
            out << "null";
        }
        out << "}";
        return out.str();
    }

  private:
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

    bool startWorkerLocked(std::string &)
    {
        std::lock_guard<std::mutex> lock(stateMutex_);
        if (worker_.joinable())
        {
            return true;
        }
        stopRequested_.store(false);
        pendingPreset_ = preset_;
        lastError_.reset();
        running_ = true;
        fpsWindowStart_ = std::chrono::steady_clock::now();
        for (const auto &streamId : kStreamIds)
        {
            fpsCounts_[streamId] = 0;
            fpsLatest_[streamId] = 0.0;
        }
        worker_ = std::thread(&RealSenseBridge::run, this);
        return true;
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
                lastError_.reset();
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

    void markStopped(std::optional<std::string> error = std::nullopt)
    {
        std::lock_guard<std::mutex> lock(stateMutex_);
        running_ = false;
        lastError_ = std::move(error);
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
            return;
        }

        try
        {
            if (depthSensor->supports(RS2_OPTION_VISUAL_PRESET))
            {
                depthSensor->set_option(RS2_OPTION_VISUAL_PRESET, static_cast<float>(it->second));
            }
        }
        catch (const rs2::error &e)
        {
            std::fprintf(stderr, "Failed to apply RealSense preset %s: %s\n", preset->c_str(),
                         e.what());
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

        try
        {
            rs2::context context;
            if (context.query_devices().size() == 0)
            {
                std::string message = "No RealSense device connected or available";
                std::fprintf(stderr, "%s\n", message.c_str());
                markStopped(message);
                return;
            }
        }
        catch (const rs2::error &e)
        {
            std::string message = std::string("Failed to query RealSense devices: ") + e.what();
            std::fprintf(stderr, "%s\n", message.c_str());
            markStopped(message);
            return;
        }

        std::optional<rs2::pipeline_profile> profile;
        std::string startError;
        for (int attempt = 1; attempt <= 3 && !stopRequested_.load(); ++attempt)
        {
            try
            {
                profile.emplace(pipeline.start(config));
                break;
            }
            catch (const rs2::error &e)
            {
                startError = e.what();
                std::fprintf(stderr,
                             "pipeline.start attempt %d/3 failed at %dx%d @%dfps: %s\n", attempt,
                             width, height, fps, e.what());
                std::this_thread::sleep_for(std::chrono::milliseconds(500));
            }
        }

        if (!profile)
        {
            if (stopRequested_.load())
            {
                markStopped();
                return;
            }
            std::string message = startError.empty()
                                      ? "Failed to start RealSense pipeline"
                                      : "Failed to start RealSense pipeline: " + startError;
            std::fprintf(stderr, "%s\n", message.c_str());
            markStopped(message);
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
        }
        catch (const rs2::error &e)
        {
            std::fprintf(stderr, "Could not inspect RealSense sensors for presets: %s\n", e.what());
        }

        tjhandle encoder = tjInitCompress();
        if (!encoder)
        {
            std::string message = "Failed to initialize TurboJPEG encoder";
            std::fprintf(stderr, "%s\n", message.c_str());
            try
            {
                pipeline.stop();
            }
            catch (const rs2::error &)
            {
            }
            markStopped(message);
            return;
        }

        rs2::colorizer colorizer;
        std::fprintf(stderr, "RealSense pipeline started at %dx%d @%dfps\n", width, height, fps);

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
            std::string message = std::string("RealSense worker error: ") + e.what();
            std::fprintf(stderr, "%s\n", message.c_str());
            markStopped(message);
        }
        catch (const std::exception &e)
        {
            std::string message = std::string("RealSense worker error: ") + e.what();
            std::fprintf(stderr, "%s\n", message.c_str());
            markStopped(message);
        }
        catch (...)
        {
            std::string message = "RealSense worker error: unknown exception";
            std::fprintf(stderr, "%s\n", message.c_str());
            markStopped(message);
        }

        tjDestroy(encoder);
        try
        {
            pipeline.stop();
        }
        catch (const rs2::error &)
        {
        }
        catch (...)
        {
        }

        if (!stopRequested_.load())
        {
            markStopped();
        }
        std::fprintf(stderr, "RealSense pipeline stopped\n");
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
    std::optional<std::string> lastError_;

    std::unordered_map<std::string, EncodedFrame> latest_;
    std::unordered_map<std::string, int> fpsCounts_;
    std::unordered_map<std::string, double> fpsLatest_;
    std::chrono::steady_clock::time_point fpsWindowStart_ = std::chrono::steady_clock::now();
};

RealSenseBridge *asBridge(RealSenseBridgeRef ref)
{
    return static_cast<RealSenseBridge *>(ref);
}
} // namespace

extern "C" RealSenseBridgeRef RealSenseBridgeCreate(void)
{
    return new RealSenseBridge();
}

extern "C" void RealSenseBridgeDestroy(RealSenseBridgeRef bridge)
{
    delete asBridge(bridge);
}

extern "C" bool RealSenseBridgeStart(RealSenseBridgeRef bridge, char *error, size_t errorLength)
{
    if (!bridge)
    {
        setError(error, errorLength, "RealSense bridge is not initialized");
        return false;
    }
    std::string message;
    bool ok = asBridge(bridge)->start(message);
    if (!ok)
    {
        setError(error, errorLength, message);
    }
    return ok;
}

extern "C" void RealSenseBridgeStop(RealSenseBridgeRef bridge)
{
    if (bridge)
    {
        asBridge(bridge)->stop();
    }
}

extern "C" bool RealSenseBridgeIsRunning(RealSenseBridgeRef bridge)
{
    return bridge && asBridge(bridge)->running();
}

extern "C" bool RealSenseBridgeConfigure(RealSenseBridgeRef bridge, int width, int height, int fps,
                                          const char *preset, char *error, size_t errorLength)
{
    if (!bridge)
    {
        setError(error, errorLength, "RealSense bridge is not initialized");
        return false;
    }
    std::string message;
    bool ok = asBridge(bridge)->configure(width, height, fps, preset ? preset : "default", message);
    if (!ok)
    {
        setError(error, errorLength, message);
    }
    return ok;
}

extern "C" char *RealSenseBridgeHealthJSON(RealSenseBridgeRef bridge)
{
    if (!bridge)
    {
        return copyString("{\"streams\":[\"color\",\"ir-left\",\"ir-right\",\"depth\"],\"running\":false,\"fps\":{\"color\":0,\"ir-left\":0,\"ir-right\":0,\"depth\":0},\"error\":null}");
    }
    return copyString(asBridge(bridge)->healthJson());
}

extern "C" void RealSenseBridgeFreeString(char *string)
{
    std::free(string);
}

extern "C" bool RealSenseBridgeIsKnownStream(const char *streamID)
{
    return streamID && isKnownStream(streamID);
}

extern "C" bool RealSenseBridgeWaitFrame(RealSenseBridgeRef bridge, const char *streamID,
                                          uint64_t lastSequence, int timeoutMilliseconds,
                                          RealSenseBridgeFrame *frame)
{
    if (!bridge || !streamID || !frame || !isKnownStream(streamID))
    {
        return false;
    }

    auto snapshot = asBridge(bridge)->waitForFrame(
        streamID, lastSequence, std::chrono::milliseconds(timeoutMilliseconds));
    if (!snapshot || snapshot->jpeg.empty())
    {
        return false;
    }

    auto *data = static_cast<uint8_t *>(std::malloc(snapshot->jpeg.size()));
    if (!data)
    {
        return false;
    }
    std::memcpy(data, snapshot->jpeg.data(), snapshot->jpeg.size());
    frame->data = data;
    frame->length = snapshot->jpeg.size();
    frame->sequence = snapshot->sequence;
    return true;
}

extern "C" void RealSenseBridgeFreeFrame(RealSenseBridgeFrame *frame)
{
    if (!frame)
    {
        return;
    }
    std::free(frame->data);
    frame->data = nullptr;
    frame->length = 0;
    frame->sequence = 0;
}
