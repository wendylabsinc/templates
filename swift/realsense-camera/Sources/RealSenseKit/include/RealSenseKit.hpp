#pragma once

#include <cstdint>

// Thin noexcept shim over librealsense. Every function catches rs2::error /
// std::exception internally: C++ exceptions must never propagate into Swift
// (Swift cannot catch them; the process would abort).
namespace rsk {

enum class PixelFormat : int {
    bgr8 = 0,
    y8 = 1,
    rgb8 = 2,
};

// A borrowed view into librealsense-owned pixel memory. Valid only until the
// next cameraWaitForFrames / cameraStop / cameraDestroy call on the same
// camera. The capture loop encodes to JPEG before waiting again, so views
// never outlive their frameset.
struct FrameView {
    const uint8_t *data = nullptr;
    int width = 0;
    int height = 0;
    PixelFormat format = PixelFormat::bgr8;

    bool isValid() const { return data != nullptr; }
};

struct FrameBatch {
    bool ok = false;
    FrameView color;    // BGR8
    FrameView irLeft;   // Y8
    FrameView irRight;  // Y8
    FrameView depth;    // colorized RGB8
};

class Camera;

Camera *cameraCreate() noexcept;
void cameraDestroy(Camera *camera) noexcept;

// Starts color + depth + 2x IR at the given mode. Retries pipeline.start 3x
// with 500ms backoff. Returns false if the pipeline could not be started
// (no device, unsupported mode, USB error).
bool cameraStart(Camera *camera, int width, int height, int fps) noexcept;
void cameraStop(Camera *camera) noexcept;

// Applies a D400 visual preset by name ("default", "hand", "high-accuracy",
// "high-density", "medium-density"). Returns false for unknown names or when
// no sensor supports presets.
bool cameraApplyPreset(Camera *camera, const char *name) noexcept;

// Blocks up to timeoutMs for the next frameset. batch.ok == false on timeout
// or device error; the caller decides whether to keep polling.
FrameBatch cameraWaitForFrames(Camera *camera, int timeoutMs) noexcept;

} // namespace rsk
