# Minimal RIFF/WAVE reader: walks chunks (not a fixed 44-byte header) and
# returns the PCM fmt fields plus the data chunk location. PCM S16LE is the
# only format the templates ship.


struct WavInfo(Copyable, Movable):
    var rate: Int
    var channels: Int
    var bits: Int
    var data_offset: Int
    var data_size: Int

    def __init__(out self, rate: Int, channels: Int, bits: Int, data_offset: Int, data_size: Int):
        self.rate = rate
        self.channels = channels
        self.bits = bits
        self.data_offset = data_offset
        self.data_size = data_size


def _u16(b: List[UInt8], off: Int) -> Int:
    return Int(b[off]) | (Int(b[off + 1]) << 8)


def _u32(b: List[UInt8], off: Int) -> Int:
    return (
        Int(b[off])
        | (Int(b[off + 1]) << 8)
        | (Int(b[off + 2]) << 16)
        | (Int(b[off + 3]) << 24)
    )


def _tag_is(b: List[UInt8], off: Int, tag: String) raises -> Bool:
    var t = tag.as_bytes()
    for i in range(4):
        if b[off + i] != t[i]:
            return False
    return True


def parse_wav(data: List[UInt8]) raises -> WavInfo:
    if len(data) < 44 or not _tag_is(data, 0, "RIFF") or not _tag_is(data, 8, "WAVE"):
        raise Error("not a RIFF/WAVE file")
    var rate = 0
    var channels = 0
    var bits = 0
    var data_offset = -1
    var data_size = 0
    var off = 12
    while off + 8 <= len(data):
        var size = _u32(data, off + 4)
        if _tag_is(data, off, "fmt "):
            if _u16(data, off + 8) != 1:
                raise Error("only PCM wav supported")
            channels = _u16(data, off + 10)
            rate = _u32(data, off + 12)
            bits = _u16(data, off + 22)
        elif _tag_is(data, off, "data"):
            data_offset = off + 8
            data_size = size
        off += 8 + size + (size & 1)  # chunks are 2-byte aligned
    if rate == 0 or data_offset < 0:
        raise Error("wav missing fmt or data chunk")
    if bits != 16:
        raise Error("only 16-bit wav supported")
    return WavInfo(rate, channels, bits, data_offset, data_size)
