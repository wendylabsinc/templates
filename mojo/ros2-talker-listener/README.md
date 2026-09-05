# ros2-talker-listener (Mojo)

The canonical ROS 2 `talker` / `listener` demo in pure Mojo, with **no ROS 2
installation anywhere**: `wendydds` speaks the ROS 2 (Humble) wire format
directly over CycloneDDS — `libddsc.so.0` loaded via `OwnedDLHandle`, the
`std_msgs::msg::dds_::String_` topic descriptor (including the XTypes
TypeInformation/TypeObject blobs idlc would generate) hand-packed into Mojo
buffers, topics mangled to `rt/chatter`, and the ROS 2 default QoS
(reliable / keep-last 10 / volatile). A two-service app group: a 1 Hz
`std_msgs/String` publisher on `/chatter` and a subscriber that logs what it
hears.

Interop is real, both directions and both major rmw vendors (verified in
containers): `ros2 topic echo /chatter std_msgs/msg/String` hears the Mojo
talker, and the Mojo listener hears `ros2 topic pub`, under both the default
`rmw_fastrtps_cpp` and `rmw_cyclonedds_cpp`.

## Notes

- CycloneDDS 0.10.5 is built from source in a Docker stage (same pin as the
  swift sibling) but only `libddsc.so.0` ships — the Mojo build needs no DDS
  headers because every symbol resolves at runtime through the dlopen
  handle.
- The descriptor byte layout is conformance-tested against the real
  CycloneDDS headers by a C oracle (`common/mojo/wendydds/tests/`), the same
  pattern as wendycam's V4L2 ABI tests.
- `ROS_DOMAIN_ID` flows through the Dockerfile `ENV` (the wendy CLI does not
  substitute `.mojo` sources yet — findings doc, Appendix W), which is also
  the standard ROS 2 environment convention.
- v1 speaks `std_msgs/String`; other message types mean packing their
  descriptor + ops the same way (or generating them with `idlc` at build
  time). That generalization is `wendydds`' roadmap.

Deploy with `wendy run` and watch the exchange via `wendy device logs`.
