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
- **Measurement:** Spike 1 will record the actual required `.so` set and the image-size delta
  between binary+libs and keeping the full wheel installed.
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
- **Detail:** Mojo 1.0's stdlib has no sockets/HTTP/WebSocket module suitable for serving; the
  community `lightbug_http`'s Mojo-1.0 health is unverified. We are hand-rolling
  `common/mojo/wendynet` (POSIX sockets via libc `external_call`, HTTP/1.1, RFC 6455) — every
  papercut found will be appended here as sub-findings.
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

*(empty — Spike 0 pending)*
