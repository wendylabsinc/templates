#!/bin/bash
# Source ROS + the built Unitree messages, bind CycloneDDS to the interface
# that reaches the Go2, then launch the control server.
set -e

source /opt/ros/humble/setup.bash
[ -f /unitree_ws/install/setup.bash ] && source /unitree_ws/install/setup.bash

GO2_IP="${GO2_IP:-192.168.123.161}"

# Find the local IP / interface that routes to the Go2 (the dog is multi-homed;
# the robot DDS lives on the internal LAN, usually eth0 @ 192.168.123.x).
read IFNAME LOCALIP <<EOF
$(python3 - "$GO2_IP" <<'PY'
import socket, subprocess, sys
ip = sys.argv[1]
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
local = ""
try:
    s.connect((ip, 1))          # no packets sent; resolves egress address
    local = s.getsockname()[0]
except Exception:
    pass
finally:
    s.close()
name = ""
try:
    out = subprocess.check_output(["ip", "-o", "-4", "addr", "show"]).decode()
    for line in out.splitlines():
        p = line.split()
        if local and len(p) >= 4 and p[3].split("/")[0] == local:
            name = p[1]
            break
except Exception:
    pass
print(name, local)
PY
)
EOF

echo "[rosbag] Go2=${GO2_IP}  iface=${IFNAME:-auto}  local=${LOCALIP:-auto}  domain=${ROS_DOMAIN_ID}"

# Bind CycloneDDS to that interface so discovery finds the robot's topics.
URI=/tmp/cyclonedds.xml
IFXML=""
[ -n "$LOCALIP" ] && IFXML="<NetworkInterface address=\"$LOCALIP\"/>"
cat > "$URI" <<XML
<?xml version="1.0" encoding="UTF-8"?>
<CycloneDDS xmlns="https://cdds.io/config">
  <Domain Id="any">
    <General>
      <Interfaces>${IFXML}</Interfaces>
      <AllowMulticast>true</AllowMulticast>
      <EnableMulticastLoopback>true</EnableMulticastLoopback>
    </General>
  </Domain>
</CycloneDDS>
XML
export CYCLONEDDS_URI="file://$URI"

mkdir -p /data
exec python3 /server.py
