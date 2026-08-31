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
| Jetson Orin Nano 8 GB | `Qwen/Qwen2.5-1.5B-Instruct` (default) or `HuggingFaceTB/SmolLM2-135M-Instruct` (fastest, toy-grade answers) |
| Jetson AGX Orin / Thor | `Qwen/Qwen2.5-7B-Instruct`-class bf16 models |
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

Template assembled 2026-08-31; container-level verification on Apple Silicon
(MAX 26.5.0): `Qwen/Qwen2.5-1.5B-Instruct` bf16 serves on the Apple GPU via
`max serve`, coherent greedy output, ~107 tok/s decode for the 135M model class
(see findings doc MMF-013 for the Mac serving spike). On-device group
verification on the Orin Nano is pending bench access — the port follows the
verified `mojo/llm` group shape exactly.
