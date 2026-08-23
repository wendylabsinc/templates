# Mojo + MAX port findings

Issues and limitations encountered (or identified up front) while porting WendyTemplates to
[Mojo](https://mojolang.org) + [MAX](https://max.modular.com), for reporting to Modular.

- **Stack versions:** MAX 26.5.0 / Mojo 1.0.0 (2026-08-11 release), nightlies noted per finding
- **Research/report date:** 2026-08-23 (living document — updated as ports land)
- **Device matrix:** Jetson Orin Nano (sm_87), Jetson AGX Thor (sm_110, JetPack 7.x),
  Raspberry Pi 5 (aarch64 CPU), Apple Silicon Mac (Metal)
- **Ports:** `mojo/` directories in this repo

Severity: **blocker** (prevents a port), **major** (forces a workaround or non-Mojo fallback),
**minor** (friction), **docs** (documentation gap). Status: `open` (not yet raised with Modular),
`reported`, `workaround`, `fixed`.

## Summary

| ID | Title | Severity | Category | Status |
|---|---|---|---|---|
| [MMF-001](#mmf-001) | No object-detection / classical-CV architectures in MAX | blocker | missing-feature | open |
| [MMF-002](#mmf-002) | ONNX + TorchScript ingestion removed with no edge migration path | blocker | missing-feature | open |
| [MMF-003](#mmf-003) | No speech modality; Whisper pipeline in-tree but unpublished | blocker | missing-feature | open |
| [MMF-004](#mmf-004) | iGPU VMM allocator crash (upstream #6961) expected on Jetson | major | bug | open — Spike 0 will confirm |
| [MMF-005](#mmf-005) | Driver ≥580 / CUDA 13 floor vs JetPack 6 (sm_87 ptxas escape hatch unconfirmed) | major | packaging | open — Spike 0 will confirm |
| [MMF-006](#mmf-006) | Serving auto-tuner unsafe on unified-memory devices (hard-freeze class) | major | bug | workaround |
| [MMF-007](#mmf-007) | "ARM64 Neoverse N1 or newer" requirement contradicts Jetson Orin (Cortex-A78AE) listing | docs | docs | open |
| [MMF-008](#mmf-008) | Telemetry opt-out and "material additional functionality" clause unclear for edge templates | major | licensing | open |
| [MMF-009](#mmf-009) | `mojo build` output dynamically linked; no slim-container packaging story | minor | packaging | open |
| [MMF-010](#mmf-010) | No ESP32 / Xtensa / bare-metal Mojo target | blocker | missing-feature | open |
| [MMF-011](#mmf-011) | No stdlib networking / HTTP / WebSocket | major | missing-feature | open |
| [MMF-012](#mmf-012) | MAX Graph API is Python-only; Mojo-native graph building deprecated | major | missing-feature | open |
| [MMF-013](#mmf-013) | Apple-GPU (Metal) serving coverage is a moving subset | minor | missing-feature | open — spike planned |
| [MMF-014](#mmf-014) | Calling Mojo from Python is beta (≤6 `PythonObject` args) | minor | missing-feature | open |
| [MMF-015](#mmf-015) | No WebRTC / DDS / ROS 2 ecosystem reachable from Mojo | major | missing-feature | open |
| [MMF-016](#mmf-016) | CPU encoding resolution broken for bf16-safetensors models on aarch64 | major | bug | open — verified |

---

## MMF-001: No object-detection / classical-CV architectures in MAX <a name="mmf-001"></a>

- **Template(s):** `camera-feed-yolo` (all 5 language variants run YOLOv8n) · **Devices:** all
- **Category:** missing-feature · **Severity:** blocker (for a drop-in port)
- **Detail:** MAX 26.5's supported-models list (https://max.modular.com/models, 63 architectures)
  contains no object-detection, segmentation, or classical-CV architecture — modalities are
  text-to-text, image-to-text (VLM), image generation, embeddings, and video. There is no path to
  serve YOLO-class models short of hand-building the network in the MAX Graph Python API with
  `max serve --custom-architectures`, plus custom Mojo ops for anything missing.
- **Expected:** an inference platform pitched at heterogeneous hardware can run the most common
  edge perception model family.
- **Actual:** robotics perception templates cannot move their model to MAX without reimplementing
  the architecture by hand.
- **Notes for Modular:** MAX's own kernel library already ships ONNX-semantics `nms.mojo`,
  `resize.mojo`, `pool.mojo`, `gather_scatter.mojo` — much of the op surface appears to exist;
  what's missing is the architecture + a detection modality. Our port (`mojo/camera-feed-yolo`)
  will hand-build YOLOv8n as a MAX graph and log every missing op here.
- **Upstream:** not yet filed.

## MMF-002: ONNX + TorchScript ingestion removed with no edge migration path <a name="mmf-002"></a>

- **Template(s):** `camera-feed-yolo` (yolov8n.onnx), `voice-ai-pipecat` (Piper TTS `.onnx`,
  openWakeWord `.onnx`, Silero VAD) · **Devices:** all
- **Category:** missing-feature · **Severity:** blocker
- **Detail:** the 2024-era MAX Engine ingested ONNX and TorchScript. TorchScript support was
  removed in 25.4 ("Removed support for TorchScript and torch MLIR models") and 25.5; the ONNX
  path is likewise gone (the old `model-formats` docs page 404s; the current C API reference has
  zero mentions of ONNX). Current ingestion is Hugging Face-architecture matching over
  safetensors/GGUF weights.
- **Expected:** a migration path for the large edge-model zoo distributed as ONNX (YOLO exports,
  Piper voices, wake-word models, VAD models).
- **Actual:** four of the five model artifacts in this repo's AI templates are ONNX; none can be
  loaded by MAX 26.5. Stale search-indexed docs (docs.modular.com caches) still claim ONNX
  support, compounding the confusion (see MMF-007 notes on the docs migration).
- **Upstream:** not yet filed. Ask: was ONNX removal announced anywhere? (We could not find it in
  any release note.)

## MMF-003: No speech modality; Whisper pipeline in-tree but unpublished <a name="mmf-003"></a>

- **Template(s):** `voice-ai-pipecat` (faster-whisper STT, Piper TTS, openWakeWord) · **Devices:** all
- **Category:** missing-feature · **Severity:** blocker (for the voice template)
- **Detail:** the supported-models table lists no speech-to-text or text-to-speech modality. Yet
  `max/python/max/pipelines/architectures/whisper/` exists in the 26.5 tree (encoder.py, graph.py,
  model.py, weight_adapters.py, 2026 copyright) — unpublished and undocumented.
- **Plan:** timeboxed experiment serving the in-tree Whisper via
  `max serve --custom-architectures`; result (either way) recorded here.
- **Upstream:** not yet filed. Ask: what is the status/roadmap of the in-tree whisper pipeline?

## MMF-004: iGPU VMM allocator crash (upstream #6961) expected on Jetson <a name="mmf-004"></a>

- **Template(s):** every GPU port (`mojo/llm`, `mojo/camera-feed-yolo`, `mojo/gpu-hello`)
  · **Devices:** Orin Nano (sm_87), AGX Thor (sm_110) — both integrated GPUs
- **Category:** bug · **Severity:** major
- **Detail:** MAX's VMM defragmenting allocator calls `cuMemCreate` with
  `allocFlags.gpuDirectRDMACapable = 1`. On integrated GPUs
  `CU_DEVICE_ATTRIBUTE_GPU_DIRECT_RDMA_WITH_CUDA_VMM_SUPPORTED = 0` while
  `CU_DEVICE_ATTRIBUTE_VIRTUAL_MEMORY_MANAGEMENT_SUPPORTED = 1`, so the call fails:
  `RuntimeError: Failed to capture graph: preBackForCapture vmmCreate failed: CUDA call failed:
  CUDA_ERROR_INVALID_DEVICE`. Confirmed upstream on DGX Spark GB10; the root cause is an iGPU
  property, not an sm_121 property, so Jetson Orin/Thor are expected to reproduce.
- **Workarounds (upstream-verified on GB10):** `MODULAR_DEVICE_CONTEXT_MEMORY_MANAGER_VMM=0`
  or `max serve --no-device-graph-capture`. Our Jetson Dockerfile stages bake the env var in.
- **Repro on Jetson:** pending Spike 0 (`max serve` any model on Orin/Thor without the env var).
- **Upstream:** https://github.com/modular/modular/issues/6961 (filed 2026-08-22 for DGX Spark).
  Suggest the fix gate on `CU_DEVICE_ATTRIBUTE_INTEGRATED` generally, and that an iGPU config
  enter CI.

## MMF-005: Driver ≥580 / CUDA 13 floor vs JetPack 6 <a name="mmf-005"></a>

- **Template(s):** all GPU ports on JetPack 6 devices · **Devices:** Orin family on L4T r36
- **Category:** packaging · **Severity:** major
- **Detail:** since 26.2, MAX bundles a CUDA 13.1 `libnvptxcompiler` and requires NVIDIA driver
  ≥580. On Jetson the driver is baked into L4T and not independently upgradable; JetPack 6 is
  CUDA 12.x with an older driver. Documented escape hatch:
  `MODULAR_NVPTX_COMPILER_PATH=/usr/local/cuda/bin/ptxas` (system ptxas). Since the `OrinNano`
  stdlib target only needs `+ptx81` (CUDA 12.1-era), a JetPack 6 ptxas *should* suffice —
  **no public confirmation exists for this combination.**
- **Repro:** pending Spike 0 on a JetPack 6 Orin (if available; our Orin Nano's JetPack version
  to be recorded here).
- **Upstream:** context in https://forum.modular.com/t/support-for-older-nvidia-gpus-and-drivers-in-mojo-and-max/2761.
  Ask: is JetPack 6 supported via the escape hatch, or is JetPack 7 a hard floor?

## MMF-006: Serving auto-tuner unsafe on unified-memory devices <a name="mmf-006"></a>

- **Template(s):** `mojo/llm`, any `max serve` use · **Devices:** all Jetson, Apple Silicon, DGX Spark
- **Category:** bug · **Severity:** major (safety-relevant on robots)
- **Detail:** `max serve` heuristics size batch/KV-cache for datacenter GPUs (upstream report shows
  512 batches × 131k context attempted on a desk device). On unified memory this can consume all
  system RAM and hard-freeze the machine (DGX Spark report: screen corruption, reboot required;
  `--max-batch-size 1 --max-length 10000` did not save it in that instance). On a robot, an OOM
  that locks the host is a safety failure, not a perf bug.
- **Workaround:** always pass explicit `--max-batch-size`, `--max-length`,
  `--device-memory-utilization` on unified-memory devices. Our Pattern C templates hard-require
  these flags.
- **Upstream:** discussed in Modular forum (DGX Spark thread, 2026-03). Ask: can the tuner detect
  unified memory (`CU_DEVICE_ATTRIBUTE_INTEGRATED`) and default conservatively?

## MMF-007: "ARM64 Neoverse N1 or newer" vs Jetson Orin's Cortex-A78AE <a name="mmf-007"></a>

- **Category:** docs · **Severity:** docs
- **Detail:** https://max.modular.com/packages lists the Linux CPU requirement as "ARM64 Neoverse
  N1 or newer (for example, AWS Graviton2 and later)" while the same page lists Jetson Orin
  (whose CPU is Cortex-A78AE — not a Neoverse core, though Armv8.2-A feature-parity) as a
  compatible GPU. The two statements are in tension; presumably the requirement means the
  Armv8.2-A feature level.
- **Related docs gaps:** arm64 multi-arch container images exist on Docker Hub
  (`modular/max-nvidia-full`/`-base`, verified 2026-08-23) but https://max.modular.com/container/
  never says so; stale `docs.modular.com` caches (pre-migration to max.modular.com / mojolang.org)
  still serve 24.x-era claims (ONNX support, old version numbers) via search engines.
- **Upstream:** not yet filed.

## MMF-008: Telemetry + "material additional functionality" clause for edge templates <a name="mmf-008"></a>

- **Category:** licensing · **Severity:** major (compliance)
- **Detail:** the Modular Community License (last modified 2026-08-18 — device caps removed,
  which is genuinely great for edge) retains: (1) telemetry — "telemetry, usage, and other data
  which captures Your interactions with and use of MAX", with no documented opt-out; problematic
  for air-gapped/regulated robot deployments; (2) redistribution requires "material additional
  functionality, beyond the included portions of MAX" — unclear how a device *template* that
  wraps `max serve` measures against this; (3) attribution and "Powered by Modular" branding
  requirements for commercial AI service providers.
- **Upstream:** not yet filed. Ask for: documented telemetry opt-out; guidance on the
  material-additional-functionality bar for template/scaffold products.

## MMF-009: `mojo build` output is dynamically linked <a name="mmf-009"></a>

- **Template(s):** every Pattern A (CPU Mojo binary) template · **Category:** packaging · **Severity:** minor
- **Detail:** `mojo build` produces a dynamically linked binary with Mojo runtime `.so`
  dependencies (and historically hardcoded paths), despite older docs implying static output.
  There is no documented "copy these N libs" contract for a slim final container stage.
- **Measured 2026-08-23 (Mojo 1.0.0, linux/arm64):** a hello-world binary is 42 KB and links
  exactly three Mojo runtime libs from the wheel's `lib/` — `libKGENCompilerRTShared.so`
  (1.2 MB), `libAsyncRTRuntimeGlobals.so` (669 KB), `libMSupportGlobals.so` (49 KB) — plus
  `libstdc++`/`libgcc_s`. Copying those three libs (~1.9 MB total) next to the binary and setting
  `LD_LIBRARY_PATH` runs correctly in a bare `debian:bookworm-slim`. So a slim final stage IS
  practical — it just isn't documented or guaranteed stable across releases.
- **Status: workaround** (undocumented). Ask Modular to document/stabilize the runtime-lib
  contract or add a `--static` / bundle mode.
- **Upstream:** https://github.com/modular/modular/issues/898.

## MMF-010: No ESP32 / Xtensa / bare-metal Mojo target <a name="mmf-010"></a>

- **Template(s):** `blink-led`, `hello-world` (platform `wendy-lite` = ESP32) · **Severity:** blocker
- **Detail:** Mojo 1.0 targets Linux x86-64/aarch64 and macOS Apple Silicon. No Xtensa or
  bare-metal RISC-V target, no `no_std`-style embedded story. These two templates cannot be
  ported at all. Competitive datapoint: Swift Embedded (`enableExperimentalFeature("Embedded")`,
  wasm/ESP32) covers this today in the same repo.
- **Upstream:** not yet filed (feature request material).

## MMF-011: No stdlib networking / HTTP / WebSocket <a name="mmf-011"></a>

- **Template(s):** every browser-facing port (14 of 17) · **Severity:** major
- **Detail:** Mojo 1.0's stdlib has no sockets/HTTP/WebSocket module suitable for serving.
  **Probed 2026-08-23 (Mojo 1.0.0):** no `std.net` (nor any socket module); no `std.json`;
  `std.hashlib` exposes only the generic `Hasher`/`default_hasher` (hash-table hashing) — no
  SHA-1/SHA-256/MD5, so the RFC 6455 `Sec-WebSocket-Accept` digest must be hand-implemented
  (`std.base64.b64encode` does exist). `std.ffi` provides `external_call` + `OwnedDLHandle`
  (note: pre-1.0 `DLHandle` was renamed), so libc socket FFI is workable.
- **Ecosystem:** `lightbug_http` — the community HTTP framework — was **archived 2026-05-12**;
  its successors are `thatstoasty/floki` (HTTP *client* only) and `bgreni/EmberJson` (JSON).
  There is currently no maintained Mojo HTTP *server*, WebSocket, or JSON stdlib story.
  We are hand-rolling `common/mojo/wendynet`; papercuts will be appended here.
- **Upstream:** not yet filed.

## MMF-012: MAX Graph API is Python-only <a name="mmf-012"></a>

- **Template(s):** `mojo/camera-feed-yolo` (hand-built YOLOv8n graph) · **Severity:** major
- **Detail:** the Mojo-native Graph/Engine/Driver APIs were deprecated in 25.3 in favor of the
  Python graph API. A "full Mojo" application must therefore either drive Python from Mojo
  (mature direction, our approach) or keep a Python sidecar for model definition. Model
  *definition* in Mojo — the language's original pitch — is not currently possible against MAX.
- **Upstream:** not yet filed. Ask: is a Mojo-native graph-building API planned post-1.0?

## MMF-013: Apple-GPU (Metal) serving coverage is a moving subset <a name="mmf-013"></a>

- **Template(s):** `mac-llm` port candidate · **Severity:** minor
- **Detail:** per Modular: Apple silicon GPU support (M1–M5) is "actively developing"; Llama,
  Gemma, Nemotron, FLUX.2 families manually run; not in nightly CI; unexercised architectures
  "fail during graph compilation rather than at inference time".
- **Plan:** verification spike on the Mac decides whether an honest `mojo/mac-llm` ships;
  outcome recorded here.

## MMF-014: Calling Mojo from Python is beta (≤6 args) <a name="mmf-014"></a>

- **Severity:** minor · **Detail:** Python→Mojo bindings are "in early development":
  `PyTypeBuilder.add_function()` supports ≤6 `PythonObject` arguments, kwargs only via trailing
  `StringDict`, non-stdlib dependencies need manually built extension modules. This constrains
  hybrid architectures where Python (e.g. a pipecat pipeline) would call into Mojo hot paths —
  the reverse of our primary interop direction, but relevant to `mojo/voice-ai`.
- **Upstream:** documented limitation (https://mojolang.org/docs/manual/python/mojo-from-python/).

## MMF-015: No WebRTC / DDS / ROS 2 ecosystem reachable from Mojo <a name="mmf-015"></a>

- **Template(s):** `go2-rc`/`g1-rc` (WebRTC camera, unitree DDS), `go2-foxglove`,
  `go2-rosbag` (skipped), `ros2-talker-listener` · **Severity:** major
- **Detail:** no Mojo bindings exist for aiortc-class WebRTC, CycloneDDS, or any ROS 2 client.
  Robotics ports must either keep Python services (WebRTC — no C-FFI-sized surface to bind) or
  hand-write FFI + CDR serialization (our `wendydds` plan for CycloneDDS). A rosbag2/mcap
  equivalent effectively cannot be ported.
- **Upstream:** not yet filed (ecosystem gap; useful roadmap signal for Modular's robotics story).

## MMF-016: CPU encoding resolution broken for bf16-safetensors models on aarch64 <a name="mmf-016"></a>

- **Template(s):** `mojo/llm` CPU path (RPi 5, Jetson CPU fallback) · **Devices:** linux/arm64
  (verified in Docker on Apple Silicon; CPU-only config-validation code paths, device-agnostic)
- **Category:** bug · **Severity:** major — **verified 2026-08-23 on MAX 26.5.0**
- **Detail:** serving a standard bf16-safetensors LLM repo on an aarch64 CPU fails in every
  encoding configuration. Repro (`HuggingFaceTB/SmolLM2-135M-Instruct`, Llama arch, bf16
  safetensors only):
  1. `max serve --model ... --devices=cpu` (no encoding) → defaults to `q4_k`, then
     `ValueError: compatible weights cannot be found for 'q4_k'` (repo has no GGUF).
  2. `--quantization-encoding bfloat16` → `ValueError: The encoding 'bfloat16' is not
     compatible with the selected device type 'cpu'` — although MAX 26.3 release notes added
     bf16 for ARM CPU in MAX graphs; the pipeline validator still refuses it.
  3. `--quantization-encoding float32` → `ValueError: Cannot cast from 'float32' to 'bfloat16'
     on device ... 'bfloat16' is not supported on this device` — the resolver attempts a
     float32→bfloat16 cast (backwards) instead of bf16→f32.
  4. GGUF-only repos as `--model` fail separately: `FileNotFoundError: No config.json ... found`
     (GGUF metadata is not used for config).
  5. The GGUF `--weight-path` escape route rejects real-world community GGUFs: both
     `SmolLM2-135M-Instruct-Q4_K_M.gguf` and `...-Q4_0.gguf` (bartowski) crash the model worker
     with `KeyError: <GGMLQuantizationType.Q8_0: 8>` — llama.cpp-convention files store
     `output.weight`/`token_embd.weight` as Q8_0, and in
     `max/graph/weights/load_gguf.py` `_FROM_QUANTIZED_GGML_DTYPES` has Q8_0 (plus Q5_0, Q8_1,
     Q2_K, Q3_K, IQ*) commented out — only Q4_0, Q4_K, Q5_K, Q6_K are wired. A load-time
     validation would at least fail cleanly instead of a worker crash mid-startup.
  6. `--weight-path other-repo/file.gguf` is not parsed as cross-repo — the basename is searched
     inside the `--model` repo only; a separate manual download is required.
- **Expected:** `--devices=cpu` with a plain bf16-safetensors repo serves via an automatic
  bf16→f32 upcast, or at minimum `--quantization-encoding float32` works; common GGUF files load.
- **Actual:** no working encoding for safetensors-only repos on aarch64 CPU, and typical
  community GGUFs crash on Q8_0 tensors. Modular's own `modularai/SmolLM-135M-Instruct-FP32`
  repo (an FP32 fork of a bf16 model) appears to exist precisely to work around this.
- **Upstream:** not yet filed.

---

## Appendix A: per-template port status

| Template | Verdict | Non-Mojo remainder → finding |
|---|---|---|
| `mojo/gpu-hello` | portable | — |
| `mojo/llm` | portable | Open WebUI (JS frontend, out of scope); serving config → MMF-004/005/006 |
| `mojo/simple-api` | portable w/ workarounds | hand-rolled HTTP → MMF-011 |
| `mojo/camera-feed` | portable w/ workarounds | → MMF-011 |
| `mojo/audio` | portable w/ workarounds | → MMF-011 |
| `mojo/camera-feed-yolo` | partial | `model.py` graph definition → MMF-001/002/012 |
| `mojo/fullstack` | partial | React frontend (JS in every variant, not a gap) → MMF-011 |
| `mojo/ros2-talker-listener` | portable w/ workarounds | hand CDR + FFI → MMF-015 |
| `mojo/voice-ai` | partial (v1 = LLM leg only) | pipecat/STT/TTS stay Python → MMF-002/003 |
| `mojo/go2-initial-test` | partial, deferred (no robot) | hardware services → MMF-015 |
| `mojo/go2-rc`, `mojo/g1-rc` | partial, deferred (no robot) | WebRTC camera, unitree SDK → MMF-015 |
| `mojo/go2-foxglove` | partial, deferred (no robot) | DDS deserialization → MMF-015 |
| `mojo/realsense-camera` | partial (needs D415) | librealsense FFI effort |
| `mojo/rc-car` | partial, deferred (no car) | proprietary Angstrong SDK service |
| `go2-rosbag` | not ported | ROS 2 tooling orchestration → MMF-015 |
| `mac-llm` | pending spike | → MMF-013 |
| `blink-led`, `hello-world` | **blocked** | → MMF-010 |

## Appendix B: device validation log

Populated as spikes and ports run. Format: date · device · JetPack/L4T · MAX version · what ran · result.

- **2026-08-23 · Docker linux/arm64 VM (Apple Silicon host, 16 vCPU / 8 GiB) · MAX 26.5.0 / Mojo 1.0.0:**
  - `mojo build` hello-world: 42 KB binary, 1 s build, runs in bare `debian:bookworm-slim` with
    3 copied runtime libs (~1.9 MB) → MMF-009 workaround verified.
  - stdlib probes: no `std.net`/`std.json`/SHA-*; `std.base64`, `std.ffi.external_call`,
    `OwnedDLHandle`, `std.subprocess`, Python interop (`std.python`) all work → MMF-011.
  - `max serve --devices=cpu`: six failure modes across encodings/GGUF sources → MMF-016.
  - `max serve --model modularai/SmolLM-135M-Instruct-FP32 --devices=cpu --max-batch-size 1
    --max-length 2048`: **works end-to-end** — `/v1/chat/completions` returned 128 tokens in
    1.98 s (~65 tok/s incl. prefill). CPU serving is real once weights are curated.
  - `max.driver` on CPU: `accelerator_count()=0`, `CPU()` device enumerates; API surface
    includes `scan_available_devices`, `accelerator_architecture_name` (basis for `gpu-hello`).
  - Caveat: arm64 VM ≠ Jetson/RPi silicon; GPU paths, #6961, and JetPack ptxas checks still
    require the physical devices (pending — Orin Nano/Thor not currently network-reachable).

- **2026-08-23 · same environment · Spike 1 (wendynet seed):** pure-Mojo HTTP + RFC 6455
  WebSocket echo server (`common/mojo/wendynet/ws_echo.mojo`, ~280 lines: libc socket FFI,
  hand-rolled SHA-1, frame codec) compiles and passes a 6-case raw-socket test suite
  (handshake digest verified independently, 2560-byte extended-length echo, ping/pong, close).
  Confirms MMF-011's workaround is viable, at the cost of implementing SHA-1 and HTTP by hand.

Mojo 1.0 porting notes (language changes hit during the spikes, for template authors): `fn`
removed (use `def`); stdlib now under `std.*`; `def` no longer implicitly raises (`def main()
raises:`); `DLHandle` → `OwnedDLHandle`; `UnsafePointer` → `Pointer`, heap buffers via
`List` (`unsafe_uninit_length=`), pointer `+` → `unsafe_offset()`; `List` is not implicitly
copyable (`return out^`); `len(String)` removed (`byte_length()` / `codepoints()` /
`graphemes()`), plain string slicing replaced by keyword forms (`s[byte=...]`); SIMD shift
RHS must match the operand type exactly (`x >> UInt32(k)`).
