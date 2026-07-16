# whisper-stt (Swift) — Design

Date: 2026-07-16
Branch: `jo/samples-consolidation`

## Goal

Port the Python `whisper-stt` template to Swift. It is a **headless**
speech-to-text app: capture audio from a USB microphone, transcribe it
continuously with [whisper.cpp](https://github.com/ggerganov/whisper.cpp)
(GPU-accelerated on NVIDIA Jetson), print each transcription with a timestamp,
and append it to a transcript file. There is no HTTP server — the payoff is
`wendy device logs` plus the transcript file, exactly like the Python original.

This is item #1 of the multi-template "port what's portable" sweep. It
establishes the reusable **whisper.cpp Swift binding** that item #2
(`voice-assistant`) will reuse.

## Binding approach (decisions locked during brainstorming)

- **Inference: in-process `libwhisper` via Swift C interop.** Build
  `libwhisper.so` in the Docker builder; expose `whisper.h` / `ggml.h` through a
  local `CWhisper` `.systemLibrary` target; call `whisper_full(...)` in-process.
  Chosen over shelling out to `whisper-cli` for lower latency (no per-chunk
  process spawn or WAV disk round-trip) and because it produces a genuine
  Swift↔whisper.cpp binding, paralleling the repo's other binding templates.
- **Compute: Jetson CUDA base with GPU-optional operation.** whisper.cpp is
  built with `-DGGML_CUDA=ON` on the same base image `llm-gguf` uses
  (`dustynv/tensorrt:8.6-r36.2.0`). See the CPU-fallback caveat below — this
  means "GPU-optional on a CUDA-capable base," not "runs on any device."

## Structure

Mirrors `swift/audio` (mic capture) and `swift/llm-gguf` (CUDA C++ inference lib
built in Docker):

```
swift/whisper-stt/
├── .swift-version                     # 6.2 (matches audio) — pinned to the toolchain the Dockerfile installs
├── template.json                      # vars: APP_ID (required), WHISPER_MODEL (default "base.en")
├── wendy.json                         # entitlements: audio, gpu, network:host, persistent volume for transcript
├── README.md
├── Package.swift                      # gstreamer-swift dep + local CWhisper systemLibrary target
├── Dockerfile                         # multi-stage: build libwhisper.so (GGML_CUDA) + model + swift build → slim runtime
└── Sources/
    ├── CWhisper/                      # module.modulemap + shim.h exposing whisper.h + ggml.h
    │   ├── module.modulemap
    │   └── shim.h
    └── whisper-stt/
        └── main.swift
```

(The rendered executable/target name is `{{.APP_ID}}`, following the `audio`
template's convention. The C target is fixed as `CWhisper`.)

## Audio capture

Reuse the `swift/audio` path verbatim — it already produces exactly Whisper's
required input format:

```swift
let source = try AudioSource.microphone()
    .withSampleRate(16_000)
    .withChannels(1)
    .withFormat(.s16le)
    .build()
```

An actor accumulates `s16le` buffers until it has `CHUNK_SECONDS` (default 5.0)
worth of samples, then:

1. Converts the interleaved `s16le` bytes to `[Float]` normalized to ±1.0
   (`Int16` / 32768.0).
2. Applies the same RMS silence gate as the Python (`rms < SILENCE_THRESHOLD`,
   default 0.01) — silent chunks are dropped without invoking Whisper.
3. Passes surviving chunks to the inference layer.

Microphone selection follows the `audio` template's ALSA convention: default
device unless overridden. USB-mic auto-detection (the Python's keyword match on
device names) is **not** ported — on a Wendy device the mic is injected via the
`audio` entitlement and the default capture device is the right one; keyword
heuristics are YAGNI here. (Documented as a deliberate simplification.)

## Inference (the C-interop core)

`Sources/CWhisper` is a `.systemLibrary` target:

- `shim.h` includes `<whisper.h>` and `<ggml.h>`.
- `module.modulemap` declares module `CWhisper` with `header "shim.h"` and
  `link "whisper"` (plus ggml link deps as needed; resolved during build
  validation).

`main.swift`:

- Once at startup: `whisper_init_from_file_with_params(modelPath, cparams)` with
  `cparams.use_gpu = true`.
- Per surviving chunk: fill a `whisper_full_params` (strategy
  `WHISPER_SAMPLING_GREEDY`), set `params.language` from `WHISPER_LANGUAGE`
  (default `en`), `params.print_realtime = false`, `params.print_progress =
  false`, then `whisper_full(ctx, params, samplesPtr, Int32(sampleCount))`.
- Collect text via `whisper_full_n_segments` +
  `whisper_full_get_segment_text(ctx, i)`, trim whitespace, and if non-empty:
  print `[<ISO-8601 timestamp>] <text>` to stdout and append the same line to
  the transcript file.
- `whisper_free(ctx)` on shutdown.

Concurrency: the whisper context is not thread-safe, so a single inference actor
owns `ctx` and processes chunks serially; capture and inference communicate over
an `AsyncStream`/bounded buffer so a slow transcription applies backpressure
rather than unbounded memory growth.

## whisper.cpp build (primary build risk)

Multi-stage Dockerfile:

1. **Builder** — base `dustynv/tensorrt:8.6-r36.2.0` (CUDA present), install the
   Swift 6.2.x toolchain the same way `llm-gguf` does (curl the swift.org
   aarch64 tarball), plus `cmake`, `git`, GStreamer dev libs
   (`libgstreamer1.0-dev`, `libgstreamer-plugins-base1.0-dev`).
   - **whisper.cpp:** pinned `git clone --depth 1 --branch <tag>
     https://github.com/ggerganov/whisper.cpp` (a pinned clone, **never** a
     vendored tarball — this is the explicit lesson from `llm-gguf`'s
     untracked `llama.cpp.tar.gz`). `cmake -DGGML_CUDA=ON
     -DCMAKE_CUDA_ARCHITECTURES=${CUDA_ARCH:-87} -DBUILD_SHARED_LIBS=ON
     -DWHISPER_BUILD_EXAMPLES=OFF -DWHISPER_BUILD_TESTS=OFF`, build the
     `whisper` library target. Install headers + `libwhisper.so`/`libggml*.so`
     to `/usr/local`.
   - **Model bake:** run whisper.cpp's `models/download-ggml-model.sh
     ${WHISPER_MODEL}` (default `base.en`) and place the resulting
     `ggml-<model>.bin` at a known image path (e.g.
     `/opt/whisper/models/ggml-${WHISPER_MODEL}.bin`).
   - `swift build -c release` the app against `CWhisper` (with
     `PKG_CONFIG_PATH`/`-Xcc -I`/`-Xlinker -L` pointing at `/usr/local` as
     needed — exact flags settled during build validation).
2. **Runtime** — slim same-family base carrying: the built `{{.APP_ID}}` binary,
   `libwhisper.so`/`libggml*.so`, the Swift runtime libs, the GStreamer runtime
   plugins (`gstreamer1.0-plugins-base/-good`, `gstreamer1.0-alsa`, `alsa-utils`
   — same runtime set as `swift/audio`), and the baked model file.

`ARG CUDA_ARCH=87` (Orin), overridable — matches `llm-gguf`.

## Model

Baked into the image at build time (see above). `WHISPER_MODEL` is both a
`template.json` variable and a Docker build `ARG`, default `base.en` (~142 MB).
At runtime, `WHISPER_MODEL_PATH` may point at a different model file (e.g. one
mounted from a volume) to override the baked default without a rebuild.

Rationale: deterministic, offline-ready, matches the edge-device story. The
alternative — download at first run into a persistent volume — was rejected as
more moving parts (network dependency at start, partial-download handling) for
no benefit given the small default model.

## Config surface

Driven entirely by environment variables (idiomatic for these templates; the
Python argparse CLI surface is dropped):

| Env var             | Default                     | Purpose                                             |
|---------------------|-----------------------------|-----------------------------------------------------|
| `WHISPER_MODEL`     | `base.en`                   | Model baked at build; informs default model path.   |
| `WHISPER_MODEL_PATH`| (baked model path)          | Override the model file at runtime.                 |
| `WHISPER_LANGUAGE`  | `en`                        | Whisper transcription language.                     |
| `CHUNK_SECONDS`     | `5.0`                       | Audio chunk length before each transcription.       |
| `SILENCE_THRESHOLD` | `0.01`                      | RMS below which a chunk is treated as silence.      |
| `TRANSCRIPT_FILE`   | `<persistent-vol>/transcript.txt` | Where transcriptions are appended.            |

Continuous mode only. The Python single-shot `--duration` mode is not ported
(YAGNI for a headless always-on device app).

## Transcript persistence

Deviation from the Python (which relied on a host `docker -v` mount that does
not map to Wendy): add a **persistent-volume entitlement** and default
`TRANSCRIPT_FILE` to a path on it, so the transcript survives app restarts
on-device. This is a small, justified improvement, not scope creep — it makes
"USB mic → transcript file" actually durable under the Wendy runtime.

## wendy.json

```jsonc
{
  "appId": "{{.APP_ID}}",
  "version": "0.1.0",
  "platform": "linux",
  "entitlements": [
    { "type": "network", "mode": "host" },
    { "type": "audio" },
    { "type": "gpu" }
    // + persistent volume entitlement mounting the transcript dir
    //   (exact schema copied from swift/sqlite-persistence / persistent-volume)
  ]
}
```

The persistent-volume entitlement's exact shape is taken from the existing
`persistent-volume` / `sqlite-persistence` templates during implementation
(same repo convention), not invented here. No `readiness` probe and no
`postStart` browser hook — this is a background/headless app.

## template.json

```json
{
  "name": "whisper-stt",
  "description": "Headless Whisper speech-to-text in Swift (whisper.cpp, GPU on Jetson): USB mic → transcript file",
  "variables": [
    { "name": "APP_ID", "description": "Application identifier", "type": "string", "required": true, "prompt": "App ID" },
    { "name": "WHISPER_MODEL", "description": "Whisper GGML model baked into the image", "type": "string", "default": "base.en", "prompt": "Whisper model" }
  ]
}
```

If a `template.schema.json` is shipped, `WHISPER_MODEL` must also appear in
`template.json` (per the known gotcha: a var only in the schema renders as
`<no value>`).

## CPU-fallback caveat (documented honestly)

A `GGML_CUDA` build still runs compute on CPU when no GPU is detected, **but**
the CUDA shared libraries must be loadable at process start. That holds on any
Jetson (CUDA runtime present) — it does **not** hold on a device with no CUDA
runtime at all (e.g. a Raspberry Pi). So "CPU fallback" here means
"GPU-optional on a CUDA-capable base," and the README will say exactly that
rather than implying it runs anywhere.

## Repo integration

- `meta.json`: `whisper-stt` entry gains Swift → `languages: ["python", "swift"]`.
- `README.md`: add a Swift row/section for `whisper-stt`.

## Validation

Templates cannot build in place (they hold `{{.APP_ID}}` / `{{.WHISPER_MODEL}}`
tokens). Per the repo's C++-interop-template lesson:

1. Render from **tracked files only** (`git archive HEAD:swift/whisper-stt`),
   substitute tokens into a scratchpad copy.
2. `docker build` — **the real test.** Expect to iterate here on: the
   `CWhisper` module map + link flags, the whisper.cpp CUDA cmake invocation,
   and which `libggml*.so` files the runtime stage needs.
3. Exercise it: feed a known-speech WAV through a `filesrc`-based test pipeline
   (or run on-device with a real USB mic) and confirm a transcription line
   appears both in `wendy device logs`/stdout and in the transcript file.

Only after a clean build + observed transcription: proceed to the PR for this
template.

## Out of scope (YAGNI)

- HTTP server / web UI (headless, like the Python).
- Single-shot `--duration` transcription mode.
- USB-mic name auto-detection heuristics.
- Non-CUDA (pure CPU) image variant — the Python targeted Jetson; a separate
  CPU-only variant can be a follow-up if a non-CUDA device needs it.
- Streaming partial results / word timestamps (chunked 5 s transcription
  matches the Python's behavior).
