# Samples → Templates Consolidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Import every unique/newer specialized app from `../samples` (plus `samples` PR #13) into this `templates` repo, fully conformed to templates conventions, in one consolidation PR — then retire `samples`.

**Architecture:** One-way import. `templates` is the source of truth and already newer for all overlapping archetypes, so nothing is back-ported into existing templates. Each imported app is copied from `samples/main`, parameterized with Go template vars, given `template.json` + `wendy.json` + a `meta.json` entry + README row, dep-bumped to the templates baseline, and validated by rendering with `wendy init` and building.

**Tech Stack:** Wendy CLI (`wendy init --template`, `wendy build`), Go text/template rendering, per-language toolchains (Python+uv, Swift 6.3/Hummingbird, Rust, Node/TS, C++), Docker/buildx, `meta.json` registry.

## Global Constraints

- **Source of truth:** copy imported app source from `../samples` at **`origin/main`** (local checkout is behind — use `git -C ../samples show origin/main:<path>` or fetch+checkout). Do NOT modify existing `templates` apps.
- **Templates baseline deps (bump imports to match sibling templates):** Swift → swift-tools `6.3`, Hummingbird `2.21.1`, add `swift-otel`; Python → `uv` + current FastAPI (match `python/simple-api`); match the sibling template's toolchain for Rust/Node/C++.
- **Parameterization:** replace the app's hardcoded application identifier with `{{.APP_ID}}` and its hardcoded primary port with `{{.PORT}}` everywhere (wendy.json, source, Dockerfile, frontend config). No other value becomes a variable unless it blocks a clean render.
- **`template.json` required** for every archetype (defaults for all vars). Add `template.schema.json` only when a var needs a description/enum — and **every var in the schema MUST also be in `template.json`** or render emits `<no value>` (see `python/llm/template.schema.json`).
- **`wendy.json` shapes:** single-service = top-level `entitlements` array + `readiness.tcpSocket` + `hooks.postStart` (see `python/simple-api/wendy.json`); multi-service group = `services` map, each with `context` + its own `entitlements` (see `python/llm/wendy.json`).
- **Entitlement types in use:** `network` (optionally `"mode": "host"`), `camera`, `gpu`, `audio`, `bluetooth`, `persist`, `mcp`.
- **`meta.json` entry format:** `{ "name", "description" }`, plus `"languages": [...]` when the template is not available in all 5 languages, plus `"targets": [...]` (e.g. `["wendyos"]`, `["darwin"]`) when platform-constrained. Omit `languages` only when it truly ships in every language.
- **Standard Import Procedure (applies verbatim to every Phase-1/2 task):**
  1. Copy source: `mkdir -p <lang>/<archetype> && git -C ../samples archive origin/main <src-path> | tar -x -C <lang>/<archetype> --strip-components=<n>` (or `cp -R` from a checked-out worktree).
  2. Find hardcoded values: `grep -rniE 'appId|"port"|:[0-9]{4}\b' <lang>/<archetype>` → note the app id string and primary port.
  3. Replace app id string → `{{.APP_ID}}` and primary port literal → `{{.PORT}}` across all files (wendy.json, source, Dockerfile, any frontend `.env`/vite config).
  4. Bump deps to baseline (see Global Constraints) in the manifest (`Package.swift` / `pyproject.toml`|`requirements.txt` / `Cargo.toml` / `package.json`).
  5. Write `template.json` (name, description, `APP_ID` string required, `PORT` integer with the archetype's default + `validate {min:1,max:65535}`; add extra vars only if step 3 found more).
  6. Write/normalize `wendy.json` per the correct shape with the entitlements listed in the task.
  7. Add the `meta.json` entry.
  8. Add a README section/row in the repo's documented style.
  9. Validate (render + build) per the task's validation step.
- **Render validation command (temp dir, never pollutes repo):**
  `WORK=$(mktemp -d); wendy init --app-id smoke --template <name> --language <lang> --var PORT=<default> --dir "$WORK/<name>-<lang>"` then `grep -rn '<no value>' "$WORK/<name>-<lang>" && echo "RENDER FAIL" || echo "RENDER OK"`.
- **Build validation:** CPU-buildable → `cd "$WORK/<name>-<lang>" && wendy build` (or `docker build .`) must succeed. GPU/Jetson/Orin-only → build-check the base image pull + syntax only; mark on-device verification as a PR follow-up (do NOT claim on-device success).
- **Commits:** one commit per archetype (per task). Commit message: `feat(<lang-or-multi>): import <archetype> template from samples`. Sign-off footer as configured for this repo.
- **Branch:** all tasks land on branch `jo/samples-consolidation` off `main` (Task 0). Do not build on `jo/hermes-agent-template`.

---

### Task 0: Create consolidation branch and fetch samples

**Files:**
- Modify: none (git plumbing only)

**Interfaces:**
- Produces: branch `jo/samples-consolidation` checked out; `../samples` fetched with `origin/main` available.

- [ ] **Step 1: Verify clean tree and fetch both repos**

Run:
```bash
cd /Users/joannisorlandos/git/wendy/templates
git status --porcelain            # expect empty
git fetch origin
git -C ../samples fetch origin
```
Expected: no uncommitted changes; fetch succeeds.

- [ ] **Step 2: Create branch off latest main**

Run:
```bash
git checkout main && git pull --ff-only
git checkout -b jo/samples-consolidation
```
Expected: on `jo/samples-consolidation`.

- [ ] **Step 3: Sanity-check the samples source is reachable**

Run: `git -C ../samples ls-tree --name-only origin/main`
Expected: lists `cpp deepstream-vision node-typescript python rust swift`.

- [ ] **Step 4: Commit a plan/branch marker (optional, skip if empty)**

No file change; nothing to commit. Proceed.

---

### Task 1: Import `persistent-volume` (all 5 languages)

**Files:**
- Create: `python/persistent-volume/`, `swift/persistent-volume/`, `rust/persistent-volume/`, `node/persistent-volume/`, `cpp/persistent-volume/` (each: source + `Dockerfile` + `wendy.json` + `template.json`)
- Modify: `meta.json`, `README.md`

**Interfaces:**
- Consumes: Standard Import Procedure; reference `python/simple-api` for Python conform pattern.
- Produces: archetype `persistent-volume` registered for `python, swift, rust, node, cpp`.

Source paths (samples `origin/main`): `python/persistent-volume`, `swift/persistent-volume`, `rust/persistent-volume`, `node-typescript/persistent-volume` (→ dest `node/`), `cpp/persistent-volume`.

- [ ] **Step 1: Run Standard Import Procedure steps 1–4 for each of the 5 languages**

For each lang copy source to `<lang>/persistent-volume`, parameterize app id → `{{.APP_ID}}`, port → `{{.PORT}}`, bump deps to baseline. Note `node-typescript` → `node`.

- [ ] **Step 2: Write `template.json` for each language**

```json
{
    "name": "persistent-volume",
    "description": "Minimal persistent volume demo: /data survives restart",
    "variables": [
        { "name": "APP_ID", "description": "Application identifier", "type": "string", "required": true, "prompt": "App ID" },
        { "name": "PORT", "description": "Primary HTTP port", "type": "integer", "default": 8080, "prompt": "HTTP port", "validate": { "min": 1, "max": 65535 } }
    ]
}
```
(If an app has no HTTP port, drop the `PORT` var and the readiness block.)

- [ ] **Step 3: Write `wendy.json` for each language**

```json
{
    "appId": "{{.APP_ID}}",
    "version": "0.1.0",
    "platform": "linux",
    "entitlements": [ { "type": "persist", "path": "/data" } ],
    "readiness": { "tcpSocket": { "port": {{.PORT}} }, "timeoutSeconds": 30 }
}
```
(Confirm the exact `persist` entitlement schema against the samples app's original `wendy.json`; keep its `path`.)

- [ ] **Step 4: Add `meta.json` entry**

Insert into the `templates` array:
```json
{ "name": "persistent-volume", "description": "Minimal persistent volume demo: /data survives restart", "languages": ["python", "swift", "rust", "node", "cpp"] }
```

- [ ] **Step 5: Add README section** documenting `persistent-volume` with a per-language directory table row (match existing style).

- [ ] **Step 6: Render-validate all 5 languages**

Run the Render validation command for each `--language` in `python swift rust node cpp`.
Expected: `RENDER OK` for all five.

- [ ] **Step 7: Build-validate**

For each language: `wendy build` (or `docker build .`) in the rendered dir.
Expected: build succeeds.

- [ ] **Step 8: Commit**

```bash
git add python/persistent-volume swift/persistent-volume rust/persistent-volume node/persistent-volume cpp/persistent-volume meta.json README.md
git commit -m "feat(multi): import persistent-volume template from samples"
```

---

### Task 2: Import `sqlite-persistence` (all 5 languages)

**Files:**
- Create: `python/sqlite-persistence/`, `swift/sqlite-persistence/`, `rust/sqlite-persistence/`, `node/sqlite-persistence/`, `cpp/sqlite-persistence/`
- Modify: `meta.json`, `README.md`

**Interfaces:**
- Consumes: Task 1's established per-language conform pattern.
- Produces: archetype `sqlite-persistence` registered for all 5 languages.

Source paths: `python/sqlite-persistence`, `swift/sqlite-persistence`, `rust/sqlite-persistence`, `node-typescript/sqlite-persistence`, `cpp/sqlite-persistence`.

- [ ] **Step 1: Standard Import Procedure steps 1–4 for each language** (SQLite DB lives on the persist volume at `/data/app.db`; keep that path).

- [ ] **Step 2: Write `template.json`** (same shape as Task 1 Step 2, `name`/`description` = `sqlite-persistence` / "SQLite on a persistent volume: append + query rows across restarts", `PORT` default 8080).

- [ ] **Step 3: Write `wendy.json`** (same shape as Task 1 Step 3, entitlement `{ "type": "persist", "path": "/data" }`).

- [ ] **Step 4: Add `meta.json` entry**
```json
{ "name": "sqlite-persistence", "description": "SQLite on a persistent volume: append + query rows across restarts", "languages": ["python", "swift", "rust", "node", "cpp"] }
```

- [ ] **Step 5: Add README section.**

- [ ] **Step 6: Render-validate all 5 languages** (Render validation command). Expected: `RENDER OK` ×5.

- [ ] **Step 7: Build-validate all 5.** Expected: build succeeds.

- [ ] **Step 8: Commit**
```bash
git add python/sqlite-persistence swift/sqlite-persistence rust/sqlite-persistence node/sqlite-persistence cpp/sqlite-persistence meta.json README.md
git commit -m "feat(multi): import sqlite-persistence template from samples"
```

---

### Task 3: Import `hello-pytorch` (python)

**Files:**
- Create: `python/hello-pytorch/` (source + CPU-slim `Dockerfile` + Jetson-slim `Dockerfile` + `wendy.json` + `template.json`)
- Modify: `meta.json`, `README.md`

**Interfaces:**
- Consumes: Standard Import Procedure.
- Produces: archetype `hello-pytorch` (python).

Source: `python/hello-pytorch`.

- [ ] **Step 1: Standard Import Procedure steps 1–4** (preserve both Dockerfiles: CPU-slim + Jetson-slim; keep the 2s CUDA/MPS/CPU poll loop).

- [ ] **Step 2: `template.json`** — `name` `hello-pytorch`, description "PyTorch CUDA/MPS/CPU availability check", vars `APP_ID` (+ `PORT` only if it serves HTTP; this app may be headless — if headless, omit `PORT` and readiness).

- [ ] **Step 3: `wendy.json`** — single-service, `entitlements: [ { "type": "gpu" } ]`; add `network` + readiness + postStart only if it serves a page.

- [ ] **Step 4: `meta.json` entry**
```json
{ "name": "hello-pytorch", "description": "PyTorch CUDA/MPS/CPU availability check (CPU + Jetson images)", "languages": ["python"] }
```

- [ ] **Step 5: README section.**

- [ ] **Step 6: Render-validate** `--language python`. Expected: `RENDER OK`.

- [ ] **Step 7: Build-validate** — CPU-slim image builds locally. Jetson-slim: base-image pull + `docker build` syntax check only; flag on-device as follow-up.

- [ ] **Step 8: Commit**
```bash
git add python/hello-pytorch meta.json README.md
git commit -m "feat(python): import hello-pytorch template from samples"
```

---

### Task 4: Import `bluetooth-discovery` (python)

**Files:**
- Create: `python/bluetooth-discovery/` (FastAPI + SSE + web UI + `Dockerfile` + `wendy.json` + `template.json`)
- Modify: `meta.json`, `README.md`

**Interfaces:**
- Produces: archetype `bluetooth-discovery` (python).

Source: `python/bluetooth-discovery`.

- [ ] **Step 1: Standard Import Procedure steps 1–4** (keep bleak `BleakScanner` + SSE stream + web UI).

- [ ] **Step 2: `template.json`** — `bluetooth-discovery`, "BLE scanner: live device stream (bleak + FastAPI + SSE + web UI)", vars `APP_ID`, `PORT` (default = the app's original port found in Step 2 of the procedure).

- [ ] **Step 3: `wendy.json`** — single-service, `entitlements: [ { "type": "bluetooth" }, { "type": "network", "mode": "host" } ]`, readiness `tcpSocket` on `{{.PORT}}`, postStart open-browser hook. Confirm exact `bluetooth` entitlement schema against the samples original.

- [ ] **Step 4: `meta.json` entry**
```json
{ "name": "bluetooth-discovery", "description": "BLE scanner: live device stream (bleak + FastAPI + SSE + web UI)", "languages": ["python"] }
```

- [ ] **Step 5: README section.**

- [ ] **Step 6: Render-validate** `--language python`. Expected: `RENDER OK`.

- [ ] **Step 7: Build-validate** — `docker build .` succeeds (BLE runtime behavior is device-only; flag on-device scan as follow-up).

- [ ] **Step 8: Commit**
```bash
git add python/bluetooth-discovery meta.json README.md
git commit -m "feat(python): import bluetooth-discovery template from samples"
```

---

### Task 5: Import `whisper-stt` (python)

**Files:**
- Create: `python/whisper-stt/`
- Modify: `meta.json`, `README.md`

**Interfaces:**
- Produces: archetype `whisper-stt` (python).

Source: `python/speech-to-text`.

- [ ] **Step 1: Standard Import Procedure steps 1–4** (headless Jetson Whisper; ALSA/PortAudio USB mic; continuous transcription to file; JetPack 6 CUDA base).

- [ ] **Step 2: `template.json`** — `whisper-stt`, "Headless Whisper speech-to-text on Jetson (USB mic → transcript file)", var `APP_ID` (headless → no `PORT`).

- [ ] **Step 3: `wendy.json`** — single-service, `entitlements: [ { "type": "audio" }, { "type": "gpu" } ]`, no readiness/postStart (headless).

- [ ] **Step 4: `meta.json` entry**
```json
{ "name": "whisper-stt", "description": "Headless Whisper speech-to-text on Jetson (USB mic → transcript file)", "languages": ["python"] }
```

- [ ] **Step 5: README section.**

- [ ] **Step 6: Render-validate** `--language python`. Expected: `RENDER OK`.

- [ ] **Step 7: Build-validate** — Jetson CUDA base: `docker build` syntax + base pull only; on-device audio flagged as follow-up.

- [ ] **Step 8: Commit**
```bash
git add python/whisper-stt meta.json README.md
git commit -m "feat(python): import whisper-stt template from samples"
```

---

### Task 6: Import `asr-nemotron` (python)

**Files:**
- Create: `python/asr-nemotron/`
- Modify: `meta.json`, `README.md`

**Interfaces:**
- Produces: archetype `asr-nemotron` (python).

Source: `python/nemotron-speech-asr`.

- [ ] **Step 1: Standard Import Procedure steps 1–4** (USB mic + Silero VAD + NVIDIA Nemotron ASR + live waveform web demo; model cache on a persist volume).

- [ ] **Step 2: `template.json`** — `asr-nemotron`, "Streaming ASR web demo: USB mic + Silero VAD + NVIDIA Nemotron", vars `APP_ID`, `PORT` (default from source).

- [ ] **Step 3: `wendy.json`** — single-service, `entitlements: [ { "type": "audio" }, { "type": "gpu" }, { "type": "persist", "path": "<model-cache-path-from-source>" }, { "type": "network", "mode": "host" } ]`, readiness on `{{.PORT}}`, postStart open-browser.

- [ ] **Step 4: `meta.json` entry**
```json
{ "name": "asr-nemotron", "description": "Streaming ASR web demo: USB mic + Silero VAD + NVIDIA Nemotron", "languages": ["python"] }
```

- [ ] **Step 5: README section.**

- [ ] **Step 6: Render-validate** `--language python`. Expected: `RENDER OK`.

- [ ] **Step 7: Build-validate** — GPU base: build syntax + base pull; on-device ASR flagged as follow-up.

- [ ] **Step 8: Commit**
```bash
git add python/asr-nemotron meta.json README.md
git commit -m "feat(python): import asr-nemotron template from samples"
```

---

### Task 7: Import `voice-assistant` (python)

**Files:**
- Create: `python/voice-assistant/`
- Modify: `meta.json`, `README.md`

**Interfaces:**
- Produces: archetype `voice-assistant` (python). Distinct from existing `voice-ai-pipecat` (Pipecat/Gemini) — this is the local wake-word + local-LLM + Piper stack.

Source: `python/voice-assistant`.

- [ ] **Step 1: Standard Import Procedure steps 1–4** (wake-word "wendy" via openwakeword → Whisper STT → local LLM with tool calls → Piper TTS).

- [ ] **Step 2: `template.json`** — `voice-assistant`, "Local voice assistant: wake-word + Whisper STT + local LLM tools + Piper TTS", vars `APP_ID` (+ `PORT` only if it serves a UI/API).

- [ ] **Step 3: `wendy.json`** — single-service, `entitlements: [ { "type": "audio" }, { "type": "gpu" } ]` (+ `network` if it serves anything); readiness/postStart only if there is a port.

- [ ] **Step 4: `meta.json` entry**
```json
{ "name": "voice-assistant", "description": "Local voice assistant: wake-word + Whisper STT + local LLM tools + Piper TTS", "languages": ["python"] }
```

- [ ] **Step 5: README section** (call out the difference vs `voice-ai-pipecat`).

- [ ] **Step 6: Render-validate** `--language python`. Expected: `RENDER OK`.

- [ ] **Step 7: Build-validate** — GPU base: build syntax + base pull; on-device flagged.

- [ ] **Step 8: Commit**
```bash
git add python/voice-assistant meta.json README.md
git commit -m "feat(python): import voice-assistant template from samples"
```

---

### Task 8: Import `mlx-llm-chat` (python) and `swift-mlx-llm` (swift)

**Files:**
- Create: `python/mlx-llm-chat/`, `swift/mlx-llm-chat/` (each with its React/shadcn frontend)
- Modify: `meta.json`, `README.md`

**Interfaces:**
- Produces: archetypes `mlx-llm-chat` (python) and `swift-mlx-llm` (swift). Distinct from `mac-llm` (native macOS + Open WebUI) — these are self-contained cross-platform (mlx on macOS / transformers on Linux) with their own frontend.

Sources: `python/mlx-qwen3-4b-4bit`, `swift/mlx-llm-example`.

- [ ] **Step 1 (python): Standard Import Procedure steps 1–4** for `python/mlx-llm-chat` (Qwen3-4B; mlx-lm macOS / transformers Linux; parameterize frontend API base URL/port too).

- [ ] **Step 2 (swift): Standard Import Procedure steps 1–4** for `swift/mlx-llm-chat`; bump to Hummingbird 2.21.1 / swift-tools 6.3 / swift-otel per baseline (this app is Swift MLX server + React frontend).
  > Note: name the swift archetype `swift-mlx-llm` in `meta.json` but keep the directory `swift/mlx-llm-chat` only if the CLI derives the name from `template.json`; otherwise use directory `swift/swift-mlx-llm`. Verify how `wendy init` resolves `--template <name> --language swift` to a directory and match it. Confirm before committing.

- [ ] **Step 3: `template.json`** for each — vars `APP_ID`, `PORT` (default from source), plus a `MODEL` string var only if the source hardcodes a model id that a user would plausibly change (default = the source's model, e.g. `Qwen3-4B-4bit`).

- [ ] **Step 4: `wendy.json`** for each — single-service, `entitlements: [ { "type": "gpu" }, { "type": "network", "mode": "host" } ]`, readiness on `{{.PORT}}`, postStart open-browser.

- [ ] **Step 5: `meta.json` entries**
```json
{ "name": "mlx-llm-chat", "description": "Self-contained Qwen3-4B chat: mlx-lm (macOS) / transformers (Linux) + React frontend", "languages": ["python"] }
```
```json
{ "name": "swift-mlx-llm", "description": "Swift MLX LLM server with React frontend", "languages": ["swift"] }
```

- [ ] **Step 6: README sections** (both; note distinction from `mac-llm`).

- [ ] **Step 7: Render-validate** `mlx-llm-chat --language python` and `swift-mlx-llm --language swift`. Expected: `RENDER OK` both.

- [ ] **Step 8: Build-validate** — CPU/transformers path builds locally where feasible; GPU/mlx path build-syntax + flagged on-device.

- [ ] **Step 9: Commit**
```bash
git add python/mlx-llm-chat swift/*mlx* meta.json README.md
git commit -m "feat(multi): import mlx-llm-chat (python) and swift-mlx-llm templates from samples"
```

---

### Task 9: Import `llm-gguf` (swift)

**Files:**
- Create: `swift/llm-gguf/` (Hummingbird server + React frontend)
- Modify: `meta.json`, `README.md`

**Interfaces:**
- Produces: archetype `llm-gguf` (swift).

Source: `swift/qwen3-4b-gguf`.

- [ ] **Step 1: Standard Import Procedure steps 1–4** (Hummingbird server driving `llama-cli` GGUF on `dustynv/tensorrt` base; bump Hummingbird/swift-tools/otel to baseline; keep the GGUF/llama-cli invocation).

- [ ] **Step 2: `template.json`** — `llm-gguf`, "Swift server driving llama.cpp (GGUF) for Qwen3-4B chat on Jetson", vars `APP_ID`, `PORT`, optional `MODEL` (default = source GGUF id).

- [ ] **Step 3: `wendy.json`** — single-service, `entitlements: [ { "type": "gpu" }, { "type": "network", "mode": "host" } ]`, readiness on `{{.PORT}}`, postStart open-browser.

- [ ] **Step 4: `meta.json` entry**
```json
{ "name": "llm-gguf", "description": "Swift server driving llama.cpp (GGUF) for Qwen3-4B chat on Jetson", "languages": ["swift"] }
```

- [ ] **Step 5: README section.**

- [ ] **Step 6: Render-validate** `--language swift`. Expected: `RENDER OK`.

- [ ] **Step 7: Build-validate** — `dustynv/tensorrt` base pull + build syntax; on-device flagged.

- [ ] **Step 8: Commit**
```bash
git add swift/llm-gguf meta.json README.md
git commit -m "feat(swift): import llm-gguf template from samples"
```

---

### Task 10: Import `tensorrt-hello` (swift)

**Files:**
- Create: `swift/tensorrt-hello/`
- Modify: `meta.json`, `README.md`

**Interfaces:**
- Produces: archetype `tensorrt-hello` (swift).

Source: `swift/tensorrt-hello`.

- [ ] **Step 1: Standard Import Procedure steps 1–4** (minimal `import TensorRT` binding demo; `dustynv/tensorrt:8.6-r36.2.0` base; keep Swift 6.2.3 if bumping to 6.3 breaks the TensorRT binding — verify build, and record the pinned version in the Dockerfile comment if kept).

- [ ] **Step 2: `template.json`** — `tensorrt-hello`, "Minimal Swift↔TensorRT binding demo", var `APP_ID` (headless → no `PORT`).

- [ ] **Step 3: `wendy.json`** — single-service, `entitlements: [ { "type": "gpu" } ]`, no readiness (headless).

- [ ] **Step 4: `meta.json` entry**
```json
{ "name": "tensorrt-hello", "description": "Minimal Swift↔TensorRT binding demo", "languages": ["swift"] }
```

- [ ] **Step 5: README section.**

- [ ] **Step 6: Render-validate** `--language swift`. Expected: `RENDER OK`.

- [ ] **Step 7: Build-validate** — TensorRT base pull + build syntax; on-device flagged.

- [ ] **Step 8: Commit**
```bash
git add swift/tensorrt-hello meta.json README.md
git commit -m "feat(swift): import tensorrt-hello template from samples"
```

---

### Task 11: Import `tensorrt-llm` (swift)

**Files:**
- Create: `swift/tensorrt-llm/`
- Modify: `meta.json`, `README.md`

**Interfaces:**
- Produces: archetype `tensorrt-llm` (swift).

Source: `swift/tensorrt-llm-streaming`.

- [ ] **Step 1: Standard Import Procedure steps 1–4** (Swift TensorRT LLM token-streaming server; baseline dep bump unless it breaks the TensorRT binding — verify).

- [ ] **Step 2: `template.json`** — `tensorrt-llm`, "Swift TensorRT LLM token-streaming server", vars `APP_ID`, `PORT`.

- [ ] **Step 3: `wendy.json`** — single-service, `entitlements: [ { "type": "gpu" }, { "type": "network", "mode": "host" } ]`, readiness on `{{.PORT}}`, postStart open-browser.

- [ ] **Step 4: `meta.json` entry**
```json
{ "name": "tensorrt-llm", "description": "Swift TensorRT LLM token-streaming server", "languages": ["swift"] }
```

- [ ] **Step 5: README section.**

- [ ] **Step 6: Render-validate** `--language swift`. Expected: `RENDER OK`.

- [ ] **Step 7: Build-validate** — TensorRT base pull + build syntax; on-device flagged.

- [ ] **Step 8: Commit**
```bash
git add swift/tensorrt-llm meta.json README.md
git commit -m "feat(swift): import tensorrt-llm template from samples"
```

---

### Task 12: Import `webcam` (python + swift)

**Files:**
- Create: `python/webcam/`, `swift/webcam/`
- Modify: `meta.json`, `README.md`

**Interfaces:**
- Produces: archetype `webcam` (python, swift). **Distinct from `camera-feed`** (MJPEG-over-WS only): `webcam` is generic UVC enumeration + GStreamer WebRTC with MJPEG fallback.

Sources: `python/webcam`, `swift/webcam`.

- [ ] **Step 1: Standard Import Procedure steps 1–4** for both languages (keep `/dev/video*` autodetect + WebRTC-when-HW-encode + MJPEG fallback; swift → baseline dep bump).

- [ ] **Step 2: `template.json`** for each — `webcam`, "Generic USB/UVC webcam: GStreamer WebRTC with MJPEG fallback", vars `APP_ID`, `PORT`.

- [ ] **Step 3: `wendy.json`** for each — single-service, `entitlements: [ { "type": "camera" }, { "type": "gpu" }, { "type": "network", "mode": "host" } ]`, readiness on `{{.PORT}}`, postStart open-browser (matches `python/camera-feed/wendy.json`).

- [ ] **Step 4: `meta.json` entry**
```json
{ "name": "webcam", "description": "Generic USB/UVC webcam: GStreamer WebRTC with MJPEG fallback", "languages": ["python", "swift"] }
```

- [ ] **Step 5: README section** (note distinction from `camera-feed`).

- [ ] **Step 6: Render-validate** `webcam --language python` and `webcam --language swift`. Expected: `RENDER OK` both.

- [ ] **Step 7: Build-validate** — `docker build .` both; live camera/WebRTC flagged on-device.

- [ ] **Step 8: Commit**
```bash
git add python/webcam swift/webcam meta.json README.md
git commit -m "feat(multi): import webcam template from samples"
```

---

### Task 13: Import `deepstream-vision` (python, multi-service group)

**Files:**
- Create: `python/deepstream-vision/` with subdirs `detector/`, `vlm/`, `gpu-stats/`, the `monitor.html` dashboard, `start.sh`, plus `wendy.json` + `template.json` at the group root
- Modify: `meta.json`, `README.md`

**Interfaces:**
- Consumes: multi-service `wendy.json` pattern from `python/llm/wendy.json` and the `go2-rc`/`rc-car` app-group precedent.
- Produces: archetype `deepstream-vision` (python) as a service group.

Source: `deepstream-vision/` (top-level in samples).

- [ ] **Step 1: Confirm the app-group layout convention first**

Run: `cat python/go2-rc/wendy.json python/rc-car/wendy.json` and inspect their directory layout.
Expected: understand how existing multi-service python groups lay out per-service dirs + a group `wendy.json` `services` map. Mirror that layout for `deepstream-vision`. Record the chosen layout in the commit body.

- [ ] **Step 2: Standard Import Procedure steps 1–4** (copy `detector/`, `vlm/`, `gpu-stats/`, `monitor.html`, `start.sh`; DeepStream YOLO11n + Qwen3-VL + tegrastats/Prometheus; parameterize app id → `{{.APP_ID}}`, dashboard port → `{{.PORT}}`).

- [ ] **Step 3: `template.json`** — `deepstream-vision`, "Jetson vision group: DeepStream YOLO11n + Qwen3-VL scene descriptions + GPU metrics dashboard", vars `APP_ID`, `PORT` (dashboard).

- [ ] **Step 4: `wendy.json`** — multi-service `services` map (per `python/llm` shape), one entry per service with its `context` (`./detector`, `./vlm`, `./gpu-stats`) and `entitlements` (`gpu` on detector + vlm; dashboard service gets `network` + readiness). Match the existing group pattern from Step 1.

- [ ] **Step 5: `meta.json` entry**
```json
{ "name": "deepstream-vision", "description": "Jetson vision group: DeepStream YOLO11n + Qwen3-VL scene descriptions + GPU metrics dashboard", "languages": ["python"] }
```

- [ ] **Step 6: README section** (document the 3 services + dashboard).

- [ ] **Step 7: Render-validate** `--language python`. Expected: `RENDER OK`.

- [ ] **Step 8: Build-validate** — each service `docker build` (DeepStream base pull may be large; syntax + pull check acceptable); full on-Jetson run flagged as PR follow-up.

- [ ] **Step 9: Commit**
```bash
git add python/deepstream-vision meta.json README.md
git commit -m "feat(python): import deepstream-vision app-group template from samples"
```

---

### Task 14: Port `ai-security-camera` (python) from samples PR #13

**Files:**
- Create: `python/ai-security-camera/` (DeepStream detector + tracking + events + dashboard + ONVIF discovery + Prometheus)
- Modify: `meta.json`, `README.md`

**Interfaces:**
- Consumes: PR-branch content, not `origin/main`; multi-service group pattern from Task 13.
- Produces: archetype `ai-security-camera` (python).

Source: samples PR #13 branch `add-ai-security-camera-sample`.

- [ ] **Step 1: Fetch the PR branch content**

Run:
```bash
git -C ../samples fetch origin pull/13/head:pr-13
git -C ../samples ls-tree -r --name-only pr-13 | grep -i security
```
Expected: lists the `ai-security-camera` sample files on branch `pr-13`.

- [ ] **Step 2: Standard Import Procedure steps 1–4** sourcing from `pr-13` (RTSP → DeepStream YOLO11n + NvDCF tracking → debounced person/vehicle events → web dashboard; ONVIF auto-discovery; Prometheus). Parameterize app id → `{{.APP_ID}}`, dashboard port → `{{.PORT}}`, and the RTSP URL → a `RTSP_URL` string var (default = source default or empty).

- [ ] **Step 3: `template.json`** — `ai-security-camera`, "AI security camera: DeepStream YOLO11n + NvDCF tracking + person/vehicle events + ONVIF discovery", vars `APP_ID`, `PORT`, `RTSP_URL`.

- [ ] **Step 4: `wendy.json`** — single- or multi-service per source; `entitlements` include `gpu` + `network` (`host` if ONVIF discovery needs LAN); readiness on `{{.PORT}}` + postStart open-browser.

- [ ] **Step 5: `meta.json` entry**
```json
{ "name": "ai-security-camera", "description": "AI security camera: DeepStream YOLO11n + NvDCF tracking + person/vehicle events + ONVIF discovery", "languages": ["python"] }
```

- [ ] **Step 6: README section.**

- [ ] **Step 7: Render-validate** `--language python` — include `--var RTSP_URL=rtsp://x`. Expected: `RENDER OK`.

- [ ] **Step 8: Build-validate** — DeepStream base build syntax + pull; on-Jetson smoke test flagged as PR follow-up.

- [ ] **Step 9: Commit**
```bash
git add python/ai-security-camera meta.json README.md
git commit -m "feat(python): port ai-security-camera template from samples PR #13"
```

---

### Task 15: Full-repo registry + render sweep

**Files:**
- Modify: `meta.json`, `README.md` (fixups only if the sweep finds issues)

**Interfaces:**
- Consumes: all prior tasks' `meta.json` entries.
- Produces: a validated registry where every entry resolves and every archetype renders.

- [ ] **Step 1: Validate `meta.json` parses**

Run: `python3 -c "import json;d=json.load(open('meta.json'));print(len(d['templates']),'templates')"`
Expected: prints the count, no exception.

- [ ] **Step 2: Every new entry resolves to a real directory per declared language**

Run:
```bash
for t in persistent-volume sqlite-persistence hello-pytorch bluetooth-discovery whisper-stt asr-nemotron voice-assistant mlx-llm-chat swift-mlx-llm llm-gguf tensorrt-hello tensorrt-llm webcam deepstream-vision ai-security-camera; do
  find . -maxdepth 2 -type d -name "$t" | sed "s/^/$t -> /"
done
```
Expected: each name maps to at least one `<lang>/<name>` directory matching its declared `languages`.

- [ ] **Step 3: Render sweep — every new archetype × declared language**

Run the Render validation command for each (archetype, language) pair added in Tasks 1–14.
Expected: `RENDER OK` for all pairs; zero `<no value>`.

- [ ] **Step 4: Fix any gaps** found in Steps 1–3 inline (missing dir, wrong `languages`, unresolved var), then re-run the failing check.

- [ ] **Step 5: Commit (only if fixups were made)**
```bash
git add meta.json README.md
git commit -m "chore: validate consolidated template registry and render sweep"
```

---

### Task 16: Retire `samples` — README pointers

**Files:**
- Modify: `README.md` (this repo — note it supersedes `samples`)
- Modify: `../samples/README.md` (replace with an archived-pointer README) — separate commit in the `samples` repo
- Create: `docs/` note only if the repo documents deprecations elsewhere (skip otherwise)

**Interfaces:**
- Consumes: the completed import (Tasks 1–15).
- Produces: `samples` clearly deprecated; `templates` documented as the source of truth.

- [ ] **Step 1: Add supersedes note to `templates/README.md`**

Add near the top: a short line that `templates` supersedes the archived `wendylabsinc/samples` repo, and that all samples now live here as `wendy init --template` archetypes.

- [ ] **Step 2: Commit the templates README change**
```bash
git add README.md
git commit -m "docs: note templates supersedes the archived samples repo"
```

- [ ] **Step 3: Replace `../samples/README.md` (in the samples repo, its own branch/PR)**

In `../samples`: create a branch, replace `README.md` with a short notice: "This repo is archived. All samples moved to wendylabsinc/templates; use `wendy init --template <name>`." Link to templates. Commit + open PR in `samples`.
> Archiving the GitHub repo (making it read-only) is a manual GitHub action to perform **after** the templates consolidation PR merges — not part of this plan's automated steps. Note it in the consolidation PR description.

- [ ] **Step 4: Verify** both READMEs render and links are correct (`grep -n samples README.md`, visual check).

---

### Task 17: Open consolidation PR + PR-checklist for templates-repo PRs

**Files:**
- Modify: none (PR authoring)

**Interfaces:**
- Consumes: branch `jo/samples-consolidation` with Tasks 1–16 committed.
- Produces: one PR against `templates:main` with a source-mapping table; a tracked checklist of the 10 templates-repo PRs.

- [ ] **Step 1: Push branch**
```bash
git push -u origin jo/samples-consolidation
```

- [ ] **Step 2: Open the PR** with a body that includes:
  - A table mapping each imported archetype → its `samples` source path / PR.
  - A per-app "verification status" list marking which apps are build-only (GPU/Jetson) and need on-device follow-up.
  - The note that `wendylabsinc/samples` should be archived after merge.

- [ ] **Step 3: Add the templates-repo PR checklist to the PR body (tracking only, not landed here):**
  - [ ] #72 hermes-agent/claude — finalize & land (currently this repo's other branch)
  - [ ] #53 go2-rosbag status — small, verified, merge
  - [ ] #60 wifi-sensing — merge (well-tested)
  - [ ] #62 ros2-talker-listener (swift) — merge
  - [ ] #74 llm Orin/Thor models+hooks — verify JP6 GPU on device, then merge
  - [ ] #59 camera-fleet — merge
  - [ ] #56 camera-feed-vlm — Jetson verify, then merge
  - [ ] #69 go2-mapper — on-device verify, then merge
  - [ ] #61 Isaac Sim RL — on-device verify, then merge
  - [ ] #70 swift/cuda-llm — de-fork mlx-swift + remove hardcoded `sm_87`, then merge

- [ ] **Step 4: Request review.**

---

## Self-Review

**Spec coverage:**
- Phase 1 imports (14 archetypes) → Tasks 1–13 (+ webcam/mlx handled as combined multi-language tasks). ✅
- Phase 2 `ai-security-camera` (PR #13) → Task 14; `camera-benchmark` #12 explicitly deferred in spec, not a task. ✅
- Full-conform (template.json/wendy.json/meta.json/README/deps/naming) → baked into every task via Standard Import Procedure + explicit steps. ✅
- Phase 3 templates PRs as checklist-only → Task 17 Step 3. ✅
- Phase 4 retire samples → Task 16. ✅
- Registry/render validation → Task 15. ✅
- One big consolidation PR + branch off main → Task 0 + Task 17. ✅

**Placeholder scan:** Remaining "confirm/verify" instructions (persist entitlement schema, bluetooth schema, app-group layout, swift template dir naming, TensorRT swift-tools compatibility) are genuine per-source discoveries, each with the exact command to resolve them — not deferred work. Full app source is intentionally copied from `samples` rather than inlined (this is a port); the concrete conform edits (var substitutions, entitlements, manifests, registry) are fully specified.

**Type consistency:** archetype names are used identically across each task's `template.json`, `meta.json` entry, README, and Task 15 sweep list. `swift-mlx-llm` vs directory naming is the one open resolution, explicitly flagged in Task 8 Step 2.

**Known risk carried from spec:** GPU/Jetson apps are build-only-verifiable; every such task marks on-device verification as a PR follow-up rather than claiming success.
