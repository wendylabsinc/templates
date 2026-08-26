# syntax=docker/dockerfile:1.6
#
# Mojo + MAX camera-feed-yolo: wendycam capture + wendynet WebSockets (AOT
# Mojo binary) driving a hand-built YOLOv8n MAX graph through Python interop.
# One image serves every target — the MAX wheel selects CPU or GPU at runtime
# (same rationale as mojo/llm; the CUDA driver arrives via the gpu
# entitlement), so there is no base-image matrix.
#
# Model pipeline baked at build time (docs/mojo-max-port-findings.md):
#   - ONNX ingestion is gone from MAX 26.5 (MMF-002): the weights stage folds
#     the ultralytics checkpoint into plain numpy tensors instead.
#   - Graph compilation peaks at ~6-7 GB that the compiler never frees
#     (MMF-023): CPU MEF artifacts are compiled here, one part per process —
#     give the Docker VM >= 8 GB. GPU MEFs cannot be cross-compiled without
#     the driver; the app compiles them on first boot on the device.
#   - gcc stays in the runtime image: MAX JIT-links through the system C
#     compiler (MMF-017), which first-boot GPU compilation needs.

# ── weights: extract fused YOLOv8n tensors (torch only lives here) ───────────
FROM python:3.12-slim-bookworm AS weights
RUN --mount=type=cache,target=/root/.cache/pip pip install ultralytics
COPY extract_weights.py /build/
RUN mkdir /out && cd /build && python extract_weights.py /out

# ── mojo-build: AOT-compile the app binary ───────────────────────────────────
FROM python:3.12-slim-bookworm AS mojo-build
# gcc: the Mojo compiler uses the system C compiler as its linker.
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir modular==26.5.0
COPY app.mojo /build/
COPY wendynet /build/wendynet
COPY wendycam /build/wendycam
COPY wendyvision /build/wendyvision
WORKDIR /build
RUN mojo build -I /build -o /build/camera-feed-yolo app.mojo
# Collect the SDK runtime libs the binary links so LD_LIBRARY_PATH can stay
# narrow (the MAX venv itself is still required for the Python side).
RUN mkdir -p /build/rt \
    && ldd /build/camera-feed-yolo | awk '$3 ~ /site-packages/ {print $3}' \
       | xargs -r -I{} cp {} /build/rt/ \
    && ls -la /build/rt

# ── final: MAX runtime + binary + precompiled CPU MEFs ───────────────────────
FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libc6-dev \
    libturbojpeg0 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir modular==26.5.0

WORKDIR /app
COPY --from=weights /out/weights.npz /out/names.txt /app/
COPY --from=mojo-build /build/camera-feed-yolo /app/camera-feed-yolo
COPY --from=mojo-build /build/rt /app/rt
COPY model.py index.html /app/
COPY assets/ /app/assets/

# Surface the device capabilities the CLI passed in (informational; the app
# probes the MAX driver at runtime for the actual GPU decision).
ARG WENDY_PLATFORM=generic
ARG WENDY_HAS_GPU=""
ENV WENDY_PLATFORM=${WENDY_PLATFORM} \
    WENDY_HAS_GPU=${WENDY_HAS_GPU}

# MMF-004: the VMM defragmenting allocator crashes on integrated GPUs
# (Jetson); harmless on CPU-only devices.
ENV MODULAR_DEVICE_CONTEXT_MEMORY_MANAGER_VMM=0 \
    LD_LIBRARY_PATH=/app/rt \
    PYTHONUNBUFFERED=1 \
    PORT={{.PORT}}

# Precompile the CPU MEFs (imgsz 224 parity with python/camera-feed-yolo).
# One part per process; ~6-7 GB peak per part (MMF-023).
RUN YOLO_DEVICE=cpu python3 /app/model.py compile

EXPOSE {{.PORT}}
CMD ["/app/camera-feed-yolo"]
