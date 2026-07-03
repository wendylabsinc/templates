#!/bin/bash
# go2-slam entrypoint: cloud shim + Point-LIO + Foxglove bridge + viz/pose sidecar.
set -e
source /opt/ros/humble/setup.bash
source /ws/install/setup.bash

# lid_topic is fixed to the shim output (/point_lio/cloud) in the yaml; only the
# IMU topic is env-tunable per device.
CFG=/tmp/mid360_go2.yaml
cp /ws/config/mid360_go2.yaml "$CFG"
sed -i "s|imu_topic:.*|imu_topic: \"${IMU_TOPIC:-/utlidar/imu}\"|" "$CFG"

# Repack the Go2's xyz+intensity cloud -> Velodyne layout Point-LIO can ingest.
LIDAR_TOPIC="${LIDAR_TOPIC:-/utlidar/cloud_deskewed}" python3 /opt/cloud_shim.py &
# Live map view for Foxglove.
ros2 run foxglove_bridge foxglove_bridge --ros-args -p port:=${FOXGLOVE_PORT:-8765} &
# viz/pose/health/snapshot/keyframe sidecar (robustness features).
python3 /opt/viz_bridge.py &
exec ros2 run point_lio pointlio_mapping --ros-args --params-file "$CFG"
