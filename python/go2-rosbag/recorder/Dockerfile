# Go2 topic recorder — ROS 2 Humble + CycloneDDS + Unitree message packages,
# so `ros2 bag record -a` has type support for the Go2's native DDS topics
# (rt/lowstate, rt/sportmodestate, rt/utlidar/*, rt/api/*, ...). Without the
# unitree_* message packages, rosbag2 skips those topics for lack of typesupport.
#
# arm64 (the Go2's Jetson). ros:humble has an arm64 image; build natively on
# Apple Silicon via `wendy run`.
FROM ros:humble-ros-base

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        ros-humble-rmw-cyclonedds-cpp \
        ros-humble-rosidl-generator-dds-idl \
        ros-humble-rosbag2 \
        ros-humble-rosbag2-storage-mcap \
        ros-humble-rosbag2-transport \
        python3-pip git iproute2 \
 && rm -rf /var/lib/apt/lists/*

# Pull the Unitree message packages and build them. We copy any package that
# looks like a unitree message package (robust to the repo's nested layout)
# and colcon-build just those — we don't need to build cyclonedds itself
# (rmw_cyclonedds_cpp comes from apt above).
RUN git clone --depth 1 https://github.com/unitreerobotics/unitree_ros2 /tmp/unitree_ros2
RUN mkdir -p /unitree_ws/src \
 && for d in $(find /tmp/unitree_ros2 -name package.xml -exec dirname {} \;); do \
        name=$(basename "$d"); \
        case "$name" in unitree_*|common) echo "  + $name"; cp -r "$d" /unitree_ws/src/ ;; esac; \
    done \
 && echo "packages to build:" && ls /unitree_ws/src
RUN . /opt/ros/humble/setup.sh \
 && cd /unitree_ws \
 && colcon build --cmake-args -DCMAKE_BUILD_TYPE=Release \
 && rm -rf build log /tmp/unitree_ros2

RUN pip3 install --no-cache-dir fastapi==0.115.6 uvicorn==0.34.0

COPY entrypoint.sh /entrypoint.sh
COPY server.py /server.py
RUN chmod +x /entrypoint.sh

# GO2_IP: any address on the robot's internal DDS network — used only to pick
# the right network interface to bind CycloneDDS to. ROS_DOMAIN_ID 0 is the
# Go2 default. AUTO_RECORD=1 starts recording immediately on launch.
ENV ROS_DOMAIN_ID=0 \
    RMW_IMPLEMENTATION=rmw_cyclonedds_cpp \
    GO2_IP=192.168.123.161 \
    AUTO_RECORD=0 \
    PORT=7000

EXPOSE 7000
CMD ["/entrypoint.sh"]
