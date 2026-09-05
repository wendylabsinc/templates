# std_msgs/String subscriber on /chatter — the Mojo half of the ROS 2
# demo_nodes talker/listener pair. Pure Mojo over CycloneDDS: wendydds
# hand-packs the topic descriptor and dlopens libddsc.so.0, speaking the
# ROS 2 Humble wire format directly. Samples are polled at 10 Hz off a
# reliable keep-last-10 reader, so nothing is missed at the talker's 1 Hz.
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
    var reader = node.create_reader()
    print("listener (mojo): subscribed to /chatter, domain", domain)

    while True:
        for s in node.take_strings(reader, 16):
            print("I heard: '" + s + "'")
        _ = external_call["usleep", c_int](c_int(100_000))
