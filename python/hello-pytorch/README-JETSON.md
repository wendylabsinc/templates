# Running on NVIDIA Jetson Orin Nano

This guide explains how to build and run this PyTorch project on the NVIDIA Jetson Orin Nano.

## Prerequisites

1. **Jetson Orin Nano** with JetPack SDK installed (JetPack 5.1.1 or later recommended)
2. **Docker** installed on the Jetson device
3. **NVIDIA Container Runtime** configured

## Setup NVIDIA Container Runtime

If not already configured, enable GPU support in Docker:

```bash
# Install nvidia-container-runtime if needed
sudo apt-get update
sudo apt-get install -y nvidia-container-runtime

# Configure Docker to use NVIDIA runtime
sudo nano /etc/docker/daemon.json
```

Add this configuration:
```json
{
    "runtimes": {
        "nvidia": {
            "path": "nvidia-container-runtime",
            "runtimeArgs": []
        }
    },
    "default-runtime": "nvidia"
}
```

Restart Docker:
```bash
sudo systemctl restart docker
```

## Build on Jetson Device

Build the Docker image directly on your Jetson Orin Nano using `Dockerfile.jetson-slim`. You must pass a `TORCH_WHL_URL` build-arg pointing at a torch wheel matching your JetPack version (see [NVIDIA's PyTorch for Jetson forum post](https://forums.developer.nvidia.com/t/pytorch-for-jetson/72048)):

```bash
docker build -f Dockerfile.jetson-slim \
  --build-arg TORCH_WHL_URL=https://developer.download.nvidia.com/compute/redist/jp/v51/pytorch/torch-2.0.0-cp38-cp38-linux_aarch64.whl \
  -t hello-pytorch-jetson .
```

## Run the Container

Run with GPU access:

```bash
docker run --runtime nvidia --rm -it hello-pytorch-jetson
```

Or with explicit GPU device access:

```bash
docker run --runtime nvidia --gpus all --rm -it hello-pytorch-jetson
```

## Expected Output

You should see output similar to:

```
============================================================
PyTorch Device Availability Checker
Polling every 2 seconds... (Press Ctrl+C to stop)
============================================================

[2025-10-31 12:00:00]
  CUDA (GPU) Available: True
  GPU Device Count: 1
    - Device 0: Orin
  MPS (Apple GPU) Available: False
  CPU Available: True
  PyTorch Version: 2.0.0
```

## Troubleshooting

### CUDA not detected

If CUDA is not detected, verify:

1. JetPack is properly installed:
   ```bash
   sudo apt-cache show nvidia-jetpack
   ```

2. NVIDIA runtime is working:
   ```bash
   docker run --runtime nvidia --rm nvcr.io/nvidia/l4t-base:r35.2.1 nvidia-smi
   ```

3. Check GPU status on host:
   ```bash
   sudo tegrastats
   ```

### Build fails or takes too long

`Dockerfile.jetson-slim` uses the lighter `l4t-base` image (no bundled PyTorch), but downloading the JetPack-matched torch wheel can still be slow. Ensure you have:
- At least a few GB free storage
- Good internet connection
- The correct `TORCH_WHL_URL` for your JetPack version — an empty/incorrect URL silently skips the torch install (see build log)

### Alternative: Use Pre-built PyTorch

If you need a different PyTorch version, NVIDIA provides wheels at:
https://forums.developer.nvidia.com/t/pytorch-for-jetson/72048

## Base Image Information

- **Base Image**: `nvcr.io/nvidia/l4t-base:r35.2.1`
- **Includes**: CUDA/cuDNN system libraries for Jetson; PyTorch is installed at build time from the wheel you supply via `TORCH_WHL_URL`
- **Architecture**: ARM64 (aarch64)
- **Compatible with**: JetPack 5.1+ (match the wheel to your JetPack/CUDA version)

## Performance Notes

The Jetson Orin Nano includes:
- NVIDIA Ampere GPU architecture
- Up to 1024 CUDA cores (depending on model)
- Optimized for AI inference workloads

This makes it excellent for running PyTorch models at the edge.
