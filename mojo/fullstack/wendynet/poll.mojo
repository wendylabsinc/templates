# poll(2) wrapper for single-threaded multiplexing. pollfd is hand-packed
# (8 bytes: i32 fd, i16 events, i16 revents) — no C struct interop in
# Mojo 1.0 FFI.
from std.ffi import external_call, c_int

comptime POLLIN = 0x1
comptime POLLERR = 0x8
comptime POLLHUP = 0x10

comptime _POLLFD_SIZE = 8


struct PollSet(Movable):
    var entries: List[UInt8]

    def __init__(out self):
        self.entries = List[UInt8]()

    def size(self) -> Int:
        return len(self.entries) // _POLLFD_SIZE

    def add(mut self, fd: c_int, events: Int):
        var base = len(self.entries)
        for _ in range(_POLLFD_SIZE):
            self.entries.append(0)
        var f = Int(fd)
        self.entries[base] = UInt8(f & 0xFF)
        self.entries[base + 1] = UInt8((f >> 8) & 0xFF)
        self.entries[base + 2] = UInt8((f >> 16) & 0xFF)
        self.entries[base + 3] = UInt8((f >> 24) & 0xFF)
        self.entries[base + 4] = UInt8(events & 0xFF)
        self.entries[base + 5] = UInt8((events >> 8) & 0xFF)

    def clear(mut self):
        self.entries = List[UInt8]()

    def poll(mut self, timeout_ms: Int) raises -> Int:
        # Returns the number of ready fds (0 on timeout).
        var n = Int(
            external_call["poll", c_int](
                self.entries.unsafe_ptr(), self.size(), c_int(timeout_ms)
            )
        )
        if n < 0:
            raise Error("poll() failed")
        return n

    def revents(self, i: Int) -> Int:
        var base = i * _POLLFD_SIZE
        return Int(self.entries[base + 6]) | (Int(self.entries[base + 7]) << 8)
