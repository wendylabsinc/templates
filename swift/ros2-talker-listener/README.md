# {{.APP_ID}}

A Swift ROS 2 talker/listener example for WendyOS. One service publishes
`std_msgs/msg/String` messages on `/chatter` at 1 Hz and another service logs
each message it receives.

## Requirements

- A reachable WendyOS device and Wendy CLI access
- Network access during the first build to fetch Swift packages and build
  CycloneDDS
- Multicast-capable networking for discovery between containers and any
  external ROS 2 peers

No robot or sensor hardware is required. The code uses the ROS 2 Humble wire
format through `swift-ros2` and CycloneDDS; it does not require `rclcpp` in the
containers.

## Run and verify

```sh
wendy run --detach
wendy device logs {{.APP_ID}} --tail 30
```

The `talker` logs a published `Hello World: N` message each second. The
`listener` logs the same body after DDS delivery. Filter one service with
`--service talker` or `--service listener`.

## Configuration

| Variable | Default | Purpose |
|---|---:|---|
| `APP_ID` | required | App-group identifier and log label prefix |
| `ROS_DOMAIN_ID` | `0` | DDS discovery domain shared by both services and any peer |

Both services use host-network entitlements so CycloneDDS multicast reaches the
device network.

## How it works

- `wendy.json` defines the `talker` and `listener` services and starts the
  listener after the talker.
- `talker/Sources/talker/main.swift` creates a publisher and sends one message
  per second.
- `listener/Sources/listener/main.swift` creates a subscription and logs its
  asynchronous message stream.
- Each Dockerfile builds CycloneDDS 0.10.5 and the corresponding Swift package.

## Interoperate with ROS 2

On a ROS 2 Humble host on the same network:

```sh
source /opt/ros/humble/setup.bash
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
export ROS_DOMAIN_ID={{.ROS_DOMAIN_ID}}
ros2 topic echo /chatter std_msgs/msg/String
```

## Extend it

Change the topic, rate, or message construction in the talker and update the
listener to match. Add another service directory and `wendy.json` service entry
for another node. Message definitions and QoS behavior are configured through
the `SwiftROS2` APIs in each source file.

## Operations and troubleshooting

```sh
wendy device apps stop {{.APP_ID}}
```

If only publish logs appear, confirm both services use the same domain and that
multicast is allowed. External peers must use the same ROS distribution wire
format, domain ID, and compatible QoS.
