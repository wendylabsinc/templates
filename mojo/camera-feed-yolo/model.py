# YOLOv8n hand-built as a MAX graph (no ONNX: MAX 26.5 cannot ingest it —
# docs/mojo-max-port-findings.md MMF-001/002). The Mojo app drives this module
# through Python interop (MMF-012): weights come from a build-time extraction
# of the ultralytics checkpoint (weights.npz), inference runs from MEF
# artifacts precompiled one graph per process because a single compile peaks
# at 5.7-7.2 GB that the 26.5 compiler never frees (MMF-023).
#
# Four graphs, chained at execute time (split keeps every part's compile
# peak comfortably inside an 8 GB Docker VM — a combined PAN+detect part
# peaked at 7.2 GB and got OOM-killed on marginal builders):
#   backbone: (1, S, S, 3)   -> P3, P4, P5 feature maps
#   pan:      P3, P4, P5     -> H15, H18, H21 (FPN/PAN feature maps)
#   detect:   H15, H18, H21  -> (1, NA, 144) raw head output
#   decode:   (1, NA, 144)   -> (1, 84, NA) xywh(px) + class scores
# Postprocessing (confidence filter, NMS, unletterboxing) stays in Mojo.
import os
import subprocess
import sys

import numpy as np

APP_DIR = os.path.dirname(os.path.abspath(__file__))
WEIGHTS_PATH = os.path.join(APP_DIR, "weights.npz")

NC = 80
REG_MAX = 16
STRIDES = (8, 16, 32)
PARTS = ("backbone", "pan", "detect", "decode")


def num_anchors(imgsz: int) -> int:
    return sum((imgsz // s) ** 2 for s in STRIDES)


def _gpu_arch() -> str:
    """Target GPU arch: the physical device's when one is present, else the
    YOLO_GPU_ARCH build/run arg (sm_87 = Jetson Orin default)."""
    try:
        from max import driver

        if driver.accelerator_count() > 0:
            return driver.accelerator_architecture_name()
    except Exception:
        pass
    return os.environ.get("YOLO_GPU_ARCH", "sm_87")


def _enable_virtual_gpu_if_needed() -> None:
    """GPU MEFs can be cross-compiled on a machine with no GPU: the driver's
    virtual-device mode targets a named arch (undocumented but working —
    docs/mojo-max-port-findings.md MMF-023 notes). The Dockerfile uses this
    to bake Jetson MEFs at image-build time."""
    from max import driver

    try:
        if driver.accelerator_count() > 0:
            return
    except Exception:
        pass
    arch = _gpu_arch()
    driver.set_virtual_device_api("cuda")
    driver.set_virtual_device_target_arch(arch)
    driver.set_virtual_device_count(1)
    print(f"[model] no physical GPU — cross-compiling via virtual device ({arch})", flush=True)


def _mef_dir(device: str, imgsz: int, part: str) -> str:
    tag = f"gpu-{_gpu_arch()}-{imgsz}" if device == "gpu" else f"cpu-{imgsz}"
    return os.path.join(APP_DIR, "mefs", tag, part)


def _session(device: str, **kwargs):
    from max.engine import InferenceSession

    if device == "gpu":
        from max.driver import Accelerator

        return InferenceSession(devices=[Accelerator()], **kwargs)
    return InferenceSession(**kwargs)


class _GraphFactory:
    """Builds the three YOLOv8n graphs from the extracted weight archive.

    Serve and compile MUST build byte-identical graphs: MEF reuse matches the
    rebuilt graph against the precompiled artifact at load time.
    """

    def __init__(self, imgsz: int, device: str):
        from max.graph import DeviceRef

        self.imgsz = imgsz
        self.device = device
        self.dev = DeviceRef.GPU() if device == "gpu" else DeviceRef.CPU()
        self.weights = {k: v for k, v in np.load(WEIGHTS_PATH).items()}
        self.registry = {}

    def _const(self, a):
        from max.dtype import DType
        from max.graph import ops

        return ops.constant(np.ascontiguousarray(a), DType.float32, device=self.dev)

    def _ext(self, name, a):
        # Weights ride the load-time registry: inlined ops.constant weights
        # OOM the constant-folder during compilation (MMF-023).
        from max.dtype import DType
        from max.graph import TensorType, ops

        a = np.ascontiguousarray(a)
        self.registry[name] = a
        return ops.constant_external(name, TensorType(DType.float32, a.shape, device=self.dev))

    def _conv_raw(self, x, wname, bname, stride=1, act=True):
        # On CPU, 1x1 convs are lowered to matmul: conv2d's RSCF->KNkni filter
        # repack has no registered CPU kernel in 26.5 (MMF-021). On GPU the
        # conv path works — and must be used: the matmul route's KN->NK weight
        # repack crashes with CUDA_ERROR_ILLEGAL_ADDRESS at model setup on the
        # Jetson iGPU (MMF-021 notes).
        from max.graph import ops

        w = self.weights[wname]
        k = w.shape[2]
        if k == 1 and stride == 1 and self.device == "cpu":
            _, h, wd, c = (int(d) for d in x.shape)
            y = ops.reshape(x, (h * wd, c))
            y = ops.add(
                ops.matmul(y, self._ext(wname + ":io", w[:, :, 0, 0].transpose(1, 0))),
                self._ext(bname, self.weights[bname]),
            )
            y = ops.reshape(y, (1, h, wd, w.shape[0]))
        else:
            pad = k // 2
            y = ops.conv2d(
                x,
                self._ext(wname + ":rscf", w.transpose(2, 3, 1, 0)),
                stride=(stride, stride),
                padding=(pad, pad, pad, pad),
                bias=self._ext(bname, self.weights[bname]),
            )
        return ops.silu(y) if act else y

    def _conv(self, x, name, stride=1):
        return self._conv_raw(x, f"{name}.conv.weight", f"{name}.conv.bias", stride=stride)

    def _bottleneck(self, x, name, shortcut):
        from max.graph import ops

        y = self._conv(x, f"{name}.cv1")
        y = self._conv(y, f"{name}.cv2")
        return ops.add(x, y) if shortcut else y

    def _c2f(self, x, name, n, shortcut):
        from max.graph import ops

        y = self._conv(x, f"{name}.cv1")
        a, b = ops.chunk(y, 2, axis=3)
        outs = [a, b]
        cur = b
        for j in range(n):
            cur = self._bottleneck(cur, f"{name}.m.{j}", shortcut)
            outs.append(cur)
        return self._conv(ops.concat(outs, axis=3), f"{name}.cv2")

    def _sppf(self, x, name):
        from max.graph import ops

        y = self._conv(x, f"{name}.cv1")
        p1 = ops.max_pool2d(y, (5, 5), stride=1, padding=2)
        p2 = ops.max_pool2d(p1, (5, 5), stride=1, padding=2)
        p3 = ops.max_pool2d(p2, (5, 5), stride=1, padding=2)
        return self._conv(ops.concat([y, p1, p2, p3], axis=3), f"{name}.cv2")

    def _upsample2x(self, x):
        # torch nearest 2x == pixel duplication; resize_nearest's coordinate
        # modes do not reproduce it exactly (MMF-022), broadcast does.
        from max.graph import ops

        _, h, w, c = (int(d) for d in x.shape)
        y = ops.reshape(x, (1, h, 1, w, 1, c))
        y = ops.broadcast_to(y, (1, h, 2, w, 2, c))
        return ops.reshape(y, (1, h * 2, w * 2, c))

    def _detect_level(self, x, name, lvl):
        from max.graph import ops

        box = self._conv(x, f"{name}.cv2.{lvl}.0")
        box = self._conv(box, f"{name}.cv2.{lvl}.1")
        box = self._conv_raw(box, f"{name}.cv2.{lvl}.2.weight", f"{name}.cv2.{lvl}.2.bias", act=False)
        cls = self._conv(x, f"{name}.cv3.{lvl}.0")
        cls = self._conv(cls, f"{name}.cv3.{lvl}.1")
        cls = self._conv_raw(cls, f"{name}.cv3.{lvl}.2.weight", f"{name}.cv3.{lvl}.2.bias", act=False)
        out = ops.concat([box, cls], axis=3)
        hw = (self.imgsz // STRIDES[lvl]) ** 2
        return ops.reshape(out, (1, hw, 4 * REG_MAX + NC))

    def build(self, part: str):
        from max.dtype import DType
        from max.graph import Graph, TensorType, ops

        s = self.imgsz
        if part == "backbone":
            with Graph(
                "yolov8n_backbone",
                input_types=[TensorType(DType.float32, (1, s, s, 3), device=self.dev)],
            ) as g:
                x = g.inputs[0]
                x = self._conv(x, "model.0", stride=2)
                x = self._conv(x, "model.1", stride=2)
                x = self._c2f(x, "model.2", 1, True)
                x = self._conv(x, "model.3", stride=2)
                p3 = self._c2f(x, "model.4", 2, True)
                x = self._conv(p3, "model.5", stride=2)
                p4 = self._c2f(x, "model.6", 2, True)
                x = self._conv(p4, "model.7", stride=2)
                x = self._c2f(x, "model.8", 1, True)
                p5 = self._sppf(x, "model.9")
                g.output(p3, p4, p5)
            return g
        if part == "pan":
            with Graph(
                "yolov8n_pan",
                input_types=[
                    TensorType(DType.float32, (1, s // 8, s // 8, 64), device=self.dev),
                    TensorType(DType.float32, (1, s // 16, s // 16, 128), device=self.dev),
                    TensorType(DType.float32, (1, s // 32, s // 32, 256), device=self.dev),
                ],
            ) as g:
                p3, p4, p5 = g.inputs
                u = self._upsample2x(p5)
                h12 = self._c2f(ops.concat([u, p4], axis=3), "model.12", 1, False)
                u = self._upsample2x(h12)
                h15 = self._c2f(ops.concat([u, p3], axis=3), "model.15", 1, False)
                d = self._conv(h15, "model.16", stride=2)
                h18 = self._c2f(ops.concat([d, h12], axis=3), "model.18", 1, False)
                d = self._conv(h18, "model.19", stride=2)
                h21 = self._c2f(ops.concat([d, p5], axis=3), "model.21", 1, False)
                g.output(h15, h18, h21)
            return g
        if part == "detect":
            with Graph(
                "yolov8n_detect",
                input_types=[
                    TensorType(DType.float32, (1, s // 8, s // 8, 64), device=self.dev),
                    TensorType(DType.float32, (1, s // 16, s // 16, 128), device=self.dev),
                    TensorType(DType.float32, (1, s // 32, s // 32, 256), device=self.dev),
                ],
            ) as g:
                h15, h18, h21 = g.inputs
                feats = ops.concat(
                    [
                        self._detect_level(h15, "model.22", 0),
                        self._detect_level(h18, "model.22", 1),
                        self._detect_level(h21, "model.22", 2),
                    ],
                    axis=1,
                )
                g.output(feats)
            return g
        if part == "decode":
            na = num_anchors(s)
            pts, strides = [], []
            for st in STRIDES:
                n = s // st
                sx = np.arange(n, dtype=np.float32) + 0.5
                gy, gx = np.meshgrid(sx, sx, indexing="ij")
                pts.append(np.stack((gx, gy), -1).reshape(-1, 2))
                strides.append(np.full((n * n, 1), st, dtype=np.float32))
            anchors_np = np.concatenate(pts)
            strides_np = np.concatenate(strides)
            with Graph(
                "yolov8n_decode",
                input_types=[TensorType(DType.float32, (1, na, 4 * REG_MAX + NC), device=self.dev)],
            ) as g:
                feats = g.inputs[0]
                box, cls = ops.split(feats, [4 * REG_MAX, NC], axis=2)
                b = ops.softmax(ops.reshape(box, (1, na, 4, REG_MAX)))
                proj = self._const(np.arange(REG_MAX, dtype=np.float32).reshape(1, 1, 1, REG_MAX))
                d4 = ops.reshape(ops.sum(ops.mul(b, proj), axis=3), (1, na, 4))
                lt, rb = ops.split(d4, [2, 2], axis=2)
                anchor = self._const(anchors_np.reshape(1, na, 2))
                stride_t = self._const(strides_np.reshape(1, na, 1))
                x1y1 = ops.sub(anchor, lt)
                x2y2 = ops.add(anchor, rb)
                cxy = ops.mul(ops.add(x1y1, x2y2), self._const(np.float32(0.5)))
                wh = ops.sub(x2y2, x1y1)
                bbox = ops.mul(ops.concat([cxy, wh], axis=2), stride_t)
                out = ops.concat([bbox, ops.sigmoid(cls)], axis=2)
                g.output(ops.permute(out, [0, 2, 1]))
            return g
        raise ValueError(f"unknown part {part!r}")


def compile_part(part: str, imgsz: int, device: str) -> None:
    if device == "gpu":
        _enable_virtual_gpu_if_needed()
    out_dir = _mef_dir(device, imgsz, part)
    os.makedirs(out_dir, exist_ok=True)
    session = _session(device, export_mefs=out_dir)
    factory = _GraphFactory(imgsz, device)
    graph = factory.build(part)
    session.load(graph, weights_registry=factory.registry)
    print(f"[model] compiled {part} (device={device}, imgsz={imgsz}) -> {out_dir}", flush=True)


def ensure_compiled(imgsz: int, device: str) -> None:
    """Compile any missing MEF, one subprocess per part: compiler memory is
    only reclaimed at process exit (MMF-023)."""
    for part in PARTS:
        d = _mef_dir(device, imgsz, part)
        if os.path.isdir(d) and any(f.endswith(".mef") for f in os.listdir(d)):
            continue
        print(f"[model] MEF for {part} missing — compiling (one-time, needs ~6-7 GB RAM)", flush=True)
        subprocess.run(
            [sys.executable, os.path.abspath(__file__), "compile", part],
            check=True,
            env={**os.environ, "YOLO_IMGSZ": str(imgsz), "YOLO_DEVICE": device},
        )



def main() -> None:
    # Compile-only CLI (Docker build stage; the serve runtime lives in
    # yolo_session.py, which is layered AFTER the MEF compiles so runtime
    # changes don't invalidate them). Defaults mirror yolo_session's
    # resolve_config, minus the driver probe: compile targets are explicit.
    device = os.environ.get("YOLO_DEVICE", "cpu")
    imgsz_env = os.environ.get("YOLO_IMGSZ", "")
    imgsz = int(imgsz_env) if imgsz_env else (320 if device == "gpu" else 224)
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "compile":
        if len(sys.argv) > 2:
            compile_part(sys.argv[2], imgsz, device)
        else:
            ensure_compiled(imgsz, device)
    else:
        print("usage: model.py compile [part]", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
