#!/bin/bash
# go2-slam entrypoint: Point-LIO + Foxglove bridge + viz/pose sidecar.
set -e
source /opt/ros/humble/setup.bash
source /ws/install/setup.bash

# Patch topics from env into the config copy (keeps template.json the single source)
CFG=/tmp/mid360_go2.yaml
cp /ws/config/mid360_go2.yaml "$CFG"
sed -i "s|lid_topic:.*|lid_topic: \"${LIDAR_TOPIC:-/utlidar/cloud_deskewed}\"|" "$CFG"
sed -i "s|imu_topic:.*|imu_topic: \"${IMU_TOPIC:-/utlidar/imu}\"|" "$CFG"

ros2 run foxglove_bridge foxglove_bridge --ros-args -p port:=${FOXGLOVE_PORT:-8765} &
python3 /opt/viz_bridge.py &
exec ros2 run point_lio pointlio_mapping --ros-args --params-file "$CFG"
