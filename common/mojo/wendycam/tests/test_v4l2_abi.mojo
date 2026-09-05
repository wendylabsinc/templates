# ABI conformance test for wendycam.v4l2: prints the same KEY=VALUE lines as
# tests/abi_ref.c so run_tests.sh can diff Mojo's view of the V4L2 ABI against
# the kernel headers'. Also asserts the pure helpers (fourcc, u32 packing).
from wendycam.v4l2 import (
    VIDIOC_QUERYCAP,
    VIDIOC_ENUM_FMT,
    VIDIOC_S_FMT,
    VIDIOC_REQBUFS,
    VIDIOC_QUERYBUF,
    VIDIOC_QBUF,
    VIDIOC_DQBUF,
    VIDIOC_STREAMON,
    VIDIOC_STREAMOFF,
    VIDIOC_S_PARM,
    V4L2_PIX_FMT_MJPEG,
    V4L2_PIX_FMT_YUYV,
    V4L2_CAP_VIDEO_CAPTURE,
    V4L2_CAP_STREAMING,
    V4L2_BUF_TYPE_VIDEO_CAPTURE,
    V4L2_MEMORY_MMAP,
    V4L2_FIELD_NONE,
    SIZEOF_CAPABILITY,
    OFF_CAP_CARD,
    OFF_CAP_DEVICE_CAPS,
    SIZEOF_FORMAT,
    OFF_FMT_PIX,
    OFF_PIX_PIXELFORMAT,
    OFF_PIX_SIZEIMAGE,
    SIZEOF_REQUESTBUFFERS,
    SIZEOF_BUFFER,
    OFF_BUF_BYTESUSED,
    OFF_BUF_MEMORY,
    OFF_BUF_M_OFFSET,
    OFF_BUF_LENGTH,
    SIZEOF_STREAMPARM,
    OFF_PARM_TIMEPERFRAME,
    fourcc,
    read_u32,
    write_u32,
)


def hex_upper(v: Int) raises -> String:
    var digits = String("0123456789ABCDEF")
    if v == 0:
        return String("0")
    var out = String("")
    var n = v
    while n > 0:
        out = String(digits[byte = (n & 0xF) : (n & 0xF) + 1]) + out
        n >>= 4
    return out


def main() raises:
    # Pure-helper asserts first: fourcc packing and u32 buffer round-trip.
    if fourcc("MJPG") != 0x47504A4D:
        raise Error("fourcc(MJPG) wrong: " + String(fourcc("MJPG")))
    if fourcc("YUYV") != 0x56595559:
        raise Error("fourcc(YUYV) wrong")
    var buf = List[UInt8]()
    for _ in range(16):
        buf.append(0)
    write_u32(buf, 4, 0xAABBCCDD)
    if read_u32(buf, 4) != 0xAABBCCDD:
        raise Error("u32 round-trip failed")
    if buf[4] != 0xDD or buf[7] != 0xAA:
        raise Error("u32 not little-endian")

    # ABI lines, same order/format as abi_ref.c.
    print("VIDIOC_QUERYCAP=0x" + hex_upper(VIDIOC_QUERYCAP))
    print("VIDIOC_ENUM_FMT=0x" + hex_upper(VIDIOC_ENUM_FMT))
    print("VIDIOC_S_FMT=0x" + hex_upper(VIDIOC_S_FMT))
    print("VIDIOC_REQBUFS=0x" + hex_upper(VIDIOC_REQBUFS))
    print("VIDIOC_QUERYBUF=0x" + hex_upper(VIDIOC_QUERYBUF))
    print("VIDIOC_QBUF=0x" + hex_upper(VIDIOC_QBUF))
    print("VIDIOC_DQBUF=0x" + hex_upper(VIDIOC_DQBUF))
    print("VIDIOC_STREAMON=0x" + hex_upper(VIDIOC_STREAMON))
    print("VIDIOC_STREAMOFF=0x" + hex_upper(VIDIOC_STREAMOFF))
    print("VIDIOC_S_PARM=0x" + hex_upper(VIDIOC_S_PARM))
    print("FMT_MJPEG=0x" + hex_upper(V4L2_PIX_FMT_MJPEG))
    print("FMT_YUYV=0x" + hex_upper(V4L2_PIX_FMT_YUYV))
    print("CAP_VIDEO_CAPTURE=0x" + hex_upper(V4L2_CAP_VIDEO_CAPTURE))
    print("CAP_STREAMING=0x" + hex_upper(V4L2_CAP_STREAMING))
    print("BUF_TYPE_VIDEO_CAPTURE=" + String(V4L2_BUF_TYPE_VIDEO_CAPTURE))
    print("MEMORY_MMAP=" + String(V4L2_MEMORY_MMAP))
    print("FIELD_NONE=" + String(V4L2_FIELD_NONE))
    print("sizeof_capability=" + String(SIZEOF_CAPABILITY))
    print("off_capability_card=" + String(OFF_CAP_CARD))
    print("off_capability_device_caps=" + String(OFF_CAP_DEVICE_CAPS))
    print("sizeof_format=" + String(SIZEOF_FORMAT))
    print("off_format_pix=" + String(OFF_FMT_PIX))
    print("off_pix_pixelformat=" + String(OFF_PIX_PIXELFORMAT))
    print("off_pix_sizeimage=" + String(OFF_PIX_SIZEIMAGE))
    print("sizeof_requestbuffers=" + String(SIZEOF_REQUESTBUFFERS))
    print("sizeof_buffer=" + String(SIZEOF_BUFFER))
    print("off_buffer_bytesused=" + String(OFF_BUF_BYTESUSED))
    print("off_buffer_memory=" + String(OFF_BUF_MEMORY))
    print("off_buffer_m_offset=" + String(OFF_BUF_M_OFFSET))
    print("off_buffer_length=" + String(OFF_BUF_LENGTH))
    print("sizeof_streamparm=" + String(SIZEOF_STREAMPARM))
    print("off_parm_timeperframe=" + String(OFF_PARM_TIMEPERFRAME))
