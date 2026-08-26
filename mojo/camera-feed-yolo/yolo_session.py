# Serve-time runtime for the Mojo app: device/imgsz resolution and the
# inference Session over the graphs defined in model.py. Kept separate from
# model.py so runtime changes don't invalidate the Docker layers that bake
# the MEF artifacts (model.py + weights feed the compile stage; this file is
# layered after it).
import os
import sys
import time

import numpy as np

import model


def resolve_config() -> tuple:
    """(device, imgsz) for this boot — called by the Mojo app before it
    allocates the interop buffers. One image serves every target: the MAX
    wheel picks CPU or GPU at runtime (same rationale as mojo/llm), so the
    device is probed here rather than baked at build.
    """
    # Default is CPU even on GPU devices: MAX 26.5's conv kernels on the
    # Jetson iGPU run ~10-40x below par (522 ms/frame at imgsz 320 vs 57 ms
    # on the same device's CPU at 224 — MMF-026), so "auto" would pick the
    # slower path. YOLO_DEVICE=gpu stays available (works end to end) for
    # when the kernels catch up; "probe" restores accelerator autodetect.
    device = os.environ.get("YOLO_DEVICE", "cpu")
    if device in ("auto", "probe"):
        try:
            from max.driver import accelerator_count

            device = "gpu" if accelerator_count() > 0 else "cpu"
        except Exception:
            device = "cpu"
    imgsz_env = os.environ.get("YOLO_IMGSZ", "")
    # Parity with python/camera-feed-yolo: 320 on GPU, 224 on CPU.
    imgsz = int(imgsz_env) if imgsz_env else (320 if device == "gpu" else 224)
    print(f"[model] device={device} imgsz={imgsz}", flush=True)
    return device, imgsz


class Session:
    """Inference session driven by the Mojo app.

    The Mojo side owns the input/output buffers and passes their addresses
    once; numpy wraps them as zero-copy views (ctypes.from_address), so the
    per-frame interop cost is one Python call.
    """

    def __init__(self, in_addr: int, out_addr: int, imgsz: int, device: str):
        import ctypes

        model.ensure_compiled(imgsz, device)
        self.device = device
        self._accel = None
        if device == "gpu":
            from max.driver import Accelerator

            self._accel = Accelerator()
        self.imgsz = imgsz
        self.na = model.num_anchors(imgsz)
        self.inp = np.ctypeslib.as_array(
            (ctypes.c_float * (imgsz * imgsz * 3)).from_address(in_addr)
        ).reshape(1, imgsz, imgsz, 3)
        self.out = np.ctypeslib.as_array(
            (ctypes.c_float * (84 * self.na)).from_address(out_addr)
        ).reshape(84, self.na)
        self.models = []
        for part in model.PARTS:
            session = model._session(device, precompiled_mefs=model._mef_dir(device, imgsz, part))
            factory = model._GraphFactory(imgsz, device)
            self.models.append(session.load(factory.build(part), weights_registry=factory.registry))
        # Warm-up execution so the first camera frame is not the slow one.
        self.infer()

    def infer(self) -> None:
        # execute() does not move host arrays to the GPU itself: the input
        # must arrive as a device Buffer, and intermediates stay on-device
        # through the chain; only the final decode output returns to host.
        if self._accel is not None:
            from max.driver import CPU, Buffer

            x = Buffer.from_numpy(self.inp).to(self._accel)
            p3, p4, p5 = self.models[0].execute(x)
            h15, h18, h21 = self.models[1].execute(p3, p4, p5)
            feats = self.models[2].execute(h15, h18, h21)[0]
            result = self.models[3].execute(feats)[0].to(CPU()).to_numpy()
        else:
            p3, p4, p5 = self.models[0].execute(self.inp)
            h15, h18, h21 = self.models[1].execute(p3, p4, p5)
            feats = self.models[2].execute(h15, h18, h21)[0]
            result = self.models[3].execute(feats)[0].to_numpy()
        np.copyto(self.out, result[0])


def main() -> None:
    device, imgsz = resolve_config()
    if (sys.argv[1] if len(sys.argv) > 1 else "") != "bench":
        print("usage: yolo_session.py bench [n]", file=sys.stderr)
        sys.exit(2)
    buf_in = np.zeros(imgsz * imgsz * 3, dtype=np.float32)
    buf_out = np.zeros(84 * model.num_anchors(imgsz), dtype=np.float32)
    s = Session(buf_in.ctypes.data, buf_out.ctypes.data, imgsz, device)
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 20
    t0 = time.perf_counter()
    for _ in range(n):
        s.infer()
    dt = (time.perf_counter() - t0) / n
    print(f"[model] {dt * 1000:.1f} ms/frame ({1 / dt:.1f} fps) device={device} imgsz={imgsz}")


if __name__ == "__main__":
    main()
