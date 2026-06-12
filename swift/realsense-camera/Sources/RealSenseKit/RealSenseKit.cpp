#include "RealSenseKit.hpp"

#include <librealsense2/rs.hpp>

#include <chrono>
#include <cstdio>
#include <cstring>
#include <optional>
#include <thread>

namespace rsk {

class Camera {
  public:
    rs2::pipeline pipeline;
    std::optional<rs2::pipeline_profile> profile;
    std::optional<rs2::sensor> presetSensor;
    rs2::colorizer colorizer;
    // Keep the last frameset and colorized depth frame alive so the
    // FrameViews handed to Swift stay valid until the next wait call.
    rs2::frameset lastFrames;
    rs2::frame lastDepth;
    bool started = false;
};

namespace {

struct PresetEntry {
    const char *name;
    rs2_rs400_visual_preset value;
};

constexpr PresetEntry kPresets[] = {
    {"default", RS2_RS400_VISUAL_PRESET_DEFAULT},
    {"hand", RS2_RS400_VISUAL_PRESET_HAND},
    {"high-accuracy", RS2_RS400_VISUAL_PRESET_HIGH_ACCURACY},
    {"high-density", RS2_RS400_VISUAL_PRESET_HIGH_DENSITY},
    {"medium-density", RS2_RS400_VISUAL_PRESET_MEDIUM_DENSITY},
};

FrameView makeView(const rs2::video_frame &frame, PixelFormat format)
{
    FrameView view;
    view.data = static_cast<const uint8_t *>(frame.get_data());
    view.width = frame.get_width();
    view.height = frame.get_height();
    view.format = format;
    return view;
}

} // namespace

Camera *cameraCreate() noexcept
{
    try
    {
        return new Camera();
    }
    catch (...)
    {
        return nullptr;
    }
}

void cameraDestroy(Camera *camera) noexcept
{
    if (!camera)
    {
        return;
    }
    cameraStop(camera);
    delete camera;
}

bool cameraStart(Camera *camera, int width, int height, int fps) noexcept
{
    if (!camera)
    {
        return false;
    }
    if (camera->started)
    {
        return true;
    }

    try
    {
        rs2::config config;
        config.enable_stream(RS2_STREAM_COLOR, width, height, RS2_FORMAT_BGR8, fps);
        config.enable_stream(RS2_STREAM_DEPTH, width, height, RS2_FORMAT_Z16, fps);
        config.enable_stream(RS2_STREAM_INFRARED, 1, width, height, RS2_FORMAT_Y8, fps);
        config.enable_stream(RS2_STREAM_INFRARED, 2, width, height, RS2_FORMAT_Y8, fps);

        bool startedPipeline = false;
        for (int attempt = 1; attempt <= 3; ++attempt)
        {
            try
            {
                camera->profile.emplace(camera->pipeline.start(config));
                startedPipeline = true;
                break;
            }
            catch (const rs2::error &e)
            {
                std::fprintf(stderr,
                             "[RealSenseKit] pipeline.start attempt %d/3 failed at %dx%d@%dfps: %s\n",
                             attempt, width, height, fps, e.what());
                if (attempt < 3)
                {
                    std::this_thread::sleep_for(std::chrono::milliseconds(500));
                }
            }
        }
        if (!startedPipeline)
        {
            return false;
        }

        camera->presetSensor.reset();
        try
        {
            for (auto &&sensor : camera->profile->get_device().query_sensors())
            {
                if (sensor.supports(RS2_OPTION_VISUAL_PRESET))
                {
                    camera->presetSensor.emplace(sensor);
                    break;
                }
            }
        }
        catch (const rs2::error &e)
        {
            std::fprintf(stderr, "[RealSenseKit] could not inspect sensors for presets: %s\n",
                         e.what());
        }

        camera->started = true;
        return true;
    }
    catch (const std::exception &e)
    {
        std::fprintf(stderr, "[RealSenseKit] start failed: %s\n", e.what());
        return false;
    }
    catch (...)
    {
        return false;
    }
}

void cameraStop(Camera *camera) noexcept
{
    if (!camera)
    {
        return;
    }
    if (camera->started)
    {
        try
        {
            camera->pipeline.stop();
        }
        catch (...)
        {
        }
    }
    camera->lastFrames = rs2::frameset();
    camera->lastDepth = rs2::frame();
    camera->profile.reset();
    camera->presetSensor.reset();
    camera->started = false;
}

bool cameraApplyPreset(Camera *camera, const char *name) noexcept
{
    if (!camera || !name || !camera->presetSensor)
    {
        return false;
    }

    const PresetEntry *entry = nullptr;
    for (const auto &candidate : kPresets)
    {
        if (std::strcmp(candidate.name, name) == 0)
        {
            entry = &candidate;
            break;
        }
    }
    if (!entry)
    {
        std::fprintf(stderr, "[RealSenseKit] unknown visual preset: %s\n", name);
        return false;
    }

    try
    {
        camera->presetSensor->set_option(RS2_OPTION_VISUAL_PRESET, static_cast<float>(entry->value));
        return true;
    }
    catch (const rs2::error &e)
    {
        std::fprintf(stderr, "[RealSenseKit] failed to apply preset %s: %s\n", name, e.what());
        return false;
    }
    catch (...)
    {
        return false;
    }
}

FrameBatch cameraWaitForFrames(Camera *camera, int timeoutMs) noexcept
{
    FrameBatch batch;
    if (!camera || !camera->started)
    {
        return batch;
    }

    try
    {
        rs2::frameset frames;
        if (!camera->pipeline.try_wait_for_frames(&frames, static_cast<unsigned int>(timeoutMs)))
        {
            return batch;
        }
        camera->lastFrames = frames;

        if (auto color = frames.get_color_frame())
        {
            batch.color = makeView(color, PixelFormat::bgr8);
        }
        if (auto irLeft = frames.get_infrared_frame(1))
        {
            batch.irLeft = makeView(irLeft, PixelFormat::y8);
        }
        if (auto irRight = frames.get_infrared_frame(2))
        {
            batch.irRight = makeView(irRight, PixelFormat::y8);
        }
        if (auto depth = frames.get_depth_frame())
        {
            camera->lastDepth = camera->colorizer.colorize(depth);
            batch.depth = makeView(camera->lastDepth.as<rs2::video_frame>(), PixelFormat::rgb8);
        }

        batch.ok = true;
        return batch;
    }
    catch (const std::exception &e)
    {
        std::fprintf(stderr, "[RealSenseKit] wait_for_frames error: %s\n", e.what());
        return FrameBatch{};
    }
    catch (...)
    {
        return FrameBatch{};
    }
}

} // namespace rsk
