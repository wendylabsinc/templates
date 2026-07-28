# syntax=docker/dockerfile:1.6
# Stage 1 — Build React frontend
FROM node:22-slim AS frontend
WORKDIR /frontend
COPY frontend/package*.json ./
RUN --mount=type=cache,target=/root/.npm npm install
COPY frontend/ ./
RUN npm run build

# Stage 2 — FastAPI backend (CPU-only DSP, no GStreamer)
FROM python:3.12-slim-bookworm
WORKDIR /app
ENV PYTHONUNBUFFERED=1

ARG WENDY_DEVICE_HOSTNAME=localhost
ENV WENDY_DEVICE_HOSTNAME=${WENDY_DEVICE_HOSTNAME}

RUN python3 -m venv /app/venv
ENV PATH="/app/venv/bin:$PATH"

COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

COPY app/ ./app/
COPY mcp/ ./mcp/
COPY start.sh ./start.sh
RUN chmod +x start.sh
COPY --from=frontend /frontend/dist ./static

ARG WENDY_DEVICE_TYPE
ARG WENDY_DEBUG=false
ENV WENDY_DEVICE_TYPE=${WENDY_DEVICE_TYPE}
ENV WENDY_DEBUG=${WENDY_DEBUG}
RUN --mount=type=cache,target=/root/.cache/pip \
    if [ "$WENDY_DEBUG" = "true" ]; then pip install debugpy; fi

EXPOSE {{.PORT}}
EXPOSE 8000

CMD ["/app/start.sh"]
