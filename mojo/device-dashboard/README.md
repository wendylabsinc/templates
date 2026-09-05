# fullstack (Mojo)

The fullstack template's FastAPI backend rebuilt in pure Mojo, serving the
same React/shadcn frontend byte-identical to the python sibling. Everything
the sibling does through frameworks goes through FFI here:

- **Cars CRUD** (`/api/cars`) — SQLite via `wendydb` (`libsqlite3.so.0`
  dlopen; no ORM, no linked dependency), persisted on the `/data` volume.
- **Camera** (`/api/cameras`, `/api/camera/stream`) — `wendycam` V4L2 MJPEG
  capture fanned out over `wendynet` WebSockets, no GStreamer.
- **Audio** (`/api/microphones`, `/api/speakers`, `/api/audio/stream`) —
  `wendyaudio` ALSA capture (S16LE mono 16 kHz), no GStreamer.
- **System** (`/api/system`) — `/proc` parsing plus `uname(2)`/`statvfs(2)`.
- **GPU** (`/api/gpu`) — where the python sibling shells out to `nvidia-smi`,
  a Jetson build runs a real Mojo matmul kernel through MAX's
  `DeviceContext` (AOT cross-compiled, gpu-hello style) and reports the
  device name and measured GFLOPS; CPU builds fall back to thermal-zone
  info. The probe runs once on first request and is cached.
- Plus `/api/logs` and `/api/debug` for headless inspection.

The whole backend is a single-threaded `poll(2)` loop; camera and microphone
open on the first WebSocket client and close with the last one, reopening
with retry on unplug. The final image is slim debian: AOT binary + Mojo
runtime `.so` set (MMF-009) + `libasound2`/`libsqlite3-0` — no Python, no
Modular SDK at runtime.

## Notes

- Backend logic lives in `main.mojo` (routing + loop), `carstore.mojo`
  (CRUD), `sysinfo.mojo` (/api/system), `gpudiag.mojo` (/api/gpu).
- `tests/run_tests.sh` exercises the CRUD JSON shapes, the /api/system
  parsers, and the GPU thermal fallback against the vendored packages in a
  local MAX container (no hardware needed).
- Camera needs MJPEG-capable USB UVC hardware, as in `mojo/camera-feed`.
