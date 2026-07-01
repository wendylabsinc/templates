# ros2-talker-listener (Swift)

The canonical ROS 2 `talker` / `listener` demo, in Swift. A two-service app
group built on [swift-ros2](https://github.com/youtalk/swift-ros2) — a pure-Swift
ROS 2 client (no `rclcpp`, no C++ interop) that speaks the ROS 2 wire format
directly over CycloneDDS.

- **talker** publishes `std_msgs/String` (`"Hello World: N"`) on `/chatter` at 1 Hz.
- **listener** subscribes to `/chatter` and logs every message it receives.

Both nodes use the **Humble** wire format over **CycloneDDS multicast**, so they
join the same ROS 2 graph as the other Humble-based Wendy templates.

## Deploy

```sh
wendy run --device <device> -y --detach
```

## See it work

```sh
wendy device logs --device <device>
```

You should see the talker's `Publishing: 'Hello World: N'` and the listener's
`I heard: 'Hello World: N'` interleaved — two separate containers discovering
each other over real DDS multicast.

## Interoperate with ROS 2

Because the wire format is Humble + CycloneDDS, a ROS 2 Humble node on the same
LAN and `ROS_DOMAIN_ID` can talk to these nodes directly:

```sh
source /opt/ros/humble/setup.bash
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
export ROS_DOMAIN_ID=0
ros2 topic echo /chatter std_msgs/msg/String   # sees the Swift talker
ros2 run demo_nodes_cpp talker                  # the Swift listener hears it
```

## Configuration

| Variable        | Default | Purpose                                                        |
|-----------------|---------|----------------------------------------------------------------|
| `APP_ID`        | —       | Application identifier.                                         |
| `ROS_DOMAIN_ID` | `0`     | CycloneDDS discovery domain. Both services (and any ROS 2 peer) must share it. |
