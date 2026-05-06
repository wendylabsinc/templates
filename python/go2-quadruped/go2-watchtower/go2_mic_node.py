#!/usr/bin/env python3
"""
go2_mic_node.py

Captures audio from the Jetson's local input device, computes RMS + a small
magnitude spectrum each block, and publishes a `std_msgs/Float32MultiArray`
to `/go2/mic/levels`. Replaces the previous capture/relay UDP split now that
the bridge runs on the same host as the mic.

    data[0]      — RMS of the latest 50 ms window (~0..1)
    data[1..32]  — normalised magnitude spectrum, low to high frequency

Set MIC_DEVICE=<int> to pick a non-default input device. List devices with
`python3 -m sounddevice` inside the running container if needed.
"""

import os
import sys

import numpy as np
import rclpy
import sounddevice as sd
from rclpy.node import Node
from std_msgs.msg import Float32MultiArray, MultiArrayDimension

_DEV = os.environ.get("MIC_DEVICE")
DEVICE = int(_DEV) if _DEV and _DEV.lstrip("-").isdigit() else None
SAMPLE_RATE = int(os.environ.get("SAMPLE_RATE", "16000"))
BLOCK_SIZE = int(os.environ.get("BLOCK_SIZE", "800"))  # 50 ms at 16 kHz
N_BINS = int(os.environ.get("N_BINS", "32"))
TOPIC = os.environ.get("MIC_TOPIC", "/go2/mic/levels")


class Go2MicNode(Node):
    def __init__(self):
        super().__init__("go2_mic_node")
        self.pub = self.create_publisher(Float32MultiArray, TOPIC, 10)
        self.get_logger().info(
            f"device={DEVICE} rate={SAMPLE_RATE} block={BLOCK_SIZE} "
            f"bins={N_BINS} -> {TOPIC}"
        )
        self.stream = sd.InputStream(
            device=DEVICE,
            samplerate=SAMPLE_RATE,
            channels=1,
            blocksize=BLOCK_SIZE,
            callback=self._on_audio,
        )
        self.stream.start()

    def _on_audio(self, indata, frames, time_info, status):
        if status:
            print(status, file=sys.stderr)
        mono = indata[:, 0].astype(np.float32)
        rms = float(np.sqrt(np.mean(mono ** 2)))
        spectrum = np.abs(np.fft.rfft(mono))[:N_BINS]
        peak = float(spectrum.max())
        if peak > 0:
            spectrum = spectrum / peak

        msg = Float32MultiArray()
        dim = MultiArrayDimension()
        dim.label = "rms_then_spectrum"
        dim.size = 1 + N_BINS
        dim.stride = 1 + N_BINS
        msg.layout.dim.append(dim)
        msg.data = [rms] + spectrum.tolist()
        self.pub.publish(msg)


def main():
    rclpy.init()
    try:
        node = Go2MicNode()
    except Exception as e:
        # The Jetson may have no usable input device — see GO2_SETUP notes.
        print(f"go2_mic_node: failed to open audio device: {e}", file=sys.stderr)
        print(
            "Set MIC_DEVICE to a valid index, or remove mic_node from app.py "
            "if the Jetson exposes no input.",
            file=sys.stderr,
        )
        rclpy.try_shutdown()
        sys.exit(1)
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, rclpy.executors.ExternalShutdownException):
        pass
    finally:
        try:
            node.stream.stop()
            node.stream.close()
        except Exception:
            pass
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
