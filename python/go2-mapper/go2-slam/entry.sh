#!/bin/bash
# go2-slam entrypoint. Two LiDAR front-ends, selected by LIDAR_SOURCE:
#   mid360 (default) — Unitree Mid-360 xyz-only cloud -> cloud_shim -> VELO16
#   hesai            — aftermarket Hesai XT-16 (real ring+time) -> HESAIxt32
# Both feed Point-LIO + the foxglove bridge + the viz/pose/snapshot sidecar.
set -e
source /opt/ros/humble/setup.bash
source /ws/install/setup.bash

LIDAR_SOURCE="${LIDAR_SOURCE:-mid360}"
CFG=/tmp/point_lio.yaml

if [ "$LIDAR_SOURCE" = "hesai" ]; then
  echo "[go2-slam] LiDAR source: Hesai XT-16 (native HESAIxt32, no shim)"
  cp /ws/config/hesai_go2.yaml "$CFG"
  sed -i "s|imu_topic:.*|imu_topic: \"${IMU_TOPIC:-/utlidar/imu}\"|" "$CFG"
  # Point the Hesai driver at this device's sensor + host NIC.
  HCFG=/tmp/hesai_xt16.yaml
  cp /ws/config/hesai_xt16.yaml "$HCFG"
  sed -i "s|device_ip_address:.*|device_ip_address: ${GO2_HESAI_IP:-192.168.123.20}|" "$HCFG"
  sed -i "s|host_ip_address:.*|host_ip_address: ${GO2_HESAI_HOST:-192.168.123.18}|" "$HCFG"
  ros2 run hesai_ros_driver hesai_ros_driver_node --ros-args -p config_path:="$HCFG" &
else
  echo "[go2-slam] LiDAR source: Unitree Mid-360 (cloud_shim -> VELO16)"
  cp /ws/config/mid360_go2.yaml "$CFG"
  sed -i "s|imu_topic:.*|imu_topic: \"${IMU_TOPIC:-/utlidar/imu}\"|" "$CFG"
  LIDAR_TOPIC="${LIDAR_TOPIC:-/utlidar/cloud_deskewed}" python3 /opt/cloud_shim.py &
fi

# Live map view for Foxglove.
ros2 run foxglove_bridge foxglove_bridge --ros-args -p port:=${FOXGLOVE_PORT:-8765} &
# viz/pose/health/snapshot/keyframe sidecar (consumes Point-LIO's /cloud_registered).
python3 /opt/viz_bridge.py &
exec ros2 run point_lio pointlio_mapping --ros-args --params-file "$CFG"
