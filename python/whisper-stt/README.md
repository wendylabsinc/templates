# Speech-to-Text for NVIDIA Jetson

A Docker-based speech-to-text application using OpenAI Whisper, optimized for NVIDIA Jetson devices with USB microphone support.

## Features

- Real-time speech-to-text transcription using Whisper
- GPU acceleration on NVIDIA Jetson (CUDA)
- USB microphone support via ALSA/PortAudio
- Continuous transcription mode
- Output to file for logging
- Multiple Whisper model sizes (tiny to large)

## Requirements

- NVIDIA Jetson Orin (Nano, NX, or AGX)
- JetPack 6.x with CUDA 12.6
- Docker with NVIDIA Container Toolkit
- USB microphone

## Quick Start

### Build the Docker Image

```bash
docker build -t speech-to-text .
```

### Run with USB Microphone

```bash
docker run -it --rm \
    --runtime nvidia \
    --device /dev/snd \
    --group-add audio \
    -v /tmp/.X11-unix:/tmp/.X11-unix \
    -e PULSE_SERVER=unix:/run/user/1000/pulse/native \
    -v /run/user/1000/pulse:/run/user/1000/pulse \
    speech-to-text
```

### Run with Output File

```bash
docker run -it --rm \
    --runtime nvidia \
    --device /dev/snd \
    --group-add audio \
    -v $(pwd)/output:/app/output \
    speech-to-text python3 speech_to_text.py --continuous --output /app/output/transcription.txt
```

## Usage

### Command Line Options

```
usage: speech_to_text.py [-h] [--model {tiny,base,small,medium,large}]
                         [--language LANGUAGE] [--device DEVICE]
                         [--list-devices] [--duration DURATION]
                         [--continuous] [--output OUTPUT]
                         [--silence-threshold SILENCE_THRESHOLD]

Options:
  --model, -m          Whisper model size (default: base)
  --language, -l       Language code (default: en)
  --device, -d         Audio input device ID
  --list-devices       List available audio devices
  --duration, -t       Recording chunk duration in seconds (default: 5.0)
  --continuous, -c     Run in continuous transcription mode
  --output, -o         Output file for transcriptions
  --silence-threshold  RMS threshold for silence detection (default: 0.01)
```

### Examples

**List available audio devices:**
```bash
docker run -it --rm --device /dev/snd speech-to-text \
    python3 speech_to_text.py --list-devices
```

**Single transcription (5 seconds):**
```bash
docker run -it --rm --runtime nvidia --device /dev/snd --group-add audio \
    speech-to-text python3 speech_to_text.py --duration 10
```

**Continuous mode with specific device:**
```bash
docker run -it --rm --runtime nvidia --device /dev/snd --group-add audio \
    speech-to-text python3 speech_to_text.py --continuous --device 2
```

**Use larger model for better accuracy:**
```bash
docker run -it --rm --runtime nvidia --device /dev/snd --group-add audio \
    -e WHISPER_MODEL=small \
    speech-to-text
```

## Whisper Models

| Model | Size | VRAM | Speed | Accuracy |
|-------|------|------|-------|----------|
| tiny | 39M | ~1GB | Fastest | Basic |
| base | 74M | ~1GB | Fast | Good |
| small | 244M | ~2GB | Medium | Better |
| medium | 769M | ~5GB | Slow | Great |
| large | 1550M | ~10GB | Slowest | Best |

For Jetson Orin Nano (8GB), recommended models: tiny, base, or small.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| WHISPER_MODEL | base | Whisper model to use |
| WHISPER_LANGUAGE | en | Target language for transcription |
| ALSA_CARD | 0 | ALSA sound card number |
| ALSA_DEVICE | 0 | ALSA device number |

## Troubleshooting

### No audio devices found

Ensure the USB microphone is connected and detected:
```bash
arecord -l  # List capture devices on host
```

Run container with full device access:
```bash
docker run -it --rm --privileged --device /dev/snd speech-to-text --list-devices
```

### CUDA not available

Ensure NVIDIA Container Toolkit is installed and working:
```bash
docker run --rm --runtime nvidia nvidia/cuda:12.6.0-base-ubuntu22.04 nvidia-smi
```

### Audio permission denied

Add user to audio group or run with `--privileged`:
```bash
sudo usermod -aG audio $USER
# Then logout and login again
```
