# go2-slam — Point-LIO live LiDAR-inertial odometry on the Go2's Jetson.
#
# Point-LIO is chosen over FAST-LIO2 because it tolerates the jerky sensor
# motion of legged robots better. Both are light: ~1 CPU core on Orin NX.
#
# Point-LIO source: the SMBU-PolarBear ROS2 port (ament, executable
# `pointlio_mapping`) pinned to tag 1.0.0. The upstream hku-mars repo has no
# ROS2 branch. It references livox_ros_driver2::msg::CustomMsg unconditionally,
# so we vendor a minimal msg-only livox_ros_driver2 package to satisfy the
# build (the Livox path is never used at runtime).
#
# LiDAR front-ends (selected at runtime by LIDAR_SOURCE, see entry.sh):
#  - mid360: /utlidar/cloud_deskewed is x,y,z,intensity only (no ring/time),
#    which no stock Point-LIO handler accepts, so cloud_shim.py repacks it into
#    the Velodyne layout and the config runs lidar_type=2 (VELO16).
#  - hesai: an aftermarket Hesai XT-16 publishes a real PointCloud2 with
#    per-point ring+timestamp on /hesai/points, consumed via lidar_type=4
#    (HESAIxt32) with no shim. Built from the Hesai ROS2 driver (ARG WITH_HESAI).
#    NOTE: Point-LIO still needs an IMU; the XT-16 has none, so it pairs with the
#    Go2's /utlidar/imu + online extrinsic estimation (see config/hesai_go2.yaml).

FROM ros:humble-ros-base

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential git cmake \
    libeigen3-dev libpcl-dev libgoogle-glog-dev \
    ros-humble-pcl-conversions ros-humble-pcl-ros \
    ros-humble-foxglove-bridge \
    ros-humble-rmw-cyclonedds-cpp \
    python3-pip python3-dev python3-numpy \
    libpcap-dev libyaml-cpp-dev libboost-all-dev libssl-dev \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir numpy

# Vendored livox_ros_driver2 messages (build-time only) + the real ROS2
# Point-LIO. colcon builds livox_ros_driver2 first (Point-LIO depends on it).
COPY livox_ros_driver2 /ws/src/livox_ros_driver2
RUN git clone --depth 1 --branch 1.0.0 \
    https://github.com/SMBU-PolarBear-Robotics-Team/point_lio.git /ws/src/point_lio

# Hesai XT-16 ROS2 driver (for LIDAR_SOURCE=hesai). Skip with --build-arg
# WITH_HESAI=0 if the target Go2 only has the Mid-360.
ARG WITH_HESAI=1
RUN if [ "$WITH_HESAI" = "1" ]; then \
      git clone --recurse-submodules --depth 1 \
        https://github.com/HesaiTechnology/HesaiLidar_ROS_2.0.git /ws/src/hesai_ros_driver; \
    fi

WORKDIR /ws
RUN . /opt/ros/humble/setup.sh && \
    colcon build --symlink-install --cmake-args -DCMAKE_BUILD_TYPE=Release

COPY config/mid360_go2.yaml /ws/config/mid360_go2.yaml
COPY config/hesai_go2.yaml /ws/config/hesai_go2.yaml
COPY config/hesai_xt16.yaml /ws/config/hesai_xt16.yaml
COPY cyclonedds.xml /opt/cyclonedds.xml
COPY cloud_shim.py /opt/cloud_shim.py
COPY viz_bridge.py /opt/viz_bridge.py
COPY entry.sh /opt/entry.sh
RUN chmod +x /opt/entry.sh

# Runtime config (baked at scaffold time from template.json — no compose file).
ENV RMW_IMPLEMENTATION=rmw_cyclonedds_cpp \
    CYCLONEDDS_URI=/opt/cyclonedds.xml \
    LIDAR_SOURCE={{.LIDAR_SOURCE}} \
    LIDAR_TOPIC={{.LIDAR_TOPIC}} \
    IMU_TOPIC={{.IMU_TOPIC}} \
    GO2_HESAI_IP={{.GO2_HESAI_IP}} \
    GO2_HESAI_HOST={{.GO2_HESAI_HOST}} \
    FOXGLOVE_PORT={{.FOXGLOVE_PORT}} \
    MAP_SAVE_DIR=/data/maps \
    MAP_SAVE_INTERVAL_S=30 \
    VIZ_VOXEL_M=0.05 \
    VIZ_HZ=1.0 \
    EXPORT_VOXEL_M=0.03 \
    MIN_VOXEL_HITS=2 \
    DYNAMIC_MIN_SPAN_S=0.0 \
    SNAPSHOT_KEEP=5 \
    KEYFRAME_DIST_M=0.5

EXPOSE {{.FOXGLOVE_PORT}}
ENTRYPOINT ["/opt/entry.sh"]
