#!/usr/bin/env python3
"""
go2_audio_bridge.py

Subscribes to the robot's main-controller audio topics (which carry G.711
mu-law-encoded mono audio at 8 kHz, 20 ms / 160-byte frames) and republishes
human-friendly representations so Foxglove can plot them.

For each direction we publish:
  /go2/audio/<dir>/levels    Float32MultiArray  [rms, peak]
  /go2/audio/<dir>/waveform  Float32MultiArray  decoded float samples in -1..1

Where <dir> is `sender` (robot speaker output) or `receiver` (incoming, usually
the head mic array).

If A-law turns out to be a better fit than mu-law, set AUDIO_CODEC=alaw via env.
"""

import audioop
import os

import numpy as np
import rclpy
from rclpy.node import Node
from std_msgs.msg import Float32MultiArray, MultiArrayDimension

try:
    from unitree_go.msg import AudioData
except ImportError:  # pragma: no cover — caught at runtime
    AudioData = None

CODEC = os.environ.get("AUDIO_CODEC", "ulaw").lower()  # "ulaw" or "alaw"
SAMPLE_RATE = 8000  # G.711 is fixed at 8 kHz


def _decode(payload: bytes) -> np.ndarray:
    """Decode a G.711 frame to float32 samples in [-1, 1]."""
    if CODEC == "alaw":
        pcm16 = audioop.alaw2lin(payload, 2)
    else:
        pcm16 = audioop.ulaw2lin(payload, 2)
    return np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0


class _Stream:
    """One subscription + matching levels/waveform publishers."""

    def __init__(self, node: Node, in_topic: str, out_prefix: str):
        self.node = node
        self.in_topic = in_topic
        self.levels_pub = node.create_publisher(
            Float32MultiArray, f"{out_prefix}/levels", 10
        )
        self.wave_pub = node.create_publisher(
            Float32MultiArray, f"{out_prefix}/waveform", 10
        )
        node.create_subscription(AudioData, in_topic, self._on_msg, 10)

    def _on_msg(self, msg):
        if not msg.data:
            return
        samples = _decode(bytes(msg.data))
        rms = float(np.sqrt(np.mean(samples ** 2)))
        peak = float(np.max(np.abs(samples)))

        levels = Float32MultiArray()
        ldim = MultiArrayDimension()
        ldim.label = "rms_peak"
        ldim.size = 2
        ldim.stride = 2
        levels.layout.dim.append(ldim)
        levels.data = [rms, peak]
        self.levels_pub.publish(levels)

        wave = Float32MultiArray()
        wdim = MultiArrayDimension()
        wdim.label = "samples"
        wdim.size = len(samples)
        wdim.stride = len(samples)
        wave.layout.dim.append(wdim)
        wave.data = samples.tolist()
        self.wave_pub.publish(wave)


class Go2AudioBridge(Node):
    def __init__(self):
        super().__init__("go2_audio_bridge")
        if AudioData is None:
            raise RuntimeError(
                "unitree_go.msg.AudioData not importable — is the unitree_ros2 "
                "workspace sourced?"
            )
        self.get_logger().info(
            f"Decoding {CODEC} @ {SAMPLE_RATE} Hz; "
            "publishing /go2/audio/{sender,receiver}/{levels,waveform}"
        )
        self._streams = [
            _Stream(self, "/audiosender", "/go2/audio/sender"),
            _Stream(self, "/audioreceiver", "/go2/audio/receiver"),
        ]


def main():
    rclpy.init()
    try:
        node = Go2AudioBridge()
    except Exception as e:
        print(f"go2_audio_bridge: init failed: {e}")
        rclpy.try_shutdown()
        raise
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, rclpy.executors.ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
