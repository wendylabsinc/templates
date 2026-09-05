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
  --language python
cd my-api
wendy run
```

Use `wendy init --help` for target, language, entitlement, and variable options.
Each generated project contains its own README with requirements, run commands,
configuration, architecture, and extension points.

## Choose a starting point

Start with **API**, **Web app**, **Camera**, or **Audio**. Each starter has a
working result and a first edit in its README. Wendy Lite offers **Hello World**
and **Blink**. The examples contain larger integrations for specific hardware.

`fullstack` is now a minimal Web app. The previous React dashboard, including
camera, audio, GPU, and SQLite pages, is available as `device-dashboard`.
Existing generated projects are unaffected.

<!-- catalog:start -->

### Starters

| Project | Languages | What you get |
|---|---|---|
| API (`simple-api`) | [Python](python/simple-api/), [Swift](swift/simple-api/), [Rust](rust/simple-api/), [Node (TypeScript)](node/simple-api/), [C++](cpp/simple-api/), [Mojo](mojo/simple-api/) | A small JSON API; no special hardware. Target: wendyos. |
| Web app (`fullstack`) | [Python](python/fullstack/), [Swift](swift/fullstack/), [Rust](rust/fullstack/), [Node (TypeScript)](node/fullstack/), [C++](cpp/fullstack/), [Mojo](mojo/fullstack/) | One React page calling one backend endpoint. Target: wendyos. |
| Camera (`camera-feed`) | [Python](python/camera-feed/), [Swift](swift/camera-feed/), [Rust](rust/camera-feed/), [Node (TypeScript)](node/camera-feed/), [C++](cpp/camera-feed/), [Mojo](mojo/camera-feed/) | View a live camera in your browser. Target: wendyos. Requires: A supported camera. |
| Audio (`audio`) | [Python](python/audio/), [Swift](swift/audio/), [Rust](rust/audio/), [Node (TypeScript)](node/audio/), [C++](cpp/audio/), [Mojo](mojo/audio/) | See a live microphone waveform. Target: wendyos. Requires: Audio capture and playback devices. |
| Hello World (`hello-world`) | [Swift](swift/hello-world/) | Say 'Hello, World'. Target: wendy-lite. |
| Blink (`blink-led`) | [Swift](swift/blink-led/) | Blink the built-in LED on an ESP32 using GPIO. Target: wendy-lite. |

### Examples

| Project | Languages | What you get |
|---|---|---|
| Voice ai pipecat (`voice-ai-pipecat`) | [Python](python/voice-ai-pipecat/) | Voice AI assistant: Pipecat + Gemini 2.5 Flash + local faster-whisper STT + Piper TTS. Target: wendyos. Requires: Audio devices, network access, and a Google API key. |
| Llm (`llm`) | [Python](python/llm/), [Mojo](mojo/llm/) | Local LLM chat app: Ollama + Open WebUI on WendyOS. Target: wendyos. Requires: Disk and memory for the selected model. |
| Mac llm (`mac-llm`) | [Swift](swift/mac-llm/) | Native macOS MLX LLM chat app with Open WebUI. Target: darwin. Requires: Apple Silicon and disk space for the selected model. |
| Camera feed yolo (`camera-feed-yolo`) | [Python](python/camera-feed-yolo/), [Swift](swift/camera-feed-yolo/), [Rust](rust/camera-feed-yolo/), [Node (TypeScript)](node/camera-feed-yolo/), [C++](cpp/camera-feed-yolo/), [Mojo](mojo/camera-feed-yolo/) | Live camera feed with YOLOv8 COCO object detection (generic CPU or Jetson GPU). Target: wendyos. Requires: A camera; a Jetson GPU is optional. |
| Ip camera feed (`ip-camera-feed`) | [Python](python/ip-camera-feed/) | Live feed from a platform-registered IP camera: GStreamer MJPEG over WebSocket via the /dev/video2xx loopback node. Target: wendyos. Requires: A registered IP camera and V4L2 loopback support. |
| Realsense camera (`realsense-camera`) | [Python](python/realsense-camera/), [C++](cpp/realsense-camera/) | Live RealSense D415 multi-stream viewer: color + 2x IR + depth as MJPEG. Target: wendyos. Requires: An Intel RealSense D415. |
| Go2 rc (`go2-rc`) | [Python](python/go2-rc/) | Remote-control the Unitree Go2 EDU from a browser: a 3-service app group (motion API + camera stream + teleop web UI). Target: wendyos. Requires: A Unitree Go2 EDU and robot network. |
| G1 rc (`g1-rc`) | [Python](python/g1-rc/) | Remote-control the Unitree G1 humanoid from a browser: a 3-service app group (motion API + camera stream + teleop web UI) with posture, gesture, and arm-preset controls. Target: wendyos. Requires: A Unitree G1 and supported camera. |
| Go2 initial test (`go2-initial-test`) | [Python](python/go2-initial-test/) | Pre-hackathon hardware self-test for the Unitree Go2: one tiny app per hardware interface (camera, LiDAR, mic, speaker, IMU, foot contact, battery, motion, GPU, Bluetooth, cloud) plus a dashboard UI showing a live pass/fail go/no-go board. Target: wendyos. Requires: A Unitree Go2 EDU; large multi-service deployment. |
| Go2 foxglove (`go2-foxglove`) | [Python](python/go2-foxglove/) | Stream a Unitree Go2 into Foxglove: LiDAR point cloud, pose+TF, body state and UWB over one WebSocket, plus the front camera (WebRTC). Ships a ready-made Foxglove layout.. Target: wendyos. Requires: A Unitree Go2 EDU and Foxglove. |
| Rc car (`rc-car`) | [Python](python/rc-car/) | Remote-control a Yahboom ROSMASTER R2 (Ackerman) robot car from a browser: a 3-service app group (Rosmaster motion + UVC camera + teleop web UI). Target: wendyos. Requires: A Yahboom ROSMASTER R2, serial controller, and camera. |
| Go2 rosbag (`go2-rosbag`) | [Python](python/go2-rosbag/) | Record a Unitree Go2's DDS topics to an mcap rosbag from a browser: discovers every topic the robot exposes and records all of them (opens directly in Foxglove / plays with ros2 bag). Target: wendyos. Requires: A Unitree Go2 EDU and persistent storage. |
| Ros2 talker listener (`ros2-talker-listener`) | [Swift](swift/ros2-talker-listener/), [Mojo](mojo/ros2-talker-listener/) | ROS 2 talker/listener demo (over CycloneDDS): std_msgs/String publisher + subscriber as a two-service app group. Target: wendyos. Requires: ROS 2-compatible networking. |
| Gpu hello (`gpu-hello`) | [Mojo](mojo/gpu-hello/) | Pure-Mojo GPU diagnostics: verified vector-add + matmul kernels on the device GPU (Jetson sm_87/sm_110), report served over HTTP. Target: wendyos. Requires: A supported Jetson GPU. |
| Device dashboard (`device-dashboard`) | [Python](python/device-dashboard/), [Swift](swift/device-dashboard/), [Rust](rust/device-dashboard/), [Node (TypeScript)](node/device-dashboard/), [C++](cpp/device-dashboard/), [Mojo](mojo/device-dashboard/) | Camera, audio, GPU, and persistent SQLite demos in a React dashboard. Target: wendyos. Requires: Matching hardware for the device pages. |

<!-- catalog:end -->

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for template structure, rendering rules,
shared-source maintenance, tests, and hosted-source deployment.

The `common/` directory contains maintainer sources used by several templates;
it is not selectable through `wendy init`.

## Acknowledgments

The audio templates include sample WAV files from
[pdx-cs-sound/wavs](https://github.com/pdx-cs-sound/wavs).
