# V4L2 capture via libc FFI: open → S_FMT (MJPEG) → REQBUFS/mmap → STREAMON,
# then DQBUF/copy/QBUF per frame. Kernel structs are hand-packed byte buffers
# using the offsets conformance-tested in v4l2.mojo (no C struct interop in
# Mojo 1.0 FFI). MJPEG frames come out ready to send — no encode step.
from std.ffi import external_call, c_int, c_ssize_t

from .v4l2 import (
    VIDIOC_QUERYCAP,
    VIDIOC_S_FMT,
    VIDIOC_REQBUFS,
    VIDIOC_QUERYBUF,
    VIDIOC_QBUF,
    VIDIOC_DQBUF,
    VIDIOC_STREAMON,
    VIDIOC_STREAMOFF,
    VIDIOC_S_PARM,
    V4L2_PIX_FMT_MJPEG,
    V4L2_CAP_VIDEO_CAPTURE,
    V4L2_CAP_STREAMING,
    V4L2_BUF_TYPE_VIDEO_CAPTURE,
    V4L2_MEMORY_MMAP,
    V4L2_FIELD_NONE,
    SIZEOF_CAPABILITY,
    OFF_CAP_CARD,
    OFF_CAP_DEVICE_CAPS,
    SIZEOF_FORMAT,
    OFF_PIX_PIXELFORMAT,
    SIZEOF_REQUESTBUFFERS,
    SIZEOF_BUFFER,
    OFF_BUF_BYTESUSED,
    OFF_BUF_MEMORY,
    OFF_BUF_M_OFFSET,
    OFF_BUF_LENGTH,
    SIZEOF_STREAMPARM,
    OFF_PARM_TIMEPERFRAME,
    read_u32,
    write_u32,
)

comptime O_RDWR = 2
comptime O_NONBLOCK = 0x800
comptime AT_FDCWD = -100
comptime NUM_BUFFERS = 4


def _open_path(cpath: List[UInt8], flags: Int) -> c_int:
    # openat(AT_FDCWD, ...) instead of open(): the Mojo stdlib's file API
    # already declares the `open` extern with a different signature, and
    # duplicate extern declarations fail LLVM lowering (see findings doc).
    return external_call["openat", c_int](
        c_int(AT_FDCWD), cpath.unsafe_ptr(), c_int(flags)
    )


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


def _ioctl(fd: c_int, request: Int, mut buf: List[UInt8]) -> Int:
    return Int(external_call["ioctl", c_int](fd, request, buf.unsafe_ptr()))


struct CameraInfo(Copyable, Movable):
    var path: String
    var name: String

    def __init__(out self, path: String, name: String):
        self.path = path
        self.name = name


def _querycap(fd: c_int) raises -> List[UInt8]:
    var cap = _zeroed(SIZEOF_CAPABILITY)
    if _ioctl(fd, VIDIOC_QUERYCAP, cap) != 0:
        raise Error("VIDIOC_QUERYCAP failed (not a V4L2 device?)")
    return cap^


def _card_name(cap: List[UInt8]) raises -> String:
    var name_bytes = List[UInt8]()
    for i in range(32):
        var b = cap[OFF_CAP_CARD + i]
        if b == 0:
            break
        name_bytes.append(b)
    return String(from_utf8=name_bytes)


def _is_capture_device(cap: List[UInt8]) -> Bool:
    var device_caps = read_u32(cap, OFF_CAP_DEVICE_CAPS)
    return (
        (device_caps & V4L2_CAP_VIDEO_CAPTURE) != 0
        and (device_caps & V4L2_CAP_STREAMING) != 0
    )


def list_cameras() raises -> List[CameraInfo]:
    # /dev/video0..63; capture-capable nodes only (UVC cameras also expose
    # metadata nodes, which QUERYCAP's device_caps filters out).
    var out = List[CameraInfo]()
    for i in range(64):
        var path = "/dev/video" + String(i)
        var cpath = _cstr(path)
        var fd = _open_path(cpath, O_RDWR | O_NONBLOCK)
        if fd < 0:
            continue
        try:
            var cap = _querycap(fd)
            if _is_capture_device(cap):
                out.append(CameraInfo(path, _card_name(cap)))
        except:
            pass
        _ = external_call["close", c_int](fd)
    return out^


struct Camera(Movable):
    var fd: c_int
    var path: String
    var name: String
    var width: Int
    var height: Int
    var buf_addrs: List[Int]
    var buf_lens: List[Int]
    var streaming: Bool

    def __init__(out self, path: String, width: Int, height: Int) raises:
        var cpath = _cstr(path)
        var fd = _open_path(cpath, O_RDWR)
        if fd < 0:
            raise Error("open(" + path + ") failed")
        self.fd = fd
        self.path = path
        self.name = String("")
        self.width = width
        self.height = height
        self.buf_addrs = List[Int]()
        self.buf_lens = List[Int]()
        self.streaming = False

        try:
            var cap = _querycap(fd)
            if not _is_capture_device(cap):
                raise Error(path + " is not a streaming capture device")
            self.name = _card_name(cap)
            self._setup()
        except e:
            _ = external_call["close", c_int](fd)
            raise e

    def _setup(mut self) raises:
        # S_FMT: MJPEG at the requested size (driver may adjust — read back).
        var fmt = _zeroed(SIZEOF_FORMAT)
        write_u32(fmt, 0, V4L2_BUF_TYPE_VIDEO_CAPTURE)
        write_u32(fmt, 8, self.width)  # fmt.pix.width
        write_u32(fmt, 12, self.height)  # fmt.pix.height
        write_u32(fmt, OFF_PIX_PIXELFORMAT, V4L2_PIX_FMT_MJPEG)
        write_u32(fmt, 20, V4L2_FIELD_NONE)  # fmt.pix.field
        if _ioctl(self.fd, VIDIOC_S_FMT, fmt) != 0:
            raise Error("VIDIOC_S_FMT failed")
        if read_u32(fmt, OFF_PIX_PIXELFORMAT) != V4L2_PIX_FMT_MJPEG:
            raise Error(self.path + " has no MJPEG support")
        self.width = read_u32(fmt, 8)
        self.height = read_u32(fmt, 12)

        # 30 fps hint; drivers are free to ignore it.
        var parm = _zeroed(SIZEOF_STREAMPARM)
        write_u32(parm, 0, V4L2_BUF_TYPE_VIDEO_CAPTURE)
        write_u32(parm, OFF_PARM_TIMEPERFRAME, 1)
        write_u32(parm, OFF_PARM_TIMEPERFRAME + 4, 30)
        _ = _ioctl(self.fd, VIDIOC_S_PARM, parm)

        # REQBUFS + QUERYBUF + mmap + initial QBUF.
        var req = _zeroed(SIZEOF_REQUESTBUFFERS)
        write_u32(req, 0, NUM_BUFFERS)
        write_u32(req, 4, V4L2_BUF_TYPE_VIDEO_CAPTURE)
        write_u32(req, 8, V4L2_MEMORY_MMAP)
        if _ioctl(self.fd, VIDIOC_REQBUFS, req) != 0:
            raise Error("VIDIOC_REQBUFS failed")
        var count = read_u32(req, 0)
        if count < 1:
            raise Error("REQBUFS returned no buffers")

        for i in range(count):
            var buf = _zeroed(SIZEOF_BUFFER)
            write_u32(buf, 0, i)  # index
            write_u32(buf, 4, V4L2_BUF_TYPE_VIDEO_CAPTURE)
            write_u32(buf, OFF_BUF_MEMORY, V4L2_MEMORY_MMAP)
            if _ioctl(self.fd, VIDIOC_QUERYBUF, buf) != 0:
                raise Error("VIDIOC_QUERYBUF failed")
            var length = read_u32(buf, OFF_BUF_LENGTH)
            var offset = read_u32(buf, OFF_BUF_M_OFFSET)
            # mmap(NULL, len, PROT_READ|PROT_WRITE=3, MAP_SHARED=1, fd, off)
            var addr = external_call["mmap", Int](
                Int(0), length, c_int(3), c_int(1), self.fd, offset
            )
            if addr == -1 or addr == 0:
                raise Error("mmap of capture buffer failed")
            self.buf_addrs.append(addr)
            self.buf_lens.append(length)
            if _ioctl(self.fd, VIDIOC_QBUF, buf) != 0:
                raise Error("initial VIDIOC_QBUF failed")

        var stream_type = _zeroed(4)
        write_u32(stream_type, 0, V4L2_BUF_TYPE_VIDEO_CAPTURE)
        if _ioctl(self.fd, VIDIOC_STREAMON, stream_type) != 0:
            raise Error("VIDIOC_STREAMON failed")
        self.streaming = True

    def read_frame(mut self) raises -> List[UInt8]:
        # Blocking DQBUF → copy JPEG bytes out → QBUF the slot back.
        var buf = _zeroed(SIZEOF_BUFFER)
        write_u32(buf, 4, V4L2_BUF_TYPE_VIDEO_CAPTURE)
        write_u32(buf, OFF_BUF_MEMORY, V4L2_MEMORY_MMAP)
        if _ioctl(self.fd, VIDIOC_DQBUF, buf) != 0:
            raise Error("VIDIOC_DQBUF failed")
        var index = read_u32(buf, 0)
        var used = read_u32(buf, OFF_BUF_BYTESUSED)
        if index >= len(self.buf_addrs) or used > self.buf_lens[index]:
            raise Error("DQBUF returned out-of-range buffer")
        var frame = List[UInt8](unsafe_uninit_length=used)
        _ = external_call["memcpy", Int](
            frame.unsafe_ptr(), self.buf_addrs[index], used
        )
        if _ioctl(self.fd, VIDIOC_QBUF, buf) != 0:
            raise Error("VIDIOC_QBUF failed")
        return frame^

    def close(mut self):
        if self.streaming:
            var stream_type = _zeroed(4)
            write_u32(stream_type, 0, V4L2_BUF_TYPE_VIDEO_CAPTURE)
            _ = _ioctl(self.fd, VIDIOC_STREAMOFF, stream_type)
            self.streaming = False
        for i in range(len(self.buf_addrs)):
            _ = external_call["munmap", c_int](self.buf_addrs[i], self.buf_lens[i])
        self.buf_addrs = List[Int]()
        self.buf_lens = List[Int]()
        if self.fd >= 0:
            _ = external_call["close", c_int](self.fd)
            self.fd = -1
