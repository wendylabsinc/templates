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
- **Offline boots work once the model is cached** (MMF-019): `max serve` normally refuses to
  start without network access even with every weight local, so the entrypoint detects
  broken DNS and sets `HF_HUB_OFFLINE=1` when the model is already in the `-models` volume
  (verified: zero-network start in ~20 s warm).
- The app-level `{ "type": "network", "mode": "host" }` entitlement in `wendy.json` is
  required on WendyOS ≤ 0.18.2 / agent 2026.08.22: group-service containers otherwise get an
  empty network namespace — no model download *and* no reachable ports (findings doc,
  Appendix W). Open WebUI reaches max-serve over loopback for the same reason.

## Model sizing

| Device | Suggested MAX_MODEL |
| --- | --- |
| Jetson Orin Nano 8 GB | `HuggingFaceTB/SmolLM2-135M-Instruct` (default, verified) or `Qwen/Qwen2.5-0.5B-Instruct` |
| Jetson AGX Orin / Thor | `Qwen/Qwen2.5-7B-Instruct`-class bf16 models |
| CPU-only (RPi 5 etc.) | `modularai/SmolLM-135M-Instruct-FP32` |

Gated models (Llama) need `HF_TOKEN` at scaffold time (baked into the image — same caveat as
other templates in this repo).

## Status

**Verified end-to-end as the two-service group** on a Jetson Orin Nano (JetPack 7.2,
WendyOS 0.18.2, agent + CLI 2026.08.22, MAX 26.5.0, 2026-08-24): browser → Open WebUI
(`:9010`) → max-serve (`:9011`) → GPU inference at ~15 tok/s (SmolLM2-135M bf16, incl.
prefill + LAN). Needs the app-level `network` entitlement and agent ≥ 2026.08.22 (earlier
agents also cap group containers at ~256 MiB) — see `docs/mojo-max-port-findings.md`
Appendix W. First boot on a fresh volume takes minutes (model download + graph compile,
MMF-018); `wendy run`'s readiness wait can elapse during it even though the deploy is
healthy — the UI comes up shortly after.
