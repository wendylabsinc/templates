# YOLOv8 on a USB camera, ONNX Runtime on the CPU.
#
# Two stages for one reason: `ultralytics` drags in torch, which is roughly 3 GB.
# It is needed once, to turn the .pt weights into .onnx, and never at run time.
# Keeping it in a throwaway stage takes the runtime image from ~3 GB to ~400 MB,
# which on a constrained link is the difference between an image that reaches
# the device and one that times out on the way.

# ---- exporter: weights -> onnx, then thrown away ----
FROM --platform=$BUILDPLATFORM python:3.11-slim-bookworm AS exporter

# Headless ultralytics still links the GUI stack during export.
RUN apt-get update && apt-get install -y --no-install-recommends \
      libgl1 libglib2.0-0 libxcb1 libsm6 libxext6 libxrender1 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir ultralytics

ARG YOLO_IMGSZ=320
# Export at the size we infer at: a fixed 320x320 graph avoids resizing work on
# every frame and is materially faster on CPU than the 640 default.
RUN python3 -c "\
from ultralytics import YOLO; \
YOLO('yolov8n.pt').export(format='onnx', imgsz=${YOLO_IMGSZ}, opset=12, simplify=False)" \
    && find / -name 'yolov8n.onnx' -exec cp {} /yolov8n.onnx \; \
    && test -s /yolov8n.onnx

# ---- runtime: no torch, no ultralytics ----
FROM python:3.11-slim-bookworm

# libgl1/libglib2.0-0 are needed by opencv even in its headless build.
RUN apt-get update && apt-get install -y --no-install-recommends \
      libgl1 libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
      fastapi \
      'uvicorn[standard]' \
      opencv-python-headless \
      onnxruntime \
      numpy

WORKDIR /app
COPY --from=exporter /yolov8n.onnx /app/yolov8n.onnx
COPY app.py index.html /app/
COPY assets/ /app/assets/

# Nothing here probes CUDA; say so explicitly so onnxruntime does not try.
ENV CUDA_VISIBLE_DEVICES="" \
    PYTHONUNBUFFERED=1 \
    YOLO_IMGSZ=320 \
    PORT={{.PORT}}

EXPOSE {{.PORT}}
CMD ["python3", "app.py"]
