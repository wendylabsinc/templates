# wendydds tests: descriptor ABI conformance against the C oracle
# (abi_ref.c), hand-packed descriptor sanity, and a real CycloneDDS loopback
# pub/sub round trip on the ROS 2 wire mapping (rt/ topic, std_msgs String
# type). Needs libddsc.so.0; run via run_tests.sh in a container with
# CycloneDDS 0.10.5 installed.
from std.ffi import external_call, c_int
from std.os import getenv
from std.pathlib import Path

from wendydds.descriptor import (
    DESC_SIZEOF,
    OFF_M_ALIGN,
    OFF_M_FLAGSET,
    OFF_M_KEYS,
    OFF_M_META,
    OFF_M_NKEYS,
    OFF_M_NOPS,
    OFF_M_OPS,
    OFF_M_SIZE,
    OFF_M_TYPENAME,
    OFF_RESTRICT_DATA_REPRESENTATION,
    OFF_TYPE_INFORMATION,
    OFF_TYPE_MAPPING,
    OP_ADR,
    OP_RTS,
    OP_TYPE_STR,
    SI_SIZEOF,
    SI_OFF_VALID_DATA,
    STRING_TYPE_NAME,
    TOPIC_XTYPES_METADATA,
    StringDescriptor,
)
from wendydds.dds import (
    QOS_DURABILITY_VOLATILE,
    QOS_HISTORY_KEEP_LAST,
    QOS_RELIABILITY_RELIABLE,
    StringNode,
)


def read_abi_ref(path: String) raises -> List[String]:
    var f = open(Path(path), "r")
    var data = f.read_bytes()
    f.close()
    var bytes = List[UInt8]()
    for b in data:
        bytes.append(b)
    var out = List[String]()
    for line in String(from_utf8=bytes).split("\n"):
        out.append(String(line))
    return out^


def abi_value(lines: List[String], key: String) raises -> Int:
    for l in lines:
        if l.startswith(key + "="):
            return Int(String(l.split("=")[1]))
    raise Error("abi oracle missing key: " + key)


def check(lines: List[String], key: String, mojo_value: Int) raises:
    var c_value = abi_value(lines, key)
    if c_value != mojo_value:
        raise Error(
            "ABI mismatch " + key + ": C=" + String(c_value)
            + " mojo=" + String(mojo_value)
        )


def read_u64_at(addr: Int) -> Int:
    var cell = List[UInt8](unsafe_uninit_length=8)
    _ = external_call["memcpy", Int](cell.unsafe_ptr(), addr, 8)
    var v = 0
    for i in range(8):
        v |= Int(cell[i]) << (8 * i)
    return v


def main() raises:
    var ref_path = getenv("DDS_ABI_REF")
    if ref_path == "":
        raise Error("set DDS_ABI_REF=/path/to/abi_ref output")
    var lines = read_abi_ref(ref_path)

    # --- descriptor + opcodes + QoS ABI vs the C headers ---
    check(lines, "sizeof_descriptor", DESC_SIZEOF)
    check(lines, "off_m_size", OFF_M_SIZE)
    check(lines, "off_m_align", OFF_M_ALIGN)
    check(lines, "off_m_flagset", OFF_M_FLAGSET)
    check(lines, "off_m_nkeys", OFF_M_NKEYS)
    check(lines, "off_m_typename", OFF_M_TYPENAME)
    check(lines, "off_m_keys", OFF_M_KEYS)
    check(lines, "off_m_nops", OFF_M_NOPS)
    check(lines, "off_m_ops", OFF_M_OPS)
    check(lines, "off_m_meta", OFF_M_META)
    check(lines, "off_type_information", OFF_TYPE_INFORMATION)
    check(lines, "off_type_mapping", OFF_TYPE_MAPPING)
    check(lines, "off_restrict_data_representation", OFF_RESTRICT_DATA_REPRESENTATION)
    check(lines, "op_adr", OP_ADR)
    check(lines, "op_rts", OP_RTS)
    check(lines, "op_type_str", OP_TYPE_STR)
    check(lines, "topic_xtypes_metadata", TOPIC_XTYPES_METADATA)
    check(lines, "reliability_reliable", QOS_RELIABILITY_RELIABLE)
    check(lines, "durability_volatile", QOS_DURABILITY_VOLATILE)
    check(lines, "history_keep_last", QOS_HISTORY_KEEP_LAST)
    check(lines, "sizeof_sample_info", SI_SIZEOF)
    check(lines, "off_si_valid_data", SI_OFF_VALID_DATA)
    print("PASS: descriptor/opcodes/QoS ABI matches CycloneDDS headers")

    # --- hand-packed descriptor: embedded pointers land on the payloads ---
    var desc = StringDescriptor()
    var base = Int(desc.desc.unsafe_ptr())
    var tn_addr = read_u64_at(base + OFF_M_TYPENAME)
    var tn = List[UInt8](unsafe_uninit_length=4)
    _ = external_call["memcpy", Int](tn.unsafe_ptr(), tn_addr, 4)
    if not (tn[0] == 115 and tn[1] == 116 and tn[2] == 100 and tn[3] == 95):
        raise Error("m_typename does not point at 'std_'")
    var ops_addr = read_u64_at(base + OFF_M_OPS)
    var op0 = List[UInt8](unsafe_uninit_length=4)
    _ = external_call["memcpy", Int](op0.unsafe_ptr(), ops_addr, 4)
    var op0_val = (
        Int(op0[0]) | (Int(op0[1]) << 8) | (Int(op0[2]) << 16) | (Int(op0[3]) << 24)
    )
    if op0_val != OP_ADR | OP_TYPE_STR:
        raise Error("ops[0] wrong: " + String(op0_val))
    if STRING_TYPE_NAME != "std_msgs::msg::dds_::String_":
        raise Error("type name wrong")
    # keep desc alive across the raw-address reads above
    if len(desc.desc) != DESC_SIZEOF:
        raise Error("descriptor buffer size wrong")
    print("PASS: hand-packed descriptor pointers + ops")

    # --- loopback pub/sub over real libddsc (ROS 2 wire shapes) ---
    var node = StringNode.create(42, "/wendydds_selftest")
    var writer = node.create_writer()
    var reader = node.create_reader()
    node.write_string(writer, "Hello World: 1")
    node.write_string(writer, "")
    node.write_string(writer, "Söhne 🚀")
    var got = List[String]()
    for _ in range(100):
        for s in node.take_strings(reader, 8):
            got.append(s)
        if len(got) >= 3:
            break
        _ = external_call["usleep", c_int](c_int(50000))
    if len(got) != 3:
        raise Error("expected 3 samples, got " + String(len(got)))
    if got[0] != "Hello World: 1" or got[1] != "" or got[2] != "Söhne 🚀":
        raise Error("payload mismatch: " + got[0] + "|" + got[1] + "|" + got[2])
    print("PASS: loopback pub/sub round trip (3 samples, unicode + empty)")

    # --- errors surface as raises ---
    var raised = False
    try:
        var bad = StringNode.create(42, "invalid topic name with spaces")
        bad.close()
    except:
        raised = True
    if not raised:
        raise Error("invalid topic name should raise")
    node.close()
    print("PASS: invalid topic raises; clean shutdown")
