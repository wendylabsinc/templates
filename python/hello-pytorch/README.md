# Hello PyTorch

A simple Docker-based Python application that polls PyTorch every 2 seconds to check GPU and CPU availability.

## Features

- Checks CUDA (NVIDIA GPU) availability
- Checks MPS (Apple Silicon GPU) availability
- Displays CPU availability
- Shows PyTorch version
- Polls every 2 seconds

## Prerequisites

- Docker installed on your system

## Build and Run

### Build the Docker image (CPU-only, default):
```bash
docker build -t hello-pytorch .
```

### Run the container (CPU only):
```bash
docker run --rm hello-pytorch
```

### Run with NVIDIA GPU support (desktop/workstation):
```bash
docker run --rm --gpus all hello-pytorch
```

For NVIDIA Jetson devices, see [README-JETSON.md](README-JETSON.md) — it uses a separate `Dockerfile.jetson-slim`.

## Project Structure

- `Dockerfile` - CPU-only image (uv-based dependency install), used by default
- `Dockerfile.jetson-slim` - Jetson runtime image (requires a JetPack-matched `TORCH_WHL_URL` build-arg; see README-JETSON.md)
- `main.py` - Python script that polls PyTorch for device availability
- `pyproject.toml` - Python project configuration with PyTorch dependency
- `.dockerignore` - Files to exclude from Docker build context

## Output Example

```
============================================================
PyTorch Device Availability Checker
Polling every 2 seconds... (Press Ctrl+C to stop)
============================================================

[2025-10-31 10:30:00]
  CUDA (GPU) Available: True
  GPU Device Count: 1
    - Device 0: NVIDIA GeForce RTX 3080
  MPS (Apple GPU) Available: False
  CPU Available: True
  PyTorch Version: 2.0.0
```
