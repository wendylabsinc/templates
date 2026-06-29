"""Raw-TensorRT YOLOv8 inference for Jetson Orin (JetPack 7.2 / WendyOS).

Uses the CUDA 13.2 + cuDNN 9.20 + TensorRT 10.16 runtime that WendyOS injects via
the `gpu` entitlement. NO torch / NO Ultralytics at runtime — only the thin
`tensorrt` (cu13) + `cuda-python` bindings, plus numpy and OpenCV.

Why this exists: the old `dustynv/pytorch:*-cu128` (JetPack 6) base can't init CUDA
against a JetPack 7.2 host (`cudaGetDeviceCount -> Error 801`) and silently falls
back to CPU. Instead of chasing a matching torch wheel, we ride the injected
TensorRT directly. See JETSON_JP7_GPU.md / WDY-1752.

The engine is built on first run from a baked `fire.onnx` (trtexec is NOT
injected, so we use the TensorRT Python Builder API) and cached for reuse.

`detect(frame_bgr, conf) -> list[dict]` returns boxes in the same shape app.py's
loop builds: {x1,y1,x2,y2,conf,cls,name}.

NB: written for TRT 10.x (execute_async_v3 / tensor-address API) and cuda-python
13.x (cuda.bindings.runtime). Validate box geometry on-device after first deploy.
"""
from __future__ import annotations

import logging
from pathlib import Path

import cv2
import numpy as np

logger = logging.getLogger("trt_yolo")

# cuda-python moved the runtime module across versions; support both.
try:
    from cuda.bindings import runtime as cudart  # cuda-python >= 12.8 / 13.x
except Exception:  # pragma: no cover
    from cuda import cudart  # older layout

# Custom fire-detection model classes — index order matches fire.pt -> fire.onnx.
FIRE_NAMES = ["fire", "other", "smoke"]


def _ck(ret):
    """Unpack a cuda-python call result (err, *vals); raise on non-zero err."""
    if not isinstance(ret, (tuple, list)):
        ret = (ret,)
    err, *vals = ret
    if int(err) != 0:
        try:
            _, msg = cudart.cudaGetErrorString(err)
            msg = msg.decode() if isinstance(msg, (bytes, bytearray)) else str(msg)
        except Exception:
            msg = str(err)
        raise RuntimeError(f"CUDA error {int(err)}: {msg}")
    if not vals:
        return None
    return vals[0] if len(vals) == 1 else tuple(vals)


class TRTYolo:
    """YOLOv8 inference on the injected TensorRT runtime. Mirrors the bits of the
    Ultralytics interface app.py uses: `.names` and a detect() that returns boxes."""

    def __init__(self, onnx_path, engine_path, imgsz=640, conf=0.25, iou=0.45, fp16=True):
        import tensorrt as trt  # lazy: needs injected libnvinfer at runtime, absent at build

        self.trt = trt
        self.imgsz = int(imgsz)
        self.conf = float(conf)
        self.iou = float(iou)
        self.names = FIRE_NAMES
        self.trt_logger = trt.Logger(trt.Logger.WARNING)

        self.engine = self._load_or_build(onnx_path, engine_path, fp16)
        self.context = self.engine.create_execution_context()

        self.input_name = self.output_name = None
        for i in range(self.engine.num_io_tensors):
            name = self.engine.get_tensor_name(i)
            if self.engine.get_tensor_mode(name) == trt.TensorIOMode.INPUT:
                self.input_name = name
            else:
                self.output_name = name

        self.context.set_input_shape(self.input_name, (1, 3, self.imgsz, self.imgsz))
        self.out_shape = tuple(self.context.get_tensor_shape(self.output_name))

        self.stream = _ck(cudart.cudaStreamCreate())
        self.in_nbytes = int(np.prod((1, 3, self.imgsz, self.imgsz))) * 4
        self.out_nbytes = int(np.prod(self.out_shape)) * 4
        self.d_in = _ck(cudart.cudaMalloc(self.in_nbytes))
        self.d_out = _ck(cudart.cudaMalloc(self.out_nbytes))
        self.context.set_tensor_address(self.input_name, int(self.d_in))
        self.context.set_tensor_address(self.output_name, int(self.d_out))
        logger.info("TRTYolo ready: in=%s out=%s imgsz=%d", self.input_name, self.out_shape, self.imgsz)

    def _load_or_build(self, onnx_path, engine_path, fp16):
        trt = self.trt
        runtime = trt.Runtime(self.trt_logger)
        ep = Path(engine_path)
        if ep.exists():
            logger.info("Loading cached TRT engine %s", ep)
            engine = runtime.deserialize_cuda_engine(ep.read_bytes())
            if engine is not None:
                return engine
            logger.warning("Cached engine failed to deserialize; rebuilding")

        logger.info("Building TRT engine from %s (first run, may take a few minutes)...", onnx_path)
        builder = trt.Builder(self.trt_logger)
        network = builder.create_network(0)  # TRT 10: networks are explicit-batch by default
        parser = trt.OnnxParser(network, self.trt_logger)
        with open(onnx_path, "rb") as f:
            if not parser.parse(f.read()):
                errs = "; ".join(str(parser.get_error(i)) for i in range(parser.num_errors))
                raise RuntimeError(f"ONNX parse failed: {errs}")
        config = builder.create_builder_config()
        # Keep the build cheap: the Orin Nano shares 8GB CPU/GPU RAM, and a large
        # workspace makes the (FP16) build spike memory and get OOM-restarted before
        # it can finish + cache. 256MB is plenty for yolov8n.
        config.set_memory_pool_limit(trt.MemoryPoolType.WORKSPACE, 1 << 28)
        if fp16 and builder.platform_has_fast_fp16:
            config.set_flag(trt.BuilderFlag.FP16)
            logger.info("FP16 enabled")
        serialized = builder.build_serialized_network(network, config)
        if serialized is None:
            raise RuntimeError("TRT build_serialized_network returned None")
        try:
            ep.parent.mkdir(parents=True, exist_ok=True)
            ep.write_bytes(serialized)
            logger.info("Cached engine -> %s", ep)
        except Exception as e:
            logger.warning("Could not cache engine (%s); will rebuild next start", e)
        return runtime.deserialize_cuda_engine(serialized)

    def _letterbox(self, img):
        h, w = img.shape[:2]
        r = min(self.imgsz / h, self.imgsz / w)
        nh, nw = int(round(h * r)), int(round(w * r))
        resized = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
        canvas = np.full((self.imgsz, self.imgsz, 3), 114, dtype=np.uint8)
        top, left = (self.imgsz - nh) // 2, (self.imgsz - nw) // 2
        canvas[top:top + nh, left:left + nw] = resized
        return canvas, r, left, top

    def detect(self, frame_bgr, conf=None):
        conf = self.conf if conf is None else float(conf)
        canvas, r, dx, dy = self._letterbox(frame_bgr)
        rgb = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        inp = np.ascontiguousarray(np.transpose(rgb, (2, 0, 1))[None])  # 1x3xHxW

        _ck(cudart.cudaMemcpyAsync(int(self.d_in), inp.ctypes.data, self.in_nbytes,
                                   cudart.cudaMemcpyKind.cudaMemcpyHostToDevice, self.stream))
        self.context.execute_async_v3(self.stream)
        out = np.empty(self.out_shape, dtype=np.float32)
        _ck(cudart.cudaMemcpyAsync(out.ctypes.data, int(self.d_out), self.out_nbytes,
                                   cudart.cudaMemcpyKind.cudaMemcpyDeviceToHost, self.stream))
        _ck(cudart.cudaStreamSynchronize(self.stream))
        return self._postprocess(out, frame_bgr.shape[:2], r, dx, dy, conf)

    def _postprocess(self, out, orig_hw, r, dx, dy, conf):
        o = out[0]
        if o.shape[0] < o.shape[1]:  # (84, 8400) -> (8400, 84)
            o = o.transpose(1, 0)
        boxes_xywh, scores_all = o[:, :4], o[:, 4:]
        class_ids = np.argmax(scores_all, axis=1)
        confs = scores_all[np.arange(scores_all.shape[0]), class_ids]
        keep = confs > conf
        boxes_xywh, confs, class_ids = boxes_xywh[keep], confs[keep], class_ids[keep]
        if boxes_xywh.shape[0] == 0:
            return []

        cx, cy, bw, bh = boxes_xywh.T
        x1, y1 = (cx - bw / 2 - dx) / r, (cy - bh / 2 - dy) / r
        x2, y2 = (cx + bw / 2 - dx) / r, (cy + bh / 2 - dy) / r
        H, W = orig_hw
        x1, x2 = np.clip(x1, 0, W), np.clip(x2, 0, W)
        y1, y2 = np.clip(y1, 0, H), np.clip(y2, 0, H)

        nms_boxes = np.stack([x1, y1, x2 - x1, y2 - y1], axis=1).tolist()
        idxs = cv2.dnn.NMSBoxes(nms_boxes, confs.tolist(), conf, self.iou)
        dets = []
        for i in (np.array(idxs).flatten() if len(idxs) else []):
            cid = int(class_ids[i])
            dets.append({
                "x1": float(x1[i]), "y1": float(y1[i]),
                "x2": float(x2[i]), "y2": float(y2[i]),
                "conf": float(confs[i]), "cls": cid,
                "name": self.names[cid] if cid < len(self.names) else str(cid),
            })
        return dets
