# JPEG decoding through libturbojpeg.so.0 via OwnedDLHandle (same FFI shape
# as wendyaudio's libasound binding). One decompressor handle is reused for
# the life of the app; decode output is tightly-packed RGB8.
from std.ffi import OwnedDLHandle, c_int

comptime TJPF_RGB = 0
comptime TJFLAG_FASTDCT = 2048


struct DecodedImage(Movable):
    var pixels: List[UInt8]  # H*W*3, RGB, row-major, no padding
    var width: Int
    var height: Int

    def __init__(out self, var pixels: List[UInt8], width: Int, height: Int):
        self.pixels = pixels^
        self.width = width
        self.height = height


struct JpegDecoder(Movable):
    var lib: OwnedDLHandle
    var handle: Int

    def __init__(out self) raises:
        var lib = OwnedDLHandle("libturbojpeg.so.0")
        var handle = Int(lib.call["tjInitDecompress", Int]())
        if handle == 0:
            raise Error("tjInitDecompress failed")
        self.lib = lib^
        self.handle = handle

    def decode_rgb(mut self, jpeg: List[UInt8]) raises -> DecodedImage:
        # tjDecompressHeader3(handle, buf, size, &w, &h, &subsamp, &colorspace)
        var dims = List[Int32](unsafe_uninit_length=4)
        for i in range(4):
            dims[i] = 0
        # Addresses passed as Int: Mojo 1.0's exclusivity checker rejects the
        # same buffer's unsafe_ptr() appearing in several arguments.
        var dp = Int(dims.unsafe_ptr())
        var rc = Int(
            self.lib.call["tjDecompressHeader3", c_int](
                self.handle,
                jpeg.unsafe_ptr(),
                len(jpeg),
                dp,
                dp + 4,
                dp + 8,
                dp + 12,
            )
        )
        if rc != 0:
            raise Error("tjDecompressHeader3 failed (not a JPEG?)")
        var width = Int(dims[0])
        var height = Int(dims[1])
        if width <= 0 or height <= 0 or width > 8192 or height > 8192:
            raise Error("JPEG dimensions out of range")

        var pixels = List[UInt8](unsafe_uninit_length=width * height * 3)
        rc = Int(
            self.lib.call["tjDecompress2", c_int](
                self.handle,
                jpeg.unsafe_ptr(),
                len(jpeg),
                pixels.unsafe_ptr(),
                c_int(width),
                c_int(width * 3),  # pitch: tightly packed rows
                c_int(height),
                c_int(TJPF_RGB),
                c_int(0),  # accurate DCT: keeps parity with PIL/cv2 decodes
            )
        )
        if rc != 0:
            raise Error("tjDecompress2 failed")
        return DecodedImage(pixels^, width, height)

    def close(mut self):
        if self.handle != 0:
            _ = self.lib.call["tjDestroy", c_int](self.handle)
            self.handle = 0
