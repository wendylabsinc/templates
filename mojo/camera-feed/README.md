# camera-feed (Mojo)

Live webcam streaming on WendyOS in pure Mojo: `wendycam` captures MJPEG
straight from V4L2 (libc FFI — `openat`/`ioctl`/`mmap`/`poll`, no GStreamer),
and `wendynet` fans the JPEG frames out to browsers over hand-rolled
WebSockets. Same endpoints and client protocol as the `python/camera-feed`
(GStreamer) sibling: `/` UI, `/cameras`, `/logs`, `/debug`, and the
`/stream` WebSocket with `{"switch_camera": "<id>"}` commands.

Cameras output MJPEG natively, so there is no encode step anywhere — a frame
goes DQBUF → WebSocket. The whole app is a single-threaded `poll(2)` loop; the
camera starts on the first client and stops when the last one leaves, and a
vanished camera (USB unplug) is retried once a second while clients wait.

## Notes

- Requires a camera with MJPEG output (every USB UVC webcam; the template
  rejects YUYV-only devices rather than shipping an encoder). CSI cameras
  need the GStreamer-based `python` sibling for now.
- The V4L2 struct layouts wendycam hand-packs are conformance-tested against
  the kernel headers (`common/mojo/wendycam/tests/run_tests.sh`) — Mojo 1.0
  FFI has no C struct interop (docs/mojo-max-port-findings.md, MMF-011 class).
- Frame fan-out uses blocking sends: one pathologically slow client can stall
  the stream for others. Fine for LAN dashboards; known v1 limitation.
- ~110 MB final image: AOT binary + the Mojo runtime `.so` set (MMF-009), no
  Python and no SDK at runtime.

## Verified

Jetson Orin Nano (WendyOS 0.18.2, JetPack 7.2), Logitech Brio 101, MJPEG
640x480–1280x720 — see Appendix B of `docs/mojo-max-port-findings.md`.
