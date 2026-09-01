# voice-ai-pipecat (Mojo/MAX)

Always-on voice AI assistant with a **local, MAX-served LLM**: local
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) STT → **`max serve`**
(Modular's OpenAI-compatible serving layer, GPU or CPU) → local
[Piper](https://github.com/rhasspy/piper) TTS, orchestrated by
[Pipecat](https://github.com/pipecat-ai/pipecat). The MAX port of
`python/voice-ai-pipecat` — same app, same React visualizer, with the cloud-LLM
default swapped for an on-device model. Cloud providers (Gemini / OpenAI /
Anthropic / Groq) stay available from the settings drawer; drop in an API key and
switch per-turn.

Two services (host networking, one app group):

- `voice-app/` — the Pipecat app from the python sibling, vendored with a
  four-line seam: `LLM_PROVIDER` / `LLM_MODEL` / `LLM_BASE_URL` /
  `OLLAMA_HEALTH_URL` env hooks point the existing OpenAI-compatible local
  provider path at max-serve. Serves the UI on `{{.PORT}}` (HTTPS, self-signed —
  browsers gate mic access behind a secure origin).
- `max-serve/` — vendored from `mojo/llm/max-serve`; serves `MAX_MODEL` on
  `:9012` (`/v1` OpenAI API; 9011 stays free so a `mojo/llm` group can co-exist
  on the same device).

## Why this is the "scoped v1" port (findings doc §MMF-002/003)

A full-Mojo/MAX voice pipeline is not feasible on MAX 26.5, and that is itself
the point of this port — each leg that stays Python maps to a filed finding in
`docs/mojo-max-port-findings.md`:

| Leg | Stays | Why |
| --- | --- | --- |
| LLM | **→ MAX** (`max serve`) | Works — this template. |
| STT | faster-whisper (CPU int8) | No speech modality in MAX; Whisper is in-tree but unpublished (MMF-003). |
| TTS | Piper (ONNX, CPU) | ONNX ingestion removed from MAX, no path for Piper voices (MMF-002). |
| Wake word | openWakeWord (ONNX) | Same ONNX gap (MMF-002). |
| Orchestration | Pipecat (Python) | Python-ecosystem framework; MAX Graph API is Python-only anyway (MMF-012). |

## Model sizing

| Device | Suggested MAX_MODEL |
| --- | --- |
| Jetson Orin Nano 8 GB | `Qwen/Qwen2.5-0.5B-Instruct` (default, verified) or `HuggingFaceTB/SmolLM2-135M-Instruct` (fastest, toy-grade answers). **Not 1.5B**: 2.88 GiB bf16 fails its memory plan at 0.6×free with the voice app resident (MMF-006 addendum) |
| Jetson AGX Orin / Thor (16 GB+) | `Qwen/Qwen2.5-1.5B-Instruct` up to `Qwen2.5-7B`-class bf16 models |
| CPU-only (RPi 5 etc.) | `modularai/SmolLM-135M-Instruct-FP32` — bf16 repos cannot serve on CPU in MAX 26.5 (MMF-016) |

A voice loop wants time-to-first-token more than throughput: the app streams LLM
output sentence-by-sentence into Piper, so TTFT ≈ added conversational latency.
Gated models (Llama) need `HF_TOKEN` at scaffold time.

## Jetson notes

All of `mojo/llm`'s hard-won serving flags are inherited by the vendored
`max-serve/` (VMM=0 + `--no-device-graph-capture` for the iGPU crash MMF-004,
explicit batch/length/memory for the unified-memory auto-tuner MMF-006, warm
compile cache on the `-llm-models` volume MMF-018, `HF_HUB_OFFLINE` fallback for
offline boots MMF-019, app-level `network` entitlement for group containers —
findings doc Appendix W). First boot downloads + graph-compiles the model; the
voice UI comes up immediately and LLM turns start succeeding once max-serve
finishes compiling (watch `/api/status` — the health banner clears).

## Status

Container-verified 2026-08-31 (Apple Silicon host, MAX 26.5.0):

- both service images build (frontend npm build, full pip stack, model seeds);
- `voice-app` boots to HTTPS with no audio hardware attached, `/api/settings`
  shows the env-hook defaults (`llm_provider=ollama`,
  `llm_model=Qwen/Qwen2.5-1.5B-Instruct`, MAX model first in the local picker);
- the health watcher polls `:9012/v1/models` — red banner with the server down,
  clears against a live OpenAI-compatible server;
- the vendored `max-serve` container serves chat completions on `:9012`
  (`modularai/SmolLM-135M-Instruct-FP32` on container CPU);
- the default model `Qwen/Qwen2.5-1.5B-Instruct` (bf16) validated under
  `max serve` on the Apple GPU: ~24 tok/s decode, TTFT 0.2 s, coherent output
  (findings doc MMF-013).

On-device (Orin Nano 8 GB, WendyOS 0.18.2, agent 2026.08.22-032001,
2026-08-31): the group deploys via `wendy init --branch` → `wendy run` and
**both services verified over the LAN with the shipped defaults** — voice-app
on :9007 (correct local-LLM defaults in `/api/settings`, health watcher green
against the live LLM leg, proving the group's shared host networking), and
max-serve serving `Qwen/Qwen2.5-0.5B-Instruct` on :9012 from the Orin GPU:
first boot ~450 s (1 GB download + graph compile), coherent completions at
**~35 tok/s incl. prefill** (SmolLM2-135M measured ~30 tok/s the same way).
Two hard-won sizing lessons (MMF-006 addendum):
`--device-memory-utilization` is a fraction of **free** unified memory at
boot — a co-resident CUDA app holding 1.7 GB made even a 256 MiB model fail
its memory plan (stop heavy GPU neighbors before first boot), and
Qwen2.5-1.5B (2.88 GiB bf16) does not plan at 0.6×free with the voice app
resident, which is why the default is the 0.5B.
