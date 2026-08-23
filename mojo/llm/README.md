# llm (Mojo/MAX)

Local LLM chat on WendyOS: **MAX serve** (Modular's OpenAI-compatible serving layer, GPU or
CPU) + **Open WebUI**. The Mojo/MAX port of the `python/llm` (Ollama) template — same
two-service shape, with Ollama replaced by `max serve`.

- `max-serve/` — `pip install modular` on `python:3.12-slim`; serves `MAX_MODEL` on `:9011`
  (`/v1` OpenAI API). One image for every target: MAX picks GPU or CPU at runtime from
  `WENDY_HAS_GPU`; the CUDA driver comes from the host via the `gpu` entitlement.
- `open-webui/` — Open WebUI pointed at the local `/v1` endpoint, on `{{.PORT}}`.

## Jetson notes (hard-won — see `docs/mojo-max-port-findings.md`)

- `MODULAR_DEVICE_CONTEXT_MEMORY_MANAGER_VMM=0` and `--no-device-graph-capture` are baked in:
  without them the iGPU VMM graph-capture path crashes on Orin/Thor (MMF-004, upstream #6961).
- Explicit `--max-batch-size/--max-length/--device-memory-utilization` are mandatory on
  unified memory — the auto-tuner OOM-kills the worker otherwise (MMF-006). Tune via the
  `MAX_LENGTH`, `MAX_BATCH_SIZE`, `MAX_DEVICE_MEMORY_UTILIZATION` env vars.
- First start on a fresh volume compiles the model graph (~6 min for a 135M model on an
  Orin Nano CPU); the cache persists in the `-models` volume, so restarts take seconds
  (MMF-018). The Web UI comes up immediately; the model appears once compiled.
- CPU-only hosts (e.g. RPi 5): bf16-safetensors repos cannot be served on CPU in MAX 26.5
  (MMF-016) — use an FP32-native model such as `modularai/SmolLM-135M-Instruct-FP32`.

## Model sizing

| Device | Suggested MAX_MODEL |
| --- | --- |
| Jetson Orin Nano 8 GB | `HuggingFaceTB/SmolLM2-135M-Instruct` (default, verified) or `Qwen/Qwen2.5-0.5B-Instruct` |
| Jetson AGX Orin / Thor | `Qwen/Qwen2.5-7B-Instruct`-class bf16 models |
| CPU-only (RPi 5 etc.) | `modularai/SmolLM-135M-Instruct-FP32` |

Gated models (Llama) need `HF_TOKEN` at scaffold time (baked into the image — same caveat as
other templates in this repo).

## Status

The **max-serve layer is verified** on a Jetson Orin Nano (JetPack 7.2, MAX 26.5.0,
2026-08-23) as a single-service deployment. The **two-service group form is currently
blocked by WendyOS 0.18.2 group-container restrictions** (no egress network for model
download; ~256 MiB memory cap) — see `docs/mojo-max-port-findings.md` Appendix W. Once the
platform issues are resolved the group deploys as-is.
