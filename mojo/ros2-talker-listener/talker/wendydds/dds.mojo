# CycloneDDS through libddsc.so.0 via OwnedDLHandle, speaking the ROS 2
# wire mapping: "/chatter" becomes DDS topic "rt/chatter" with type
# std_msgs::msg::dds_::String_ and the ROS 2 default QoS (reliable 100 ms,
# keep-last 10, volatile). Entities are int32 handles and samples are
# pointer-in/pointer-out, so no C struct ABI beyond the descriptor
# (descriptor.mojo) and dds_sample_info_t's valid_data flag is involved.
from std.ffi import OwnedDLHandle, external_call, c_int

from .descriptor import SI_OFF_VALID_DATA, SI_SIZEOF, StringDescriptor

# QoS enum values (dds/ddsc/dds_public_qos.h) — verified by tests/abi_ref.c.
comptime QOS_RELIABILITY_RELIABLE = 1
comptime QOS_DURABILITY_VOLATILE = 0
comptime QOS_HISTORY_KEEP_LAST = 0
comptime QOS_DEPTH = 10  # ROS 2 default history depth
comptime QOS_MAX_BLOCKING_NS = 100_000_000  # DDS_MSECS(100), rmw default
comptime _CSTR_CAP = 65536


def _cstr(s: String) -> List[UInt8]:
    var out = List[UInt8]()
    for b in s.as_bytes():
        out.append(b)
    out.append(0)
    return out^


def _read_cstring(addr: Int) -> String:
    # Byte-at-a-time copy up to NUL: no strlen extern (MMF-020) and no
    # over-read past the sample allocation.
    var out = List[UInt8]()
    var byte = List[UInt8]()
    byte.append(0)
    for i in range(_CSTR_CAP):
        _ = external_call["memcpy", Int](byte.unsafe_ptr(), addr + i, 1)
        if byte[0] == 0:
            break
        out.append(byte[0])
    var s: String
    try:
        s = String(from_utf8=out)
    except:
        s = String("<invalid utf-8>")
    return s


def _read_u64(addr: Int) -> Int:
    var cell = List[UInt8](unsafe_uninit_length=8)
    _ = external_call["memcpy", Int](cell.unsafe_ptr(), addr, 8)
    var v = 0
    for i in range(8):
        v |= Int(cell[i]) << (8 * i)
    return v


def ros_topic_to_dds(topic: String) raises -> String:
    # "/chatter" -> "rt/chatter" (rmw topic mangling for plain topics).
    if topic.startswith("/"):
        return "rt" + topic
    return "rt/" + topic


struct StringNode(Movable):
    # One DDS participant with one std_msgs/String topic. The descriptor
    # lives here so its embedded pointers outlive every entity created from
    # it; dds_delete(participant) tears down children recursively.
    var lib: OwnedDLHandle
    var desc: StringDescriptor
    var participant: c_int
    var topic: c_int

    def __init__(
        out self,
        var lib: OwnedDLHandle,
        var desc: StringDescriptor,
        participant: c_int,
        topic: c_int,
    ):
        self.lib = lib^
        self.desc = desc^
        self.participant = participant
        self.topic = topic

    def _retcode_error(mut self, what: String, rc: Int) -> Error:
        var msg_ptr = Int(self.lib.call["dds_strretcode", Int](c_int(rc)))
        var detail = _read_cstring(msg_ptr) if msg_ptr != 0 else String("")
        return Error(what + " failed: " + detail + " (" + String(rc) + ")")

    @staticmethod
    def create(domain: Int, ros_topic: String) raises -> StringNode:
        var lib = OwnedDLHandle("libddsc.so.0")
        var participant = lib.call["dds_create_participant", c_int](
            c_int(domain), Int(0), Int(0)
        )
        if Int(participant) < 0:
            raise Error(
                "dds_create_participant failed: " + String(Int(participant))
            )
        var desc = StringDescriptor()
        var name_c = _cstr(ros_topic_to_dds(ros_topic))
        var topic = lib.call["dds_create_topic", c_int](
            participant,
            desc.desc.unsafe_ptr(),
            name_c.unsafe_ptr(),
            Int(0),
            Int(0),
        )
        # name_c may be freed after the call (CycloneDDS copies the name);
        # referencing it here keeps it alive across the call above.
        if len(name_c) == 0 or Int(topic) < 0:
            _ = lib.call["dds_delete", c_int](participant)
            raise Error("dds_create_topic failed: " + String(Int(topic)))
        return StringNode(lib^, desc^, participant, topic)

    def _ros_default_qos(mut self) raises -> Int:
        var qos = Int(self.lib.call["dds_create_qos", Int]())
        if qos == 0:
            raise Error("dds_create_qos failed")
        _ = self.lib.call["dds_qset_reliability", c_int](
            qos, c_int(QOS_RELIABILITY_RELIABLE), QOS_MAX_BLOCKING_NS
        )
        _ = self.lib.call["dds_qset_history", c_int](
            qos, c_int(QOS_HISTORY_KEEP_LAST), c_int(QOS_DEPTH)
        )
        _ = self.lib.call["dds_qset_durability", c_int](
            qos, c_int(QOS_DURABILITY_VOLATILE)
        )
        return qos

    def create_writer(mut self) raises -> c_int:
        var qos = self._ros_default_qos()
        var writer = self.lib.call["dds_create_writer", c_int](
            self.participant, self.topic, qos, Int(0)
        )
        _ = self.lib.call["dds_delete_qos", c_int](qos)
        if Int(writer) < 0:
            raise self._retcode_error("dds_create_writer", Int(writer))
        return writer

    def create_reader(mut self) raises -> c_int:
        var qos = self._ros_default_qos()
        var reader = self.lib.call["dds_create_reader", c_int](
            self.participant, self.topic, qos, Int(0)
        )
        _ = self.lib.call["dds_delete_qos", c_int](qos)
        if Int(reader) < 0:
            raise self._retcode_error("dds_create_reader", Int(reader))
        return reader

    def write_string(mut self, writer: c_int, message: String) raises:
        # The C sample is struct { char *data; }: an 8-byte cell holding the
        # pointer to the NUL-terminated bytes.
        var data_c = _cstr(message)
        var sample = List[Int]()
        sample.append(Int(data_c.unsafe_ptr()))
        var rc = Int(self.lib.call["dds_write", c_int](writer, sample.unsafe_ptr()))
        # Reference data_c after the call so it outlives the serialization.
        if len(data_c) == 0 or rc < 0:
            raise self._retcode_error("dds_write", rc)

    def take_strings(mut self, reader: c_int, max_samples: Int) raises -> List[String]:
        var samples = List[Int]()
        for _ in range(max_samples):
            samples.append(0)
        var infos = List[UInt8](unsafe_uninit_length=max_samples * SI_SIZEOF)
        var n = Int(
            self.lib.call["dds_take", c_int](
                reader,
                samples.unsafe_ptr(),
                infos.unsafe_ptr(),
                max_samples,
                c_int(max_samples),
            )
        )
        if n < 0:
            raise self._retcode_error("dds_take", n)
        var out = List[String]()
        for i in range(n):
            if infos[i * SI_SIZEOF + SI_OFF_VALID_DATA] != 0:
                # sample -> struct { char *data; } -> the string bytes
                var data_ptr = _read_u64(samples[i])
                if data_ptr != 0:
                    out.append(_read_cstring(data_ptr))
                else:
                    out.append(String(""))
        if n > 0:
            _ = self.lib.call["dds_return_loan", c_int](
                reader, samples.unsafe_ptr(), c_int(n)
            )
        return out^

    def close(mut self):
        if Int(self.participant) >= 0:
            _ = self.lib.call["dds_delete", c_int](self.participant)
            self.participant = c_int(-1)
