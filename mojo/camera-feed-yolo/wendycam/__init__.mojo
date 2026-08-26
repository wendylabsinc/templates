# wendycam: pure-Mojo V4L2 webcam capture for the Mojo templates.
# Talks to /dev/video* through libc FFI (open/ioctl/mmap/poll) — no GStreamer.
# ABI constants are conformance-tested against the kernel headers in
# tests/run_tests.sh; capture is verified on hardware (Jetson + UVC cameras).
from .v4l2 import fourcc, V4L2_PIX_FMT_MJPEG, V4L2_PIX_FMT_YUYV
from .camera import Camera, CameraInfo, list_cameras
