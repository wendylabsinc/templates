# V4L2 ABI: ioctl request numbers, format/capability flags, and the byte
# layout of the kernel structs wendycam packs by hand (Mojo 1.0 has no C
# struct interop for FFI calls, so buffers are Lists with explicit offsets).
# Layout values are for 64-bit Linux (arm64/x86_64) and conformance-tested
# against <linux/videodev2.h> by tests/run_tests.sh.

# --- _IOC ioctl-number math (asm-generic/ioctl.h) ---
comptime _IOC_WRITE = 1
comptime _IOC_READ = 2


def _ioc(dir: Int, typ: Int, nr: Int, size: Int) -> Int:
    return (dir << 30) | (size << 16) | (typ << 8) | nr


def _iow(typ: Int, nr: Int, size: Int) -> Int:
    return _ioc(_IOC_WRITE, typ, nr, size)


def _ior(typ: Int, nr: Int, size: Int) -> Int:
    return _ioc(_IOC_READ, typ, nr, size)


def _iowr(typ: Int, nr: Int, size: Int) -> Int:
    return _ioc(_IOC_READ | _IOC_WRITE, typ, nr, size)


# --- struct sizes / member offsets (64-bit) ---
comptime SIZEOF_CAPABILITY = 104
comptime OFF_CAP_CARD = 16
comptime OFF_CAP_DEVICE_CAPS = 88
comptime SIZEOF_FORMAT = 208
comptime OFF_FMT_PIX = 8
comptime OFF_PIX_PIXELFORMAT = 16
comptime OFF_PIX_SIZEIMAGE = 28
comptime SIZEOF_REQUESTBUFFERS = 20
comptime SIZEOF_BUFFER = 88
comptime OFF_BUF_BYTESUSED = 8
comptime OFF_BUF_MEMORY = 60
comptime OFF_BUF_M_OFFSET = 64
comptime OFF_BUF_LENGTH = 72
comptime SIZEOF_STREAMPARM = 204
comptime OFF_PARM_TIMEPERFRAME = 12
comptime SIZEOF_FMTDESC = 64

# --- ioctl requests ('V' == 0x56) ---
comptime _V = 0x56


def _vidioc_querycap() -> Int:
    return _ior(_V, 0, SIZEOF_CAPABILITY)


comptime VIDIOC_QUERYCAP = _vidioc_querycap()
comptime VIDIOC_ENUM_FMT = _iowr(_V, 2, SIZEOF_FMTDESC)
comptime VIDIOC_S_FMT = _iowr(_V, 5, SIZEOF_FORMAT)
comptime VIDIOC_REQBUFS = _iowr(_V, 8, SIZEOF_REQUESTBUFFERS)
comptime VIDIOC_QUERYBUF = _iowr(_V, 9, SIZEOF_BUFFER)
comptime VIDIOC_QBUF = _iowr(_V, 15, SIZEOF_BUFFER)
comptime VIDIOC_DQBUF = _iowr(_V, 17, SIZEOF_BUFFER)
comptime VIDIOC_STREAMON = _iow(_V, 18, 4)
comptime VIDIOC_STREAMOFF = _iow(_V, 19, 4)
comptime VIDIOC_S_PARM = _iowr(_V, 22, SIZEOF_STREAMPARM)


def fourcc(code: String) raises -> Int:
    var b = code.as_bytes()
    if len(b) != 4:
        raise Error("fourcc needs exactly 4 chars")
    return Int(b[0]) | (Int(b[1]) << 8) | (Int(b[2]) << 16) | (Int(b[3]) << 24)


# fourcc("MJPG") / fourcc("YUYV") — literal because comptime aliases cannot
# call raising functions; the test asserts fourcc() produces these values.
comptime V4L2_PIX_FMT_MJPEG = 0x47504A4D
comptime V4L2_PIX_FMT_YUYV = 0x56595559

# --- capability / enum flags ---
comptime V4L2_CAP_VIDEO_CAPTURE = 0x1
comptime V4L2_CAP_STREAMING = 0x4000000
comptime V4L2_BUF_TYPE_VIDEO_CAPTURE = 1
comptime V4L2_MEMORY_MMAP = 1
comptime V4L2_FIELD_NONE = 1


# --- little-endian u32 access into hand-packed struct buffers ---
def write_u32(mut buf: List[UInt8], off: Int, val: Int):
    buf[off] = UInt8(val & 0xFF)
    buf[off + 1] = UInt8((val >> 8) & 0xFF)
    buf[off + 2] = UInt8((val >> 16) & 0xFF)
    buf[off + 3] = UInt8((val >> 24) & 0xFF)


def read_u32(buf: List[UInt8], off: Int) -> Int:
    return (
        Int(buf[off])
        | (Int(buf[off + 1]) << 8)
        | (Int(buf[off + 2]) << 16)
        | (Int(buf[off + 3]) << 24)
    )
