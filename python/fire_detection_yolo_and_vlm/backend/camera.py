"""Single-frame camera capture for the AI agent, using GStreamer."""

import base64
import platform
import subprocess

from cameras import _name_to_index, _discover_macos


class Camera:
    def __init__(self, device: str = "0"):
        self.device = device

    def capture_jpeg_base64(self) -> str | None:
        """Capture a single JPEG frame and return as base64."""
        # Use detector's latest frame if available (avoids opening a second camera stream)
        try:
            import detector
            frame = detector.get_latest_frame_bytes()
            if frame:
                return base64.b64encode(frame).decode("ascii")
        except Exception:
            pass

        if platform.system() == "Darwin":
            idx = _name_to_index.get(self.device)
            if idx is None:
                _discover_macos()
                idx = _name_to_index.get(self.device, 0)
            cmd = (
                f"gst-launch-1.0 -q "
                f"avfvideosrc device-index={idx} num-buffers=1 ! "
                f"video/x-raw,framerate=30/1 ! "
                f"videoconvert ! "
                f"jpegenc quality=80 ! "
                f"fdsink"
            )
        else:
            cmd = (
                f"gst-launch-1.0 -q "
                f"v4l2src device=/dev/video{self.device} num-buffers=1 ! "
                f"video/x-raw,framerate=30/1 ! "
                f"videoconvert ! "
                f"jpegenc quality=80 ! "
                f"fdsink"
            )

        try:
            result = subprocess.run(cmd, capture_output=True, timeout=10, shell=True)
            if result.returncode == 0 and result.stdout:
                return base64.b64encode(result.stdout).decode("ascii")
        except Exception:
            pass
        return None
