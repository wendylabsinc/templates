# YOLO-style letterbox preprocessing: bilinear-resize an RGB8 image to fit a
# square imgsz canvas (aspect preserved), pad with gray 114, normalize to
# [0,1] fp32 HWC into a caller-owned buffer (shared with Python as a numpy
# view). The returned mapping inverts model-space coords back to the frame.


struct LetterboxMap(Copyable, Movable):
    var scale: Float64  # frame px * scale = model px
    var left: Int  # x padding offset in model space
    var top: Int  # y padding offset in model space

    def __init__(out self, scale: Float64, left: Int, top: Int):
        self.scale = scale
        self.left = left
        self.top = top

    def unmap_x(self, x_model: Float64) -> Float64:
        return (x_model - Float64(self.left)) / self.scale

    def unmap_y(self, y_model: Float64) -> Float64:
        return (y_model - Float64(self.top)) / self.scale


def letterbox_rgb(
    pixels: List[UInt8],
    width: Int,
    height: Int,
    imgsz: Int,
    mut out: List[Float32],
) raises -> LetterboxMap:
    # out is imgsz*imgsz*3 fp32 HWC RGB, preallocated by the caller.
    if len(out) != imgsz * imgsz * 3:
        raise Error("letterbox output buffer has wrong size")
    if len(pixels) < width * height * 3:
        raise Error("letterbox input buffer too small")

    var scale = Float64(imgsz) / Float64(width)
    var hs = Float64(imgsz) / Float64(height)
    if hs < scale:
        scale = hs
    var new_w = Int(Float64(width) * scale + 0.5)
    var new_h = Int(Float64(height) * scale + 0.5)
    if new_w > imgsz:
        new_w = imgsz
    if new_h > imgsz:
        new_h = imgsz
    var left = (imgsz - new_w) // 2
    var top = (imgsz - new_h) // 2

    comptime PAD = Float32(114.0 / 255.0)
    for i in range(imgsz * imgsz * 3):
        out[i] = PAD

    # cv2.INTER_LINEAR sampling: src = (dst + 0.5) / scale - 0.5, clamped.
    var x_ratio = Float64(width) / Float64(new_w)
    var y_ratio = Float64(height) / Float64(new_h)
    for oy in range(new_h):
        var sy = (Float64(oy) + 0.5) * y_ratio - 0.5
        if sy < 0:
            sy = 0
        var y0 = Int(sy)
        if y0 > height - 1:
            y0 = height - 1
        var y1 = y0 + 1
        if y1 > height - 1:
            y1 = height - 1
        var fy = Float32(sy - Float64(y0))
        var row_out = ((top + oy) * imgsz + left) * 3
        for ox in range(new_w):
            var sx = (Float64(ox) + 0.5) * x_ratio - 0.5
            if sx < 0:
                sx = 0
            var x0 = Int(sx)
            if x0 > width - 1:
                x0 = width - 1
            var x1 = x0 + 1
            if x1 > width - 1:
                x1 = width - 1
            var fx = Float32(sx - Float64(x0))
            var p00 = (y0 * width + x0) * 3
            var p01 = (y0 * width + x1) * 3
            var p10 = (y1 * width + x0) * 3
            var p11 = (y1 * width + x1) * 3
            for c in range(3):
                var v0 = (
                    Float32(pixels[p00 + c]) * (1 - fx)
                    + Float32(pixels[p01 + c]) * fx
                )
                var v1 = (
                    Float32(pixels[p10 + c]) * (1 - fx)
                    + Float32(pixels[p11 + c]) * fx
                )
                out[row_out + ox * 3 + c] = (v0 * (1 - fy) + v1 * fy) / 255.0
    return LetterboxMap(scale, left, top)
