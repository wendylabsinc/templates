FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-venv \
    python3-gi \
    gir1.2-gst-plugins-base-1.0 \
    gir1.2-gst-plugins-bad-1.0 \
    gir1.2-gstreamer-1.0 \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    gstreamer1.0-libav \
    gstreamer1.0-nice \
    v4l-utils \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN python3 -m venv --system-site-packages /app/venv
ENV PATH="/app/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Export YOLOv8n to ONNX for faster CPU inference (and install onnxruntime)
RUN pip install --no-cache-dir onnxruntime
RUN python3 -c "from ultralytics import YOLO; YOLO('yolov8n.pt').export(format='onnx', imgsz=320)"

COPY app.py index.html ./
COPY assets/ ./assets/

ENV PYTHONUNBUFFERED=1

EXPOSE {{.PORT}}

CMD ["/app/venv/bin/uvicorn", "app:app", "--host", "0.0.0.0", "--port", "{{.PORT}}"]
