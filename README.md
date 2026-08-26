<p align="center">
  <img src="docs/media/demo.gif" alt="Creating a Wendy project from a template" width="360">
</p>

# Wendy templates

This repository contains project templates for the Wendy CLI. Use a template to
create a runnable WendyOS, Wendy Lite, or Wendy Agent for Mac project.

## Create a project

Run the interactive project wizard:

```sh
wendy init
```

Or choose the project settings on the command line:

```sh
wendy init \
  --app-id my-api \
  --template simple-api \
  --language python \
  --var PORT=8080
cd my-api
wendy run
```

Use `wendy init --help` for target, language, entitlement, and variable options.
Each generated project contains its own README with requirements, run commands,
configuration, architecture, and extension points.

## Template catalog

Unless a row says otherwise, the target is WendyOS. Camera, audio, USB, GPU,
robot, and model requirements are summarized here to help with selection; the
generated project README contains the setup details.

| Template | Languages | Target or main requirement | Purpose |
|---|---|---|---|
| `simple-api` | [C++](cpp/simple-api/), [Node](node/simple-api/), [Python](python/simple-api/), [Rust](rust/simple-api/), [Swift](swift/simple-api/) | WendyOS; no special hardware | Minimal JSON API with health and item endpoints |
| `fullstack` | [C++](cpp/fullstack/), [Node](node/fullstack/), [Python](python/fullstack/), [Rust](rust/fullstack/), [Swift](swift/fullstack/) | WendyOS; device features need matching hardware | React dashboard, CRUD API, persistent SQLite data, and device pages |
| `camera-feed` | [C++](cpp/camera-feed/), [Node](node/camera-feed/), [Python](python/camera-feed/), [Rust](rust/camera-feed/), [Swift](swift/camera-feed/) | WendyOS and a camera | Browser camera viewer using GStreamer and WebSocket MJPEG |
| `audio` | [C++](cpp/audio/), [Node](node/audio/), [Python](python/audio/), [Rust](rust/audio/), [Swift](swift/audio/) | WendyOS and capture/playback devices | Live microphone waveform, device selection, and sample playback |
| `camera-feed-yolo` | [C++](cpp/camera-feed-yolo/), [Node](node/camera-feed-yolo/), [Python](python/camera-feed-yolo/), [Rust](rust/camera-feed-yolo/), [Swift](swift/camera-feed-yolo/) | WendyOS and a camera; optional NVIDIA Jetson GPU | Live camera viewer with YOLOv8 object detection |
| `realsense-camera` | [C++](cpp/realsense-camera/), [Python](python/realsense-camera/) | WendyOS and Intel RealSense D415 | Color, infrared, and depth stream viewer |
| `ip-camera-feed` | [Python](python/ip-camera-feed/) | WendyOS with a registered IP camera and loopback support | View a platform-managed IP camera through its V4L2 node |
| `voice-ai-pipecat` | [Python](python/voice-ai-pipecat/) | WendyOS, audio devices, network, and an AI provider key | Wake-word voice assistant with local speech processing and cloud LLMs |
| `llm` | [Python](python/llm/) | WendyOS with enough disk and memory for the selected model | Ollama and Open WebUI multi-service chat app |
| `mac-llm` | [Swift](swift/mac-llm/) | Wendy Agent for Mac on Apple Silicon | Native MLX model backend with Open WebUI |
| `ros2-talker-listener` | [Swift](swift/ros2-talker-listener/) | WendyOS and ROS 2-compatible networking | Swift ROS 2 publisher and subscriber over CycloneDDS |
| `go2-rc` | [Python](python/go2-rc/) | Unitree Go2 EDU | Browser teleoperation with motion and camera services |
| `g1-rc` | [Python](python/g1-rc/) | Unitree G1 with supported camera and robot network | Browser teleoperation, posture, gestures, and arm presets |
| `rc-car` | [Python](python/rc-car/) | Yahboom ROSMASTER R2, serial controller, camera, and optional joystick | Browser and gamepad control for an Ackermann robot car |
| `go2-foxglove` | [Python](python/go2-foxglove/) | Unitree Go2 EDU and Foxglove | Stream Go2 DDS and camera data to one Foxglove connection |
| `go2-rosbag` | [Python](python/go2-rosbag/) | Unitree Go2 EDU and persistent storage | Inspect Go2 topics and record MCAP rosbag files in a browser |
| `go2-initial-test` | [Python](python/go2-initial-test/) | Unitree Go2 EDU; large multi-service deployment | Dashboard for checking robot interfaces before development |
| `blink-led` | [Swift](swift/blink-led/) | Wendy Lite | Blink the board LED through GPIO |
| `hello-world` | [Swift](swift/hello-world/) | Wendy Lite | Print one message from a minimal embedded Swift app |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for template structure, rendering rules,
shared-source maintenance, tests, and hosted-source deployment.

The `common/` directory contains maintainer sources used by several templates;
it is not selectable through `wendy init`.

## Acknowledgments

The audio templates include sample WAV files from
[pdx-cs-sound/wavs](https://github.com/pdx-cs-sound/wavs).
