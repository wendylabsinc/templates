# go2-slam — Point-LIO live LiDAR-inertial odometry on the Go2's Jetson.
#
# Point-LIO is chosen over FAST-LIO2 because it tolerates the jerky sensor
# motion of legged robots better. Both are light: ~1 CPU core on Orin NX.
#
# NOTE (the fiddly part): Unitree publishes the Mid-360 as a standard
# sensor_msgs/PointCloud2 on /utlidar/cloud_deskewed, NOT the livox custom
# msg. config/mid360_go2.yaml therefore uses the generic PointCloud2
# preprocessing path. If your firmware's cloud lacks per-point timestamps,
# deskewed input still works — Unitree already motion-corrected it.

FROM ros:humble-ros-base

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential git cmake \
    libeigen3-dev libpcl-dev \
    ros-humble-pcl-conversions ros-humble-pcl-ros \
    ros-humble-foxglove-bridge \
    python3-pip \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir numpy

# Build Point-LIO (ROS2 branch)
WORKDIR /ws/src
RUN git clone --depth 1 --branch ROS2 https://github.com/hku-mars/Point-LIO.git
WORKDIR /ws
RUN . /opt/ros/humble/setup.sh && \
    colcon build --symlink-install --cmake-args -DCMAKE_BUILD_TYPE=Release

COPY config/mid360_go2.yaml /ws/config/mid360_go2.yaml
COPY cyclonedds.xml /opt/cyclonedds.xml
COPY viz_bridge.py /opt/viz_bridge.py
COPY entry.sh /opt/entry.sh
RUN chmod +x /opt/entry.sh

# Runtime config (baked at scaffold time from template.json — no compose file).
ENV RMW_IMPLEMENTATION=rmw_cyclonedds_cpp \
    CYCLONEDDS_URI=/opt/cyclonedds.xml \
    LIDAR_TOPIC={{.LIDAR_TOPIC}} \
    IMU_TOPIC={{.IMU_TOPIC}} \
    FOXGLOVE_PORT={{.FOXGLOVE_PORT}} \
    MAP_SAVE_DIR=/data/maps \
    MAP_SAVE_INTERVAL_S=30 \
    VIZ_VOXEL_M=0.05 \
    VIZ_HZ=1.0

EXPOSE {{.FOXGLOVE_PORT}}
ENTRYPOINT ["/opt/entry.sh"]
