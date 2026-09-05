# std_msgs/String publisher on /chatter at 1 Hz — the Mojo half of the ROS 2
# demo_nodes talker/listener pair. Pure Mojo over CycloneDDS: wendydds
# hand-packs the topic descriptor and dlopens libddsc.so.0, speaking the
# ROS 2 Humble wire format directly (topic rt/chatter, type
# std_msgs::msg::dds_::String_, ROS default QoS). No rclcpp, no C shim.
from std.ffi import external_call, c_int
from std.os import getenv

from wendydds.dds import StringNode

# ROS_DOMAIN_ID comes from the environment (set by the Dockerfile from the
# scaffold variable): the wendy CLI's template substitution does not yet
# cover .mojo files (Appendix W in docs/mojo-max-port-findings.md), and the
# env var is the ROS 2 convention anyway.


def main() raises:
    var domain = 0
    var env = getenv("ROS_DOMAIN_ID")
    if env != "":
        domain = Int(env)
    var node = StringNode.create(domain, "/chatter")
    var writer = node.create_writer()
    print("talker (mojo): publishing std_msgs/String on /chatter, domain", domain)

    var count = 0
    while True:
        count += 1
        var msg = "Hello World: " + String(count)
        node.write_string(writer, msg)
        print("Publishing: '" + msg + "'")
        _ = external_call["usleep", c_int](c_int(1_000_000))
