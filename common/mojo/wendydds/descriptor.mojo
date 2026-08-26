# Hand-packed dds_topic_descriptor_t for std_msgs::msg::dds_::String_ —
# byte-for-byte what CycloneDDS 0.10.5's idlc generates from the ROS 2 IDL
# (module std_msgs::msg::dds_ { struct String_ { string data; }; }), packed
# into Lists because Mojo 1.0 FFI has no C struct interop. The layout
# constants are conformance-tested against the real headers by
# tests/abi_ref.c. The XTypes TypeInformation/TypeObject blobs are the exact
# idlc-emitted bytes, so ROS 2 peers see full type discovery metadata.
from std.ffi import external_call

# struct dds_topic_descriptor (LP64) — verified by tests/abi_ref.c.
comptime DESC_SIZEOF = 96
comptime OFF_M_SIZE = 0
comptime OFF_M_ALIGN = 4
comptime OFF_M_FLAGSET = 8
comptime OFF_M_NKEYS = 12
comptime OFF_M_TYPENAME = 16
comptime OFF_M_KEYS = 24
comptime OFF_M_NOPS = 32
comptime OFF_M_OPS = 40
comptime OFF_M_META = 48
comptime OFF_TYPE_INFORMATION = 56  # struct dds_type_meta_ser { data*, sz }
comptime OFF_TYPE_MAPPING = 72
comptime OFF_RESTRICT_DATA_REPRESENTATION = 88

# Serializer opcodes (dds/ddsc/dds_opcodes.h).
comptime OP_ADR = 0x01000000
comptime OP_RTS = 0x00000000
comptime OP_TYPE_STR = 0x00050000
comptime TOPIC_XTYPES_METADATA = 0x40  # DDS_TOPIC_XTYPES_METADATA (1<<6)

# dds_sample_info_t (LP64) — verified by tests/abi_ref.c.
comptime SI_SIZEOF = 64
comptime SI_OFF_VALID_DATA = 12

comptime STRING_TYPE_NAME = "std_msgs::msg::dds_::String_"

# sizeof/alignof of the C sample struct: struct { char *data; }.
comptime _SAMPLE_SIZE = 8
comptime _SAMPLE_ALIGN = 8
comptime _NOPS = 2

# idlc-emitted XCDR2-serialized TypeInformation / TypeObject mapping for
# std_msgs::msg::dds_::String_ (CycloneDDS 0.10.5), hex-encoded verbatim
# (extracted mechanically from idlc output; lengths asserted in __init__).
comptime _TYPE_INFO_HEX = "6000000001100040280000002400000014000000f159656d080ab926a333a69d269cb1002800000000000000040000000000000002100040280000002400000014000000f2c3da80d638e91b9fad0a6b765cc20053000000000000000400000000000000"
comptime _TYPE_MAP_HEX = "3c00000001000000f159656d080ab926a333a69d269cb10024000000f1510100010000000000000014000000010000000c00000000000000010070008d777f386700000001000000f2c3da80d638e91b9fad0a6b765cc2004f000000f251010025000000000000001d0000007374645f6d7367733a3a6d73673a3a6464735f3a3a537472696e675f000000001b000000010000001300000000000000010070000500000064617461000000002200000001000000f2c3da80d638e91b9fad0a6b765cc2f159656d080ab926a333a69d269cb1"


def _hex_bytes(hex: String) raises -> List[UInt8]:
    var out = List[UInt8]()
    var hi = -1
    for cp in hex.codepoints():
        var c = Int(cp.to_u32())
        var v: Int
        if c >= 48 and c <= 57:
            v = c - 48
        elif c >= 97 and c <= 102:
            v = c - 87
        else:
            continue  # ignore whitespace/newlines in the literal
        if hi < 0:
            hi = v
        else:
            out.append(UInt8(hi * 16 + v))
            hi = -1
    return out^


def _cstr(s: String) -> List[UInt8]:
    var out = List[UInt8]()
    for b in s.as_bytes():
        out.append(b)
    out.append(0)
    return out^


def _zeroed(n: Int) -> List[UInt8]:
    var out = List[UInt8]()
    for _ in range(n):
        out.append(0)
    return out^


def write_u32(mut buf: List[UInt8], offset: Int, v: Int):
    for i in range(4):
        buf[offset + i] = UInt8((v >> (8 * i)) & 0xFF)


def write_u64(mut buf: List[UInt8], offset: Int, v: Int):
    for i in range(8):
        buf[offset + i] = UInt8((v >> (8 * i)) & 0xFF)


struct StringDescriptor(Movable):
    # The Lists whose addresses are embedded in `desc` live as fields, so the
    # descriptor's internal pointers stay valid exactly as long as the value:
    # List heap storage does not move when the struct moves.
    var typename_c: List[UInt8]
    var meta_c: List[UInt8]
    var ops: List[UInt8]
    var type_info: List[UInt8]
    var type_map: List[UInt8]
    var desc: List[UInt8]

    def __init__(out self) raises:
        self.typename_c = _cstr(STRING_TYPE_NAME)
        self.meta_c = _cstr("")
        self.ops = _zeroed(12)
        write_u32(self.ops, 0, OP_ADR | OP_TYPE_STR)
        write_u32(self.ops, 4, 0)  # offsetof(String_, data)
        write_u32(self.ops, 8, OP_RTS)
        self.type_info = _hex_bytes(_TYPE_INFO_HEX)
        self.type_map = _hex_bytes(_TYPE_MAP_HEX)
        if len(self.type_info) != 100 or len(self.type_map) != 210:
            raise Error("XTypes blob decode length wrong")

        var d = _zeroed(DESC_SIZEOF)
        write_u32(d, OFF_M_SIZE, _SAMPLE_SIZE)
        write_u32(d, OFF_M_ALIGN, _SAMPLE_ALIGN)
        write_u32(d, OFF_M_FLAGSET, TOPIC_XTYPES_METADATA)
        write_u32(d, OFF_M_NKEYS, 0)
        write_u64(d, OFF_M_TYPENAME, Int(self.typename_c.unsafe_ptr()))
        write_u64(d, OFF_M_KEYS, 0)
        write_u32(d, OFF_M_NOPS, _NOPS)
        write_u64(d, OFF_M_OPS, Int(self.ops.unsafe_ptr()))
        write_u64(d, OFF_M_META, Int(self.meta_c.unsafe_ptr()))
        write_u64(d, OFF_TYPE_INFORMATION, Int(self.type_info.unsafe_ptr()))
        write_u32(d, OFF_TYPE_INFORMATION + 8, len(self.type_info))
        write_u64(d, OFF_TYPE_MAPPING, Int(self.type_map.unsafe_ptr()))
        write_u32(d, OFF_TYPE_MAPPING + 8, len(self.type_map))
        write_u32(d, OFF_RESTRICT_DATA_REPRESENTATION, 0)
        self.desc = d^
