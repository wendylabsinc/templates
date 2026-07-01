# syntax=docker/dockerfile:1.6
# Stage 1 — Build React frontend
FROM node:22-slim AS frontend
WORKDIR /frontend
COPY frontend/package*.json ./
RUN --mount=type=cache,target=/root/.npm npm install
COPY frontend/ ./
RUN npm run build

# Stage 2 — FastAPI backend with GStreamer for camera/audio
FROM debian:bookworm-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1

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
    gstreamer1.0-alsa \
    alsa-utils \
    v4l-utils \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv --system-site-packages /app/venv
ENV PATH="/app/venv/bin:$PATH"

COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

COPY app/ ./app/
COPY --from=frontend /frontend/dist ./static

ARG WENDY_DEVICE_TYPE
ARG WENDY_DEBUG=false
ENV WENDY_DEVICE_TYPE=${WENDY_DEVICE_TYPE}
ENV WENDY_DEBUG=${WENDY_DEBUG}
RUN --mount=type=cache,target=/root/.cache/pip \
    if [ "$WENDY_DEBUG" = "true" ]; then pip install debugpy; fi

EXPOSE {{.PORT}}

CMD ["/app/venv/bin/uvicorn", "app:api", "--host", "0.0.0.0", "--port", "{{.PORT}}"]
