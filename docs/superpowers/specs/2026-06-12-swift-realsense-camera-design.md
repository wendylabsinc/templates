# Swift `realsense-camera` Template — Design

**Date:** 2026-06-12
**Status:** Approved
**Goal:** Add `swift/realsense-camera`, a Swift port of the existing C++/Python RealSense D415 multi-stream viewer templates, using Swift C++ interop against librealsense's `rs.hpp` API.

## Context

The `realsense-camera` template (live D415 viewer: color + left IR + right IR + colorized depth as MJPEG) exists for Python (`FastAPI + pyrealsense2`) and C++ (`Drogon + librealsense + TurboJPEG`). The shared React viewer frontend lives at `common/realsense-camera-frontend/` and is vendored into each language directory. Swift is missing.

Key decisions (made with the user):

- **librealsense binding:** Swift C++ interop (`.interoperabilityMode(.Cxx)`) against `rs.hpp`, not the C API.
- **JPEG encoding:** TurboJPEG (`libturbojpeg`) via C interop from Swift, matching the C++ template's encoder.

## Constraint that shapes the architecture

Swift cannot catch C++ exceptions; an `rs2::error` propagating into Swift aborts the process. librealsense throws routinely (failed `pipeline.start`, device unplug, timeout). Therefore all throwing librealsense calls live inside a thin C++ shim target whose public surface is `noexcept`, returning status codes / empty results. Swift imports the shim's C++ types directly via interop.

## Package layout

```
swift/realsense-camera/
├── Package.swift                  # Hummingbird 2 + swift-otel; app target uses .interoperabilityMode(.Cxx)
├── Sources/
│   ├── RealSenseKit/              # C++ target, links realsense2 (pkg-config)
│   │   ├── include/RealSenseKit.hpp
│   │   └── RealSenseKit.cpp       # noexcept shim: pipeline lifecycle (start retried 3x),
│   │                              #   frameset → 4 frame views, depth colorizer, visual presets
│   ├── CTurboJPEG/                # systemLibrary target, pkgConfig "libturbojpeg"
│   │   └── module.modulemap
│   └── {{.APP_ID}}/               # Swift app target (C++ interop enabled)
│       ├── App.swift              # router, endpoints, OTel bootstrap, ServiceGroup
│       ├── RealSensePump.swift    # dedicated Thread: wait_for_frames → TurboJPEG encode → publish
│       ├── FrameStore.swift       # actor: latest jpeg + sequence per stream, fps window, waiters
│       └── StaticFileMiddleware.swift  # reused from swift/camera-feed
├── src/, public/, index.html, package.json, vite.config.ts, …  # vendored common/realsense-camera-frontend
├── Dockerfile
├── wendy.json
└── template.json
```

## HTTP contract

Identical to the C++/Python backends so the vendored frontend needs zero changes:

- `POST /start` → `{"running": bool}`
- `POST /stop` → `{"running": bool}`
- `POST /config?width&height&fps&preset` — validates ranges (width/height 1–8192, fps 1–300, preset one of `default|hand|high-accuracy|high-density|medium-density`); restarts the pipeline if size/fps changed while running; applies preset live. 400 with `{"error": …}` on bad input.
- `GET /health` → `{"streams": [...], "running": bool, "fps": {stream: float}}`
- `GET /stream/{color|ir-left|ir-right|depth}` → `multipart/x-mixed-replace; boundary=frame`, `Cache-Control: no-store`; 404 JSON error for unknown stream ids.
- `GET /` and SPA fallback serve the built frontend from the container's static directory.

## Concurrency model

- `wait_for_frames` blocks, so the capture loop runs on a dedicated `Thread` (never the cooperative pool). Per iteration: poll frameset via shim → encode color (BGR, 4:2:0), IR left/right (grayscale), colorized depth (RGB, 4:2:0) with TurboJPEG → publish the batch to `FrameStore`.
- `FrameStore` is an actor holding `latest[stream] = (jpeg, sequence)`, a 1-second fps window per stream, and continuation-based waiters. Handlers call `waitForFrame(stream:after:timeout: 5s)`.
- A small lifecycle actor owns pump start/stop/reconfigure (restart on resolution/fps change), mirroring the C++ `RealSensePump` semantics.
- MJPEG handlers loop: await next frame → write multipart part through Hummingbird's response body writer; the stream ends on timeout with the pump stopped, or on client disconnect.

## Error handling

- All `rs2::error` paths terminate inside `RealSenseKit.cpp`; the shim logs and returns failure values.
- `pipeline.start` retried 3× with 500 ms backoff (parity with C++).
- Frame-wait timeouts inside the loop are non-fatal (continue).
- JPEG encode failure for one stream skips that stream for that frame batch.

## Build & deploy

- **Dockerfile** (3 stages):
  1. `node:22-alpine` — build the vendored frontend (`npm install && npm run build`).
  2. `swift:{{.SWIFT_VERSION}}-bookworm` — build librealsense `v2.55.1` from source (`FORCE_RSUSB_BACKEND=ON`, same flags as cpp template), apt `libturbojpeg0-dev libusb-1.0-0-dev`, then `swift build -c release`.
  3. `swift:{{.SWIFT_VERSION}}-bookworm-slim` — runtime: `libusb-1.0-0 libturbojpeg ca-certificates`, librealsense `.so` copied from stage 2 + `ldconfig`, app binary, frontend `dist/` as static dir.
- **wendy.json:** entitlements `usb` + `network` (host); TCP readiness on `{{.PORT}}` with 60 s timeout; postStart `wendy utils open-browser`.
- **template.json:** `APP_ID` (required), `PORT` default **6007**, `SWIFT_VERSION` default **6.3**.
- **meta.json:** add `"swift"` to the `realsense-camera` entry's `languages`.
- **README.md:** add the Swift row to the realsense-camera table.
- Local macOS dev: `brew install librealsense jpeg-turbo`; platforms `.macOS("26.0")` as in `swift/camera-feed`.

## Testing

- `swift build` locally (macOS) for compile validation of interop + shim.
- `docker build` / `wendy build` for the full Linux path.
- Live stream verification requires a physical D415 on a WendyOS device — manual step, flagged at handoff.
