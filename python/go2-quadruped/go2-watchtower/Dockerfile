FROM ros:humble-ros-base-jammy

ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        curl \
        python3-pip \
        python3-colcon-common-extensions \
        ros-humble-foxglove-bridge \
        ros-humble-rmw-cyclonedds-cpp \
        ros-humble-rosidl-generator-dds-idl \
        ros-humble-vision-msgs \
        libportaudio2 \
        portaudio19-dev \
        libsndfile1 \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Build the unitree_ros2 workspace so foxglove_bridge can deserialize the
# robot's DDS topics (lidar, lowstate, sportmodestate, uwbstate, etc.).
WORKDIR /opt
RUN git clone https://github.com/unitreerobotics/unitree_ros2 \
    && cd unitree_ros2/cyclonedds_ws/src \
    && git clone https://github.com/ros2/rmw_cyclonedds -b humble \
    && git clone https://github.com/eclipse-cyclonedds/cyclonedds -b releases/0.10.x \
    && cd /opt/unitree_ros2/cyclonedds_ws \
    && bash -lc "source /opt/ros/humble/setup.bash && colcon build --packages-select cyclonedds" \
    && bash -lc "source /opt/ros/humble/setup.bash && source /opt/unitree_ros2/cyclonedds_ws/install/setup.bash && colcon build"

RUN pip3 install --no-cache-dir \
        numpy \
        opencv-python \
        sounddevice \
        aiortc \
        aiohttp \
        unitree_webrtc_connect \
        ultralytics \
        "supervision>=0.21"

# Pre-download YOLOv11n weights so first-frame latency isn't gated on a
# GitHub round-trip and the container can start with no internet.
# The path matches the YOLO_MODEL env default in go2_vision_detector.py.
RUN mkdir -p /app/models \
    && curl -fsSL -o /app/models/yolo11n.pt \
         https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11n.pt

WORKDIR /app
COPY cyclonedds.xml app.py \
     go2_video_bridge.py go2_mic_node.py go2_audio_bridge.py \
     go2_uwb_bridge.py go2_uwb_filter.py go2_dog_marker.py \
     go2_vision_detector.py go2_vision_tracker.py \
     go2_pose_bridge.py go2_lidar_filter.py go2_tf_bridge.py \
     ./

ENV PYTHONUNBUFFERED=1
ENV CYCLONEDDS_URI=file:///app/cyclonedds.xml
ENV RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
ENV GO2_IP={{.GO2_IP}}
ENV FOXGLOVE_PORT={{.WATCHTOWER_PORT}}

# BYTETrack tuning for the CPU-bound, ~2 FPS reality on Orin NX with 4
# cores online. Defaults assume 30 FPS and break at this rate:
#   - BUFFER_FRAMES=30 at 2 FPS = 15 s of phantom-track persistence;
#     drop to 6 (~3 s) so dead tracks don't linger and shadow new ones.
#   - MATCH_THRESH=0.8 fails between consecutive 500 ms frames because
#     a walking person's bbox barely overlaps frame-to-frame; 0.35
#     accepts loose IoU as a match so the same person keeps their ID.
# Override at run time (e.g. once GPU/TensorRT is back) by setting these
# in WendyOS or `docker run -e ...` to whatever's right for the rate.
ENV BYTETRACK_BUFFER_FRAMES=6
ENV BYTETRACK_MATCH_THRESH=0.35

EXPOSE {{.WATCHTOWER_PORT}}

CMD ["bash", "-lc", "source /opt/ros/humble/setup.bash && source /opt/unitree_ros2/cyclonedds_ws/install/setup.bash && exec python3 /app/app.py"]
