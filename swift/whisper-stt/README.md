# whisper-stt (Swift)

Headless speech-to-text in Swift. Captures audio from a USB microphone via
[gstreamer-swift](https://github.com/wendylabsinc/gstreamer-swift), transcribes
it continuously with [whisper.cpp](https://github.com/ggerganov/whisper.cpp)
(GPU-accelerated on NVIDIA Jetson), prints each transcription with a timestamp,
and appends it to a transcript file on a persistent volume.

The Swift port of `python/whisper-stt`. whisper.cpp is linked **in-process**
through a small C-interop target (`CWhisper`) — no shelling out.

## Deploy

```sh
wendy run --device <device> -y --detach
```

## See it work

```sh
wendy device logs --device <device>
```

Speak into the USB mic; you should see `[<timestamp>] <transcribed text>` lines.
The same lines are appended to `/data/transcript.txt`, which survives restarts.

## Configuration

| Variable            | Default                                   | Purpose                                             |
|---------------------|-------------------------------------------|-----------------------------------------------------|
| `WHISPER_MODEL`     | `base.en`                                 | GGML model baked into the image (template variable).|
| `WHISPER_MODEL_PATH`| `/opt/whisper/models/ggml-<model>.bin`    | Override the model file at runtime.                 |
| `WHISPER_LANGUAGE`  | `en`                                      | Transcription language.                             |
| `CHUNK_SECONDS`     | `5.0`                                      | Audio captured before each transcription.           |
| `SILENCE_THRESHOLD` | `0.01`                                     | RMS below which a chunk is skipped as silence.       |
| `TRANSCRIPT_FILE`   | `/data/transcript.txt`                    | Where transcriptions are appended (persistent).      |

## Compute

Built for NVIDIA Jetson: whisper.cpp is compiled with CUDA (`GGML_CUDA`) on the
same JetPack base image as the `llm-gguf` template. The image requires a
CUDA-capable runtime (any Jetson) to start; it is **not** a pure-CPU image and
will not run on a device with no CUDA runtime (e.g. a Raspberry Pi).
