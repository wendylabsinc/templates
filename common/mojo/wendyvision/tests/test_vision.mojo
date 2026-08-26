# wendyvision test: decode the oracle JPEG with TurboJPEG and letterbox it,
# diffing both against the PIL/numpy oracle written by gen_oracle.py.
from std.memory import bitcast
from std.pathlib import Path

from wendyvision.jpeg import JpegDecoder
from wendyvision.letterbox import letterbox_rgb

comptime OUT = "/tmp/wendyvision"
comptime IMGSZ = 224


def read_bytes(path: String) raises -> List[UInt8]:
    var f = open(Path(path), "r")
    var data = f.read_bytes()
    f.close()
    var out = List[UInt8]()
    for b in data:
        out.append(b)
    return out^


def read_words(path: String) raises -> List[String]:
    var f = open(Path(path), "r")
    var text = f.read()
    f.close()
    var out = List[String]()
    for part in text.split():
        out.append(String(part))
    return out^


def main() raises:
    var jpeg = read_bytes(OUT + "/test.jpg")
    var dec = JpegDecoder()
    var img = dec.decode_rgb(jpeg)

    var meta = read_words(OUT + "/decoded_ref.meta")
    var ref_w = Int(meta[0])
    var ref_h = Int(meta[1])
    if img.width != ref_w or img.height != ref_h:
        raise Error(
            "decode size mismatch: got "
            + String(img.width)
            + "x"
            + String(img.height)
        )

    # TurboJPEG and PIL both wrap libjpeg-turbo; with the accurate DCT the
    # decode is bit-exact today — the margin only covers library skew.
    var ref_rgb = read_bytes(OUT + "/decoded_ref.rgb")
    var max_diff = 0
    for i in range(len(ref_rgb)):
        var d = Int(img.pixels[i]) - Int(ref_rgb[i])
        if d < 0:
            d = -d
        if d > max_diff:
            max_diff = d
    print("decode max channel diff vs PIL:", max_diff)
    if max_diff > 2:
        raise Error("JPEG decode diverges from PIL beyond tolerance")

    var lb_meta = read_words(OUT + "/letterbox_ref.meta")
    if Int(lb_meta[0]) != IMGSZ:
        raise Error("oracle imgsz mismatch")
    var out = List[Float32](unsafe_uninit_length=IMGSZ * IMGSZ * 3)
    var map = letterbox_rgb(img.pixels, img.width, img.height, IMGSZ, out)
    if map.left != Int(lb_meta[2]) or map.top != Int(lb_meta[3]):
        raise Error("letterbox offsets mismatch")

    var ref_raw = read_bytes(OUT + "/letterbox_ref.f32")
    if len(ref_raw) != IMGSZ * IMGSZ * 3 * 4:
        raise Error("letterbox oracle size mismatch")
    # Reinterpret oracle bytes as f32 (little-endian, same as host).
    var max_f = Float64(0)
    for i in range(IMGSZ * IMGSZ * 3):
        var bits = (
            UInt32(ref_raw[i * 4])
            | (UInt32(ref_raw[i * 4 + 1]) << 8)
            | (UInt32(ref_raw[i * 4 + 2]) << 16)
            | (UInt32(ref_raw[i * 4 + 3]) << 24)
        )
        var ref_val = Float64(bitcast[DType.float32, 1](SIMD[DType.uint32, 1](bits))[0])
        var d = Float64(out[i]) - ref_val
        if d < 0:
            d = -d
        if d > max_f:
            max_f = d
    print("letterbox max abs diff vs numpy:", max_f)
    # decode drift propagates through bilinear; bound covers ±2/255 skew.
    if max_f > 0.01:
        raise Error("letterbox diverges from numpy oracle")
    dec.close()
    print("PASS: wendyvision decode + letterbox match oracle")
