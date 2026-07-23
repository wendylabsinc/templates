# Consolidate `samples` into `templates` and retire `samples`

**Date:** 2026-07-15
**Status:** Design approved (pending spec review)

## Problem

Wendy has two overlapping repos:

- **`templates`** (this repo) — the curated source of truth. Registry-driven
  (`meta.json`), consumed by `wendy init --template`. Apps are Go-templated
  (`{{.APP_ID}}`, `{{.PORT}}`, `template.json`), use current deps
  (Hummingbird 2.21.1, swift-tools 6.3, OpenTelemetry), share frontends via
  `common/`, and are documented in `README.md` tables.
- **`../samples`** (`wendylabsinc/samples`) — an older, looser demo collection.
  No registry; hardcoded app IDs/ports; older deps; manual `wendy run` README.

They must be consolidated so there is one source of truth. `templates` wins:
for **every archetype that overlaps by name, `templates` is already newer and
more evolved than `samples`** (verified: `samples` Hummingbird 2.0.0 / tools 6.2
/ no OTel / hardcoded vs `templates` 2.21.1 / 6.3 / OTel / parameterized). Nothing
flows back from `samples` into existing `templates` apps.

The only value in `samples` is the **specialized apps `templates` lacks**, plus
one portable open PR. After import, `samples` is retired.

## Goal & non-goals

**Goal:** Import the unique/newer specialized apps from `samples` (and one open
PR) into `templates`, fully conformed to `templates` conventions, in a single
consolidation PR; then retire the `samples` repo.

**Non-goals:**
- Not a two-way sync. No existing `templates` app is modified from `samples`.
- Not importing archetypes `templates` already covers.
- Not merging the 10 open `templates`-repo PRs here (tracked as a checklist;
  most need on-device verification driven separately).
- Not a git-history graft — histories are unrelated; content is imported.

## Decisions (locked)

| Decision | Choice |
|---|---|
| Merge meaning | Consolidate & retire `samples`; `templates` is the single source of truth |
| Scope | Only unique/newer apps; skip archetypes `templates` already covers |
| Conformance | **Full conform** — `meta.json` entry, `template.json`, `wendy.json`, README row, standardized naming, dep bumps |
| Open PRs | Port unmerged PR content directly (applies to `samples` #13; optionally #12) |
| Delivery | **One big consolidation PR** |
| `templates` own PRs | **Checklist only** — not landed as part of this effort |

## What gets imported (Phase 1)

Each app is imported under the standardized archetype name below and given the
full `templates` treatment: parameterize with `{{.APP_ID}}`/`{{.PORT}}`, add a
`template.json` (and `template.schema.json` where variables need description —
note: any var in the schema **must** also appear in `template.json` or render
emits `<no value>`), bump deps to the `templates` baseline, add/normalize
`wendy.json`, register in `meta.json`, and add a README table row.

| Archetype | Source (in `samples/main`) | Lang(s) | Entitlements (approx) | Notes |
|---|---|---|---|---|
| `deepstream-vision` | `deepstream-vision/` (`detector/`, `vlm/`, `gpu-stats/`) | python | gpu | Multi-service group: DeepStream YOLO11n + Qwen3-VL + tegrastats/Prometheus + `monitor.html` dashboard. Needs an app-group home. |
| `bluetooth-discovery` | `python/bluetooth-discovery` | python | bluetooth, network | BLE scan (bleak) + FastAPI + SSE + web UI |
| `voice-assistant` | `python/voice-assistant` | python | audio, gpu | Wake-word ("wendy", openwakeword) → Whisper STT → local LLM w/ tools → Piper TTS |
| `asr-nemotron` | `python/nemotron-speech-asr` | python | audio, gpu, persist | USB mic + Silero VAD + NVIDIA Nemotron ASR + waveform |
| `whisper-stt` | `python/speech-to-text` | python | audio, gpu | Headless Jetson Whisper, ALSA/PortAudio, JetPack 6 |
| `hello-pytorch` | `python/hello-pytorch` | python | gpu | CUDA/MPS/CPU availability poll; CPU-slim + Jetson-slim Dockerfiles |
| `mlx-llm-chat` | `python/mlx-qwen3-4b-4bit` | python | gpu, network | Qwen3-4B; mlx-lm (macOS) / transformers (Linux); own React/shadcn frontend |
| `swift-mlx-llm` | `swift/mlx-llm-example` | swift | gpu, network | Swift MLX LLM server + React frontend |
| `llm-gguf` | `swift/qwen3-4b-gguf` | swift | gpu | Hummingbird server driving `llama-cli` (GGUF) on `dustynv/tensorrt` base + React frontend |
| `tensorrt-hello` | `swift/tensorrt-hello` | swift | gpu | Minimal Swift↔TensorRT binding demo |
| `tensorrt-llm` | `swift/tensorrt-llm-streaming` | swift | gpu, network | Swift TensorRT LLM token-streaming server |
| `webcam` | `python/webcam`, `swift/webcam` | python, swift | camera, gpu, network | Generic UVC: GStreamer WebRTC w/ HW encode, MJPEG-over-WS fallback; `/dev/video*` autodetect. **Distinct from existing `camera-feed`** (MJPEG-only). |
| `persistent-volume` | `*/persistent-volume` | cpp, node, rust, swift, python | persist | Minimal `persist` demo (`/data` survives restart) |
| `sqlite-persistence` | `*/sqlite-persistence` | cpp, node, rust, swift, python | persist | SQLite on `persist` volume, append+query across restarts |

**Frontend handling:** apps with React/shadcn frontends (`mlx-llm-chat`,
`swift-mlx-llm`, `llm-gguf`, `webcam`) start with per-app frontends. Only promote
a frontend to `common/` if two imported apps genuinely share one (mirrors the
existing `common/shadcn-vite-frontend` pattern). Do not pre-factor.

**Explicitly NOT imported** (templates already newer / a superset):
`simple-server`→`simple-api`, `web-app`→`fullstack`, `audio`, `yolov8`→
`camera-feed-yolo`, `pipecat-assistant`→`voice-ai-pipecat` (samples uses older
cloud Deepgram/OpenAI; templates uses local faster-whisper + Piper), and the
Linux-container `hello-world` variants (covered by `simple-api`).

## Open-PR content to port (Phase 2)

| PR | Disposition |
|---|---|
| **samples #13 `ai-security-camera`** | **PORT** as a new template. DeepStream YOLO11n + NvDCF tracking + debounced person/vehicle events + ONVIF discovery + Prometheus + dashboard. Complete & documented; on-Jetson smoke test pending. Highest-value port. Full-conform on import. |
| samples #12 `camera-benchmark` | **DEFER (optional).** Self-contained + complete with synthetic fallback, but niche/marketing and depends on the WendyOS Pi-CSI path. Import later only if a benchmarking template is wanted. |
| samples #11 realsense (Vite/React) | **SKIP.** Frontend-only placeholders; superseded by the existing complete `realsense-camera` template. |
| samples #10 GPU Dockerfile fixes | **SKIP for templates.** Targets samples-only apps; optionally merge in `samples` before archiving. |
| samples #8 CUDA Dockerfile | **SKIP/STALE.** Superseded by templates PR #70 (`swift/cuda-llm`). |
| samples #7 profile-based run flows | **SKIP/STALE.** Uses a config model that doesn't apply to `template.json`. |
| samples #6 kiosk UI | **SKIP/STALE.** Abandoned draft; revisit only if a kiosk template is explicitly wanted. |
| samples #2 non-16kHz mic | **SKIP/STALE.** Target dir deleted in samples; logic already surpassed by `voice-ai-pipecat` device selection. |

## `templates` own open PRs (Phase 3 — checklist only)

Not landed by this effort. Tracked with recommendation; merged separately with
device access as needed.

- **#72** hermes-agent + lean `claude` node templates — *this is the current
  branch (`jo/hermes-agent-template`), unmerged.* Finalize & land first.
- **#53** go2-rosbag per-topic status — tiny, verified on real Go2. Merge now.
- **#60** wifi-sensing (CSI + MCP) — best-tested new template (real ESP32-C6
  hardware + 26 tests). Merge.
- **#62** ros2-talker-listener (Swift) — build + cross-container run verified. Merge.
- **#74** llm Orin/Thor models + hooks — high value; verify JP6 GPU on device.
- **#59** camera-fleet — LAN-verified; introduces fleet/topology pattern.
- **#56** camera-feed-vlm (moondream2) — CPU-verified; wants a Jetson pass.
- **#69** go2-mapper, **#61** Isaac Sim RL — new drafts; merge after on-device verify.
- **#70** swift/cuda-llm — merge only after de-forking mlx-swift + removing
  hardcoded `sm_87`.

## Retire `samples` (Phase 4)

1. (Optional) Merge samples #10 in `samples` so its final state is clean.
2. Replace `samples/README.md` with a short pointer to `templates` (`wendy init
   --template`) and a note that `samples` is archived/read-only.
3. Add a line to `templates/README.md` noting it supersedes `samples`.
4. Archive the `wendylabsinc/samples` GitHub repo (read-only) after the
   consolidation PR merges — **not** before, so nothing is lost.

## Branching & PR strategy

- Create a fresh branch off `main` for the consolidation (do **not** build on
  `jo/hermes-agent-template`/#72; that lands on its own). If #72 must merge
  first, rebase the consolidation branch onto the updated `main`.
- One consolidation PR containing all Phase 1 imports + Phase 2 `ai-security-camera`.
- Big diff is expected and accepted (per delivery decision). Mitigate review pain
  with a clean, per-archetype commit history (one commit per imported archetype)
  and a PR description mapping each archetype to its `samples` source.

## Per-archetype import checklist (applied to every imported app)

1. Copy source from `samples/main` into the correct `templates/<lang>/<archetype>/`.
2. Parameterize: replace hardcoded app IDs → `{{.APP_ID}}`, ports → `{{.PORT}}`,
   and any other user-facing constants → template vars.
3. Add `template.json` (defaults for all vars) + `template.schema.json` where
   vars need descriptions. **Every schema var must also be in `template.json`.**
4. Bump deps to the `templates` baseline for that language (e.g. Hummingbird
   2.21.1 / swift-tools 6.3 + OTel for Swift; uv + current FastAPI for Python).
5. Add/normalize `wendy.json` with correct entitlements + readiness probe +
   postStart hook, matching sibling templates.
6. Add a `meta.json` entry (`name`, `description`, and `targets`/`languages`
   where they constrain applicability).
7. Add a README table row/section in the repo's documented style.
8. Verify: `wendy init --template <name> --language <lang>` renders cleanly
   (no `<no value>`), and the rendered app builds. GPU/Jetson-only apps are
   build-verified where possible and flagged in the PR for on-device follow-up.

## Testing / validation

- **Render validation:** every imported archetype must render via `wendy init
  --template` with no unresolved `<no value>` and produce a buildable project.
- **Build validation:** CPU-buildable apps built locally; GPU/Jetson/Orin/Thor
  apps build-checked against their base images where feasible, with on-device
  verification explicitly deferred and called out per app in the PR.
- **Registry validation:** `meta.json` parses and every new entry resolves to a
  real directory for each declared language.

## Risks

- **GPU/Jetson apps can't be fully verified without hardware** — accept
  build-only verification + explicit PR callouts; do not claim on-device success.
- **`deepstream-vision` is a multi-service group** — needs an app-group layout
  decision consistent with existing groups (e.g. `go2-*`); confirm the group
  convention before importing.
- **Large single PR** — accepted; mitigated by per-archetype commits + a mapping
  table in the PR body.
- **Dep bumps may break samples code** written against older APIs (e.g.
  Hummingbird 2.0.0 → 2.21.1) — each import validates a build post-bump.
