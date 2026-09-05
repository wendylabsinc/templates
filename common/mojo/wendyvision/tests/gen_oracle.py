# Oracle for wendyvision tests: synthesize a JPEG, then dump PIL's decode of
# it (RGB bytes) and a numpy reference letterbox (same sampling formula the
# Mojo code documents) for the Mojo side to diff against.
import struct
import sys

import numpy as np
from PIL import Image

OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/wendyvision"
W, H, IMGSZ = 401, 233, 224  # odd sizes to exercise rounding


def main():
    # Deterministic synthetic scene: gradients + a few hard edges.
    y, x = np.mgrid[0:H, 0:W]
    img = np.stack(
        [
            (x * 255 // max(W - 1, 1)),
            (y * 255 // max(H - 1, 1)),
            ((x + y) * 255 // max(W + H - 2, 1)),
        ],
        axis=-1,
    ).astype(np.uint8)
    img[40:80, 50:120] = (255, 0, 0)
    img[100:180, 200:320] = (0, 255, 32)
    Image.fromarray(img).save(f"{OUT}/test.jpg", quality=90)

    decoded = np.asarray(Image.open(f"{OUT}/test.jpg").convert("RGB"))
    decoded.tofile(f"{OUT}/decoded_ref.rgb")
    with open(f"{OUT}/decoded_ref.meta", "w") as f:
        f.write(f"{decoded.shape[1]} {decoded.shape[0]}\n")

    # Letterbox reference on PIL's decode, same formula as letterbox.mojo.
    src = decoded.astype(np.float32)
    h, w = src.shape[:2]
    scale = min(IMGSZ / w, IMGSZ / h)
    new_w = min(int(w * scale + 0.5), IMGSZ)
    new_h = min(int(h * scale + 0.5), IMGSZ)
    left = (IMGSZ - new_w) // 2
    top = (IMGSZ - new_h) // 2
    out = np.full((IMGSZ, IMGSZ, 3), 114.0 / 255.0, dtype=np.float32)
    xs = np.clip((np.arange(new_w) + 0.5) * (w / new_w) - 0.5, 0, None)
    ys = np.clip((np.arange(new_h) + 0.5) * (h / new_h) - 0.5, 0, None)
    x0 = np.minimum(xs.astype(np.int64), w - 1)
    y0 = np.minimum(ys.astype(np.int64), h - 1)
    x1 = np.minimum(x0 + 1, w - 1)
    y1 = np.minimum(y0 + 1, h - 1)
    fx = (xs - x0).astype(np.float32)[None, :, None]
    fy = (ys - y0).astype(np.float32)[:, None, None]
    v0 = src[y0][:, x0] * (1 - fx) + src[y0][:, x1] * fx
    v1 = src[y1][:, x0] * (1 - fx) + src[y1][:, x1] * fx
    out[top : top + new_h, left : left + new_w] = (v0 * (1 - fy) + v1 * fy) / 255.0
    out.tofile(f"{OUT}/letterbox_ref.f32")
    with open(f"{OUT}/letterbox_ref.meta", "w") as f:
        f.write(f"{IMGSZ} {scale:.10f} {left} {top}\n")
    print(f"oracle written to {OUT} (jpeg {w}x{h}, imgsz {IMGSZ})")


main()
