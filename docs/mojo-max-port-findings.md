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
| [MMF-003](#mmf-003) | No speech modality; Whisper pipeline in-tree but unpublished | blocker | missing-feature | open — experiment done: no serving task to register against |
| [MMF-004](#mmf-004) | iGPU VMM graph-capture failure (upstream #6961) **confirmed on Jetson Orin** | major | bug | workaround — verified |
| [MMF-005](#mmf-005) | Driver ≥580 / CUDA 13 floor vs JetPack 6 (sm_87 ptxas escape hatch unconfirmed) | major | packaging | open — Spike 0 will confirm |
| [MMF-006](#mmf-006) | Serving auto-tuner unsafe on unified-memory devices (hard-freeze class) | major | bug | workaround |
| [MMF-007](#mmf-007) | "ARM64 Neoverse N1 or newer" requirement contradicts Jetson Orin (Cortex-A78AE) listing | docs | docs | open |
| [MMF-008](#mmf-008) | Telemetry opt-out and "material additional functionality" clause unclear for edge templates | major | licensing | open |
| [MMF-009](#mmf-009) | `mojo build` output dynamically linked; no slim-container packaging story | minor | packaging | open |
| [MMF-010](#mmf-010) | No ESP32 / Xtensa / bare-metal Mojo target | blocker | missing-feature | open |
| [MMF-011](#mmf-011) | No stdlib networking / HTTP / WebSocket | major | missing-feature | open |
| [MMF-012](#mmf-012) | MAX Graph API is Python-only; Mojo-native graph building deprecated | major | missing-feature | open |
| [MMF-013](#mmf-013) | Apple-GPU (Metal) serving coverage is a moving subset | minor | missing-feature | open — spiked 2026-08-31: serving works on Apple GPU |
| [MMF-014](#mmf-014) | Calling Mojo from Python is beta (≤6 `PythonObject` args) | minor | missing-feature | open |
| [MMF-015](#mmf-015) | No WebRTC / DDS / ROS 2 ecosystem reachable from Mojo | major | missing-feature | open |
| [MMF-016](#mmf-016) | CPU encoding resolution broken for bf16-safetensors models on aarch64 | major | bug | open — verified |
| [MMF-017](#mmf-017) | `max serve` JIT-compiles Mojo at runtime → undocumented C-toolchain requirement, opaque failure | minor | docs | open — verified |
| [MMF-018](#mmf-018) | Cold-start graph compile is minutes on edge CPUs; no precompiled-cache distribution | minor | missing-feature | open — verified |
| [MMF-019](#mmf-019) | `max serve` requires network access to start a fully-cached model (offline crash-loop) | major | bug | open — verified |
| [MMF-020](#mmf-020) | `external_call` re-declaring a libc symbol the stdlib uses fails LLVM lowering with an opaque error | minor | bug/docs | open — verified |
| [MMF-021](#mmf-021) | 1×1 `conv2d` fails CPU compilation: no kernel for `layout_transform_RSCF_to_KNkni` | major | bug | workaround — verified |
| [MMF-022](#mmf-022) | `resize_nearest` cannot reproduce torch/ONNX nearest-2× upsampling | minor | bug/docs | workaround — verified |
| [MMF-023](#mmf-023) | Graph compilation needs 5.7–7.2 GB per YOLOv8n sub-graph, never freed; inline weight constants OOM the folder | major | bug | workaround — verified |
| [MMF-024](#mmf-024) | MEF store is positional per-session; non-default `InferenceSession` options conflict with the implicit global context | minor | bug/docs | workaround — verified |
| [MMF-025](#mmf-025) | `weights_registry` weights fault iGPU kernels with `CUDA_ERROR_ILLEGAL_ADDRESS` | major | bug | workaround — verified |
| [MMF-026](#mmf-026) | GPU conv kernels ~10-40× below par on Jetson Orin (522 ms YOLOv8n@320 vs 57 ms on the same device's CPU) | major | performance | open — verified |

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
  hand-builds YOLOv8n as a MAX graph and logs every missing op here.
- **Hand-build outcome (2026-08-26, MAX 26.5.0, arm64 CPU):** the op surface is **complete** —
  `conv2d`/`silu`/`max_pool2d`/`chunk`/`concat`/`softmax`/`matmul` and friends cover all of
  YOLOv8n; the graph's raw output matches the fused ultralytics model to 2e-6 (class scores) /
  0.002 px (boxes) and runs 13.5 ms/frame at imgsz 320 on an M-series Docker VM CPU. What it
  took: 1×1-conv matmul rewrite (MMF-021), broadcast upsample (MMF-022), 3-way graph split with
  per-process MEF precompilation (MMF-023), external weight registry (MMF-023). So the gap is
  purely the missing architecture/modalities — the kernels are there.
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
- **Experiment (2026-08-31, MAX 26.5.0 wheel):** serving the in-tree Whisper via
  `max serve --custom-architectures` is **not possible**, and the gap is structural, not a
  missing registration:
  - The in-tree module is a partial graph implementation only — `encoder.py`, `graph.py`,
    `model.py`, `weight_adapters.py`. No `arch.py`/`__init__.py`, no tokenizer, no config, no
    batch processor, and no context type (`model.py` carries a literal
    `TODO: Need specific Context type`).
  - `--custom-architectures` registers modules exposing an `ARCHITECTURES` list of
    `SupportedArchitecture`, and every `SupportedArchitecture` must name a
    `task: PipelineTask`. `PipelineTask` in 26.5 is exactly {`TEXT_GENERATION`,
    `EMBEDDINGS_GENERATION`, `PIXEL_GENERATION`, `UNDEFINED`} — **there is no speech task**,
    and `max/serve` has no `/v1/audio/*` route to expose one through.
  - Conclusion: a speech leg on MAX needs upstream serving-layer work (a transcription task +
    endpoint), not an out-of-tree architecture shim. The voice template's STT/TTS therefore
    stay on faster-whisper/Piper (see `mojo/voice-ai-pipecat`).
- **Upstream:** not yet filed. Ask: what is the status/roadmap of the in-tree whisper pipeline,
  and is a speech `PipelineTask` + `/v1/audio/transcriptions` route planned?

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
- **CONFIRMED on Jetson Orin Nano, 2026-08-23** (MAX 26.5.0, JP7.2 / L4T r39.2, driver 595.78,
  CUDA 13.2, 8 GB): serving `HuggingFaceTB/SmolLM2-135M-Instruct` with
  `--devices=gpu --max-batch-size 1 --max-length 512 --device-memory-utilization 0.2` and
  graph capture + VMM enabled crashes at the same call site as #6961:
  `RuntimeError: Failed to capture graph: preBackForCapture vmmCreate failed: CUDA call
  failed: CUDA_ERROR_OUT_OF_MEMORY` — note **OUT_OF_MEMORY on sm_87 vs INVALID_DEVICE on
  GB10/sm_121**, at a memory fraction (~1.5 GB) that trivially fits a 135M model, so this is
  the VMM-on-iGPU path failing, not genuine memory exhaustion. With
  `MODULAR_DEVICE_CONTEXT_MEMORY_MANAGER_VMM=0` + `--no-device-graph-capture`, the same
  command **serves successfully end-to-end on the Orin GPU**.
- **Upstream:** https://github.com/modular/modular/issues/6961 (filed 2026-08-22 for DGX Spark).
  Our Orin repro extends it to sm_87 with a different CUDA error code. Suggest the fix gate on
  `CU_DEVICE_ATTRIBUTE_INTEGRATED` generally, and that an iGPU config enter CI.

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
- **Jetson evidence (2026-08-23, Orin Nano 8GB, JP7.2, MAX 26.5.0):** even with explicit
  conservative-looking flags (`--max-batch-size 1 --max-length 2048
  --device-memory-utilization 0.5`), serving a **135M-parameter** model got the model worker
  **SIGKILLed (OOM)** during "Pre-capturing overlap device graphs" warmup; the VMM=0 retry was
  killed ~1 minute later during its own startup. On unified memory,
  `--device-memory-utilization 0.5` reserves ~3.9 GB of "device" memory that is the same
  physical RAM the host, compile working set, and Python runtime are using — the fraction is
  double-counted. Isolation runs at 0.2 pending.
- **Workaround:** always pass explicit `--max-batch-size`, `--max-length`, and a *much smaller
  than intuitive* `--device-memory-utilization` on unified-memory devices. Our Pattern C
  templates hard-require these flags.
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
  for air-gapped/regulated robot deployments. **Observed on-device (2026-08-23, Orin Nano):
  `max serve` POSTs to `https://telemetry.modular.com/v1/metrics` and, when DNS resolution
  fails in the container, dumps full `requests.exceptions.ConnectionError` tracebacks into the
  serving log** (non-fatal, but noisy — and confirms the phone-home is active during serving); (2) redistribution requires "material additional
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
- **GPU case verified on-device (2026-08-23, Orin Nano):** an AOT `--target-accelerator sm_87`
  binary adds exactly one more lib (`libAsyncRTMojoBindings.so`, 1.2 MB) and **executes GPU
  kernels from a `debian:bookworm-slim` final stage** (~110 MB image, no Python, no SDK) with
  the copied libs on `LD_LIBRARY_PATH` — the host driver's `libcuda` is injected by the
  container runtime. This is the packaging pattern `mojo/gpu-hello` ships.
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
- **Verified working (2026-08-26, `mojo/camera-feed-yolo`):** the drive-Python-from-Mojo
  direction holds up: the AOT Mojo binary imports `model.py`, passes its buffer addresses once
  (`Int(list.unsafe_ptr())`), numpy wraps them as zero-copy views
  (`np.ctypeslib.as_array((ctypes.c_float * n).from_address(addr))`), and per-frame interop is
  a single Python call. Conversion syntax that exists in 1.0: `Int(py=obj)` / `Float64(py=obj)`
  (plain `Int(obj)` does not). Pointer-from-address constructors are gone from `Pointer`, which
  is why addresses cross the boundary as `Int` and numpy does the wrapping.
- **Upstream:** not yet filed. Ask: is a Mojo-native graph-building API planned post-1.0?

## MMF-013: Apple-GPU (Metal) serving coverage is a moving subset <a name="mmf-013"></a>

- **Template(s):** `mac-llm` port candidate · **Severity:** minor
- **Detail:** per Modular: Apple silicon GPU support (M1–M5) is "actively developing"; Llama,
  Gemma, Nemotron, FLUX.2 families manually run; not in nightly CI; unexercised architectures
  "fail during graph compilation rather than at inference time".
- **Spike (2026-08-31, Apple Silicon Mac, macOS 26.6.2, Python 3.14.3):** MAX serving on the
  Apple GPU **works**. `pip install modular` (26.5.0) is clean on macOS arm64;
  `max.driver.accelerator_count()` = 1 and `Accelerator()` enumerates the Metal GPU.
  `max serve` picks `gpu[0]` by default and served both models tried, full OpenAI surface
  verified (`/v1/models`, `/v1/chat/completions` incl. streaming + usage):
  - `HuggingFaceTB/SmolLM2-135M-Instruct` (bf16): compile 46 s cold, **~107 tok/s decode**,
    TTFT 0.09–0.5 s, coherent greedy output. (High-temperature gibberish observed is the
    135M model, not a Metal numerics issue — greedy output is clean.)
  - `Qwen/Qwen2.5-1.5B-Instruct` (bf16): compile ~39 s, **~24.3 tok/s decode**, TTFT 0.20 s,
    coherent.
- **Outcome:** the MAX side of a `mojo/mac-llm` port is ready. What blocks the template is
  Wendy-side, not Modular-side: darwin apps currently support only Xcode-scheme run targets
  (`swift/mac-llm`'s shape) — there is no plain-process/pip runtime for a `max serve`
  service on macOS (Appendix W, item 7). Port parked until that lands; the coverage caveat
  above (families outside the manually-run set fail at graph compile) remains the risk to
  re-test per model.

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

## MMF-017: `max serve` JIT-compiles Mojo at runtime → C toolchain required, opaque failure <a name="mmf-017"></a>

- **Template(s):** any `max serve` deployment · **Devices:** verified on Jetson Orin Nano (JP7.2)
- **Category:** docs/packaging · **Severity:** minor — **verified 2026-08-23 on MAX 26.5.0**
- **Detail:** on first CLI invocation, `max serve` imports `max._kv_cache_ops`, which the Mojo
  Python import hook (`mojo/importer.py` → `_compile_mojo_to_so`) compiles to a `.so` at
  runtime. In a slim container without gcc this fails — and the underlying compiler error
  ("unable to find suitable c compiler for linking") is swallowed, surfacing only as
  `ImportError: Import of Mojo module failed due to compilation error.` The docs list a C
  compiler as a *development* requirement; it is effectively a *serving runtime* requirement.
- **Expected:** either ship `_kv_cache_ops` pre-compiled in the wheel, or chain the real
  compile error into the ImportError.
- **Workaround:** install `gcc` + `libc6-dev` in the serving image (adds ~250 MB to slim images).
- **Upstream:** not yet filed.

## MMF-018: Cold-start graph compile is minutes on edge CPUs <a name="mmf-018"></a>

- **Template(s):** `mojo/llm`, all MAX serving · **Devices:** verified on Jetson Orin Nano
- **Category:** missing-feature/perf · **Severity:** minor (major for UX) — **verified 2026-08-23**
- **Detail:** first-run pipeline init for a **135M-parameter** model on the Orin Nano's 6-core
  Cortex-A78AE took **398 s** (graph compile 387.8 s); with a warm on-disk cache the same init
  is **18.8 s** (21× faster). The cache defaults to an ephemeral container path — in containers,
  persisting `XDG_CACHE_HOME` to a volume is essential and undocumented. There is no mechanism
  to ship a precompiled cache with an image (compile-once-distribute-many), which multi-minute
  edge cold starts would seem to warrant.
- **Workaround:** persist `XDG_CACHE_HOME` (our templates mount it on the persist volume).
- **Upstream:** not yet filed.

## MMF-019: `max serve` requires network access to start a fully-cached model <a name="mmf-019"></a>

- **Template(s):** `mojo/llm`, all MAX serving · **Devices:** verified on Jetson Orin Nano — **2026-08-24**
- **Category:** bug · **Severity:** major for edge (devices legitimately boot offline)
- **Repro:** deploy `max serve --model HuggingFaceTB/SmolLM2-135M-Instruct` in a container
  with **no network** but a **fully populated `HF_HOME`** (weights + tokenizer + compiled
  graph cache all on a persist volume from a prior online run).
- **Expected:** serve from the local cache.
- **Actual:** crash-loop at startup — `validate_hf_repo_access()`
  (`max/pipelines/weights/hf_utils.py`) calls `huggingface_hub.repo_info()` over the network
  *before* consulting the cache, and the `ConnectError` is re-raised as
  `ValueError: Failed to access repository … [Errno -3] Temporary failure in name resolution`.
  The worker never checks that every artifact it needs is already local.
- **Workaround (verified):** set `HF_HUB_OFFLINE=1` when the model directory exists under
  `$HF_HOME/hub/` — with it, the same container starts with zero network: pipeline init
  20.6 s (warm cache), server ready, inference works. Our template's entrypoint now
  auto-detects broken DNS + cached weights and sets the flag.
- **Suggestion:** try the cache first (or honor offline mode automatically) when repo
  validation can't reach the Hub; a warning beats a crash-loop on an edge device.
- **Upstream:** not yet filed.

## MMF-020: `external_call` symbol collisions with stdlib externs fail opaquely <a name="mmf-020"></a>

- **Template(s):** any FFI-heavy Mojo code (`wendycam`, `wendynet`) · **Mojo 1.0.0** — **2026-08-24**
- **Category:** bug/docs · **Severity:** minor (but a guaranteed papercut for FFI users)
- **Repro:** in one program, call `external_call["open", c_int](ptr, flags)` (2-arg) and
  also use the stdlib file API (which declares `open` with its own signature); same story
  for `write` vs `print`. Compile with `mojo build`/`mojo run`.
- **Expected:** either both calls lower (C varargs-style symbol reuse) or a clear
  frontend diagnostic naming the conflicting declarations.
- **Actual:** LLVM pipeline failure at the end of compilation —
  `failed to legalize operation 'pop.external_call' that was explicitly marked illegal`
  / `run LowerToLLVMPipeline failed` — pointing at the stdlib's internal call site, not
  the user's. Nothing in the message says "signature conflict for symbol X".
- **Workaround (verified):** call a sibling symbol the stdlib doesn't declare
  (`openat(AT_FDCWD, …)` for `open`; `send` on a socketpair instead of `write` on a
  pipe), or match the stdlib's exact declared shape.
- **Addendum (2026-08-26, found building wendydds):** the same lowering failure hits
  **two of your own `external_call`s to one symbol with different Mojo argument types**
  — e.g. `external_call["memcpy", Int](ptr, addr_as_Int, n)` in one place and
  `external_call["memcpy", Int](ptr, other_ptr, n)` in another. Keep exactly one
  argument shape per symbol across the whole program (the templates standardize on
  `memcpy(dest_ptr, src_as_Int, len_Int)`).
- **Upstream:** not yet filed.

## MMF-021: 1×1 `conv2d` fails CPU compilation (missing layout-transform kernel) <a name="mmf-021"></a>

- **Template(s):** `mojo/camera-feed-yolo` (YOLOv8n is ~half 1×1 convs) · **MAX 26.5.0, arm64 CPU** — **2026-08-26**
- **Category:** bug · **Severity:** major (blocks any CNN with pointwise convs on CPU)
- **Repro:** build a graph with a single `ops.conv2d` whose filter is 1×1 (e.g. 32→32,
  stride 1, RSCF layout), load it in an `InferenceSession` on CPU.
- **Expected:** compiles like the 3×3 case does.
- **Actual:** `'mo.layout.transform' op [MO_TO_MOGG] no kernel registered for
  'layout_transform_RSCF_to_KNkni'` → `[ConstantFold] Unable to load models: Failed to run
  MOToMGP pipeline`. `FilterLayout.FCRS` fails identically; k=3 convs of any channel count
  compile fine, so the KNkni packing path is only reached — and only broken — for 1×1.
- **Workaround (verified, exact):** lower 1×1/stride-1 convs to
  `reshape (1,H,W,C)→(H·W,C)` → `matmul` → bias `add` → `reshape` back; 0.0 output diff.
- **GPU flip side (Jetson Orin, 2026-08-26):** the matmul lowering must NOT be used on GPU —
  model setup dies with `kernel "transpose_mogg_8": CUDA call failed: CUDA_ERROR_ILLEGAL_ADDRESS`
  while repacking a `[512,256] KN → [256,512] NK` matmul weight on the iGPU. In hindsight this
  was the first sighting of MMF-025 (the repacked weight was `weights_registry`-backed); 1×1
  `conv2d` with inline constants works on GPU. Net: the same graph needs per-device lowering —
  conv2d on GPU, matmul on CPU — which also means CPU and GPU MEFs cannot share a graph
  definition.
- **Upstream:** not yet filed.

## MMF-022: `resize_nearest` cannot reproduce torch/ONNX nearest-2× upsampling <a name="mmf-022"></a>

- **Template(s):** `mojo/camera-feed-yolo` (FPN upsample path) · **MAX 26.5.0, arm64 CPU** — **2026-08-26**
- **Category:** bug/docs · **Severity:** minor (easy rewrite, hard-to-spot numerics)
- **Repro:** `ops.resize_nearest(x, [1, 2H, 2W, C], coordinate_transform_mode=2)` (asymmetric)
  on an NHWC tensor vs. plain 2×2 pixel duplication.
- **Expected:** integer-factor nearest upscaling equals pixel duplication (torch
  `nn.Upsample(scale_factor=2, mode="nearest")`, ONNX `Resize` nearest/asymmetric/floor).
- **Actual:** outputs differ on real feature maps for every coordinate/round mode tried; the
  docs don't say which combination (if any) matches the torch/ONNX convention.
- **Workaround (verified, exact):** `reshape (1,H,1,W,1,C)` → `broadcast_to (1,H,2,W,2,C)` →
  `reshape (1,2H,2W,C)` — duplication by construction.
- **Upstream:** not yet filed. Ask: which mode pair is ONNX `nearest`+`asymmetric`+`floor`?

## MMF-023: Graph compilation memory: 5.7–7.2 GB per YOLOv8n sub-graph, never freed <a name="mmf-023"></a>

- **Template(s):** `mojo/camera-feed-yolo` · **MAX 26.5.0, arm64 CPU (Docker VM, 16 vcpu)** — **2026-08-26**
- **Category:** bug · **Severity:** major (locks 8 GB edge devices out of on-device compile)
- **Detail:** compiling the hand-built YOLOv8n as one graph exceeds 7.2 GB and OOMs; split into
  backbone / PAN+detect / DFL-decode the parts peak at 5.7 / 7.2 / 1.4 GB (our template splits
  further into backbone / PAN / detect / decode = 5.7 / 3.1 / 6.0 / 1.4 GB so a marginal 8 GB
  builder VM survives). The footprint is **independent of imgsz** (640 vs 320 identical) — it
  scales with op count, i.e. it's per-kernel codegen, not activation planning. Compiler memory
  is **never released** after `session.load()`: compiling the parts sequentially in one process
  OOMs where each part alone succeeds. Two aggravators: (a) weights inlined as `ops.constant`
  blow up the constant-fold pass (an 8 GB container dies even though the weights total 12 MB) —
  use `ops.constant_external` + `weights_registry`; (b) `OMP_NUM_THREADS`/cpuset made no
  difference. Silver lining: a **local compiler cache** (surviving in image layers) makes
  recompiling an already-seen graph near-free — backbone drops from 5.7 GB / 45 s cold to
  0.4 GB / seconds warm — consistent with MMF-018's 400 s→19 s warm-start observation.
- **Consequence for edge:** an Orin Nano (8 GB shared) cannot compile this CV model on-device
  with anything else resident. Our template precompiles CPU MEFs in the Docker build (one part
  per process) and defers GPU MEFs to a one-time first-boot compile — measured on-device numbers
  to follow in Appendix B.
- **Mitigation that works:** `InferenceSession(export_mefs=…)` / `precompiled_mefs=…` — all
  three MEFs load in 0.1 s at 827 MB resident. This is the missing "precompiled-cache
  distribution" story of MMF-018, and it does work per-machine.
- **Cross-target GPU compile works (verified 2026-08-26, undocumented):**
  `driver.set_virtual_device_api("cuda")` + `set_virtual_device_target_arch("sm_87")` +
  `set_virtual_device_count(1)` on a GPU-less arm64 build machine lets
  `InferenceSession(devices=[Accelerator()], export_mefs=…)` compile Jetson-Orin GPU MEFs —
  our Dockerfile bakes them at image build. These APIs appear in no public docs; they deserve
  a documented "precompile for target device" story (it obsoletes on-device cold compiles,
  MMF-018).
- **Upstream:** not yet filed.

## MMF-024: MEF store and `InferenceSession` option-conflict papercuts <a name="mmf-024"></a>

- **Template(s):** `mojo/camera-feed-yolo` · **MAX 26.5.0** — **2026-08-26**
- **Category:** bug/docs · **Severity:** minor
- **Papercut 1 — positional MEF store:** `export_mefs` names artifacts `000-<graph>.mef` per
  session; several single-graph compile processes sharing one directory overwrite the manifest
  and imports then fail with "graph 0 does not match the precompiled artifact" even though the
  right `.mef` sits beside it. Matching is positional per-session, not by graph name.
  **Workaround:** one subdirectory per graph, one import session each.
- **Papercut 2 — option conflicts with the implicit context:** `InferenceSession(num_threads=N)`
  aborts with `LLVM ERROR: Init::getOrCreateContext() requested an M::Context with different
  Init::Options…` (or the `AsyncRT::getOrCreateCPUDevice` variant) when a `Graph`, `CPU()`, or
  even just module import created the context/device first with defaults. There is no
  supported way to order around it in a normal program.
- **Upstream:** not yet filed.

## MMF-025: `weights_registry` weights fault iGPU kernels (`CUDA_ERROR_ILLEGAL_ADDRESS`) <a name="mmf-025"></a>

- **Template(s):** `mojo/camera-feed-yolo` · **Jetson Orin Nano (sm_87), MAX 26.5.0, JP7.2** — **2026-08-26**
- **Category:** bug · **Severity:** major (silently poisons any GPU graph fed by a registry)
- **Repro:** build the YOLOv8n graph for GPU with every weight as
  `ops.constant_external` + `weights_registry` at `session.load()`; execute on the Orin.
- **Expected:** registry weights are staged into device-accessible memory (that is the
  documented serving path, and it works on CPU).
- **Actual:** the first conv's fused epilogue kernel dies with
  `CUDA_ERROR_ILLEGAL_ADDRESS`. Identical failure whether the MEF was cross-compiled on a
  build machine or compiled on the device itself, so it is a runtime weight-staging issue, not
  a codegen one — consistent with kernels receiving un-mapped host pointers on the
  unified-memory iGPU (MMF-004's assumption family). The matmul-weight `KN→NK` setup repack
  crash in MMF-021's GPU note is the same root cause.
- **Workaround (verified):** inline every weight as `ops.constant` for GPU graphs. (On CPU
  that direction is what OOMs the constant-folder, MMF-023 — the two workarounds are exact
  opposites per device.)
- **Upstream:** not yet filed.

## MMF-026: GPU conv kernels ~10-40× below par on Jetson Orin <a name="mmf-026"></a>

- **Template(s):** `mojo/camera-feed-yolo` · **Jetson Orin Nano (sm_87), MAX 26.5.0, JP7.2** — **2026-08-26**
- **Category:** performance · **Severity:** major (GPU CV is slower than the same device's CPU)
- **Detail:** with MMF-025 worked around, the hand-built YOLOv8n runs end-to-end on the Orin
  GPU but at **522 ms/frame (imgsz 320)** — per-stage (forced syncs): backbone 194 ms, PAN
  135 ms, detect 193 ms, DFL-decode ~1 ms. The conv-dominated parts are uniformly slow while
  the elementwise/softmax decode is fine; effective throughput is ~8 GFLOP/s on a ~2 TFLOP/s
  fp32 part, i.e. the `mo.conv` path appears to hit a naive/fallback kernel on sm_87 (matmul
  workloads — `mojo/llm` serving — perform fine on this device). The **same model on the same
  device's CPU runs 57 ms at imgsz 224**, so our template defaults to CPU and leaves
  `YOLO_DEVICE=gpu` opt-in.
- **Expected:** GPU convolution comfortably ahead of CPU on a 1024-core Ampere iGPU.
- **Upstream:** not yet filed. Ask: is there a tuned conv path for sm_8x, and is the
  fallback-kernel selection observable/loggable?

---

## Appendix A: per-template port status

| Template | Verdict | Non-Mojo remainder → finding |
|---|---|---|
| `mojo/gpu-hello` | portable | — |
| `mojo/llm` | portable | Open WebUI (JS frontend, out of scope); serving config → MMF-004/005/006; offline start → MMF-019 |
| `mojo/simple-api` | portable w/ workarounds | hand-rolled HTTP → MMF-011 |
| `mojo/camera-feed` | portable w/ workarounds | → MMF-011 |
| `mojo/audio` | portable w/ workarounds | → MMF-011 |
| `mojo/camera-feed-yolo` | partial — **verified on Orin** (CPU 57 ms@224; GPU works but MMF-026-slow, opt-in) | `model.py` graph definition → MMF-001/002/012; workarounds MMF-021/022/023/024/025; GPU perf → MMF-026 |
| `mojo/fullstack` | portable w/ workarounds — **verified on Orin** (CRUD + system + camera/audio WS + in-process Mojo GPU kernel route, 122 GFLOPS) | React frontend (JS in every variant, not a gap); hand-rolled HTTP → MMF-011; SQLite via `wendydb` dlopen (no stdlib DB layer, same no-batteries class as MMF-011) |
| `mojo/ros2-talker-listener` | portable w/ workarounds — **container-verified interop with real ROS 2 (both rmw vendors, both directions)** | libddsc FFI + hand-packed topic descriptor → MMF-015 (no hand CDR needed after all: descriptor-based topics let CycloneDDS serialize; no C shim either) |
| `mojo/voice-ai-pipecat` | partial — **v1 shipped: LLM leg on local `max serve`** (Qwen2.5-1.5B validated on Apple GPU; Orin group verification pending bench access) | pipecat/STT/TTS/wake-word stay Python → MMF-002/003 (Whisper experiment: no serving speech task to register against) |
| `mojo/go2-initial-test` | partial, deferred (no robot) | hardware services → MMF-015 |
| `mojo/go2-rc`, `mojo/g1-rc` | partial, deferred (no robot) | WebRTC camera, unitree SDK → MMF-015 |
| `mojo/go2-foxglove` | partial, deferred (no robot) | DDS deserialization → MMF-015 |
| `mojo/realsense-camera` | partial (needs D415) | librealsense FFI effort |
| `mojo/rc-car` | partial, deferred (no car) | proprietary Angstrong SDK service |
| `go2-rosbag` | not ported | ROS 2 tooling orchestration → MMF-015 |
| `mac-llm` | MAX-ready (spike 2026-08-31: Apple-GPU serving verified) — parked on Wendy darwin runtime, Appendix W#7 | → MMF-013 |
| `blink-led`, `hello-world` | **blocked** | → MMF-010 |

## Appendix W: WendyOS platform issues hit during porting (NOT for Modular)

Found while deploying `mojo/llm` as a two-service group on WendyOS 0.18.2. First hit with
CLI/agent 2026.08.18; re-tested 2026-08-24 with **CLI 2026.08.22-053704 + agent
2026.08.22-032001** (matrix results inline):

1. **Group-service containers get an empty network namespace by default** — re-tested on
   2026.08.22: a group deployed with *no* network entitlement (the exact shape of the
   shipped `python/llm`) gets containers whose netns holds **only `lo`** (`ip -brief addr`
   from inside), i.e. **no egress and no ingress** — published ports unreachable from the
   LAN while a single-service app on the same device serves fine. Rewriting
   `/etc/resolv.conf` can't help; there is no interface. **Workaround (verified 2026-08-24):**
   an **app-level** `{ "type": "network", "mode": "host" }` entitlement on a **fresh create**
   attaches host networking — egress (HF download) and LAN ingress both work, and an
   in-place redeploy *keeps* networking if the entitlement was already present at create
   time. Per-service placement of the same entitlement did not work on 2026.08.18 (not
   re-tested since). Consequence: `python/llm`'s group (Ollama pull at runtime) cannot work
   as shipped on these agents either.
2. **Group-service memory cap (~256 MiB) — appears fixed/lifted on agent 2026.08.22,
   REGRESSED on agent "dev" (observed 2026-08-31)**: on 2026.08.18 MAX's estimator saw
   254 MiB ("Model size exceeds available memory (256.60 MiB > 76.25 MiB)") and Open WebUI
   never finished booting; on 2026.08.22 the same group compiles and serves the model (no
   estimator complaint) and Open WebUI boots fully. No memory/resources knob exists in
   wendy.json to have caused this; agent-side change presumed. **2026-08-31, same Orin now
   running agentVersion "dev"** (CLI 2026.08.22-053704): the `mojo/voice-ai-pipecat` group's
   max-serve — same serving image lineage and same SmolLM2-135M that served fine on
   2026.08.22 — crash-loops with "The model 256.60 MiB, activations 0.00 KiB, and signal
   buffers 0.00 KiB don't leave room for KV cache", i.e. the container is again capped at a
   few hundred MiB. The sibling voice-app service in the same group runs uncapped enough
   (425 MB resident) to serve, and host networking still works (W#1 entitlement honored) —
   the cap appears specific to whatever cgroup limit the dev agent assigns.
3. Entitlement changes on an existing app group are not applied by `wendy run` redeploy —
   a full `apps remove` + fresh deploy is required. (Not re-tested on 2026.08.22; the
   workaround in W1 was applied via remove + fresh create.)
4. `wendy device logs` streaming connections drop after ~2 minutes of quiet (WiFi device;
   still observed with 2026.08.22 — "keepalive ping failed" during a long graph compile).
   `wendy device shell`: unsupported by agent with CLI 2026.08.18; with CLI 2026.08.22 it
   connects (interactive TTY only — no one-shot `-- command` use over a pipe).
5. **(new, found 2026-08-24)** `wendy run`'s readiness probe window starts before image
   transfer completes and spans service cold-start; a first-boot graph compile (~4 min) plus
   Open WebUI's first-boot downloads exceeded the template's 300 s `timeoutSeconds`, so
   `wendy run` reports a readiness timeout for a deploy that comes up healthy a minute
   later. Cosmetic, but users will read it as a failed deploy.
6. **(new, found 2026-08-24)** `wendy init` template substitution skips `.mojo` files:
   `isTextFile()` in `go/internal/cli/commands/template.go` (WendyAgent) allowlists
   `.py/.rs/.swift/...` but not `.mojo`, so `{{.PORT}}`-style tokens survive into
   scaffolded Mojo sources and the build fails on a parse error. Until the CLI fix ships,
   the Mojo templates read `PORT` from the environment (set via `ENV PORT={{.PORT}}` in
   the Dockerfile, which *is* substituted); one-line CLI fix proposed in WendyAgent.
7. **(new, found 2026-08-31)** darwin (`platform: "darwin"`) apps support only Xcode-scheme
   run targets (`"xcode": { "scheme": … }` + `run.args`, per `swift/mac-llm`) — there is no
   plain-process or container run mode on the Mac agent. This is the only thing blocking a
   `mojo/mac-llm` template: `max serve` on Apple Silicon itself works (MMF-013 spike) but
   needs a way to run a pip-installed server process as a Wendy app on macOS.

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

- **2026-08-23 · Jetson Orin Nano 8GB · WendyOS 0.18.2 · JetPack 7.2 / L4T r39.2 · driver
  595.78 / CUDA 13.2 · MAX 26.5.0 / Mojo 1.0.0 (pip, python:3.12-slim arm64 container, `gpu`
  entitlement):**
  - **Mojo GPU kernel WORKS on sm_87**: 1M-element vector-add via `DeviceContext` +
    `TileTensor`, zero verification errors; device reported as "Orin (nvgpu)", api cuda.
    First-launch kernel+sync 889 ms (context/module init; subsequent launches not yet measured).
  - **Cross-compilation works**: binary built on an arm64 macOS Docker host with
    `mojo build --target-accelerator sm_87`, executed unmodified on the device. Note
    `has_accelerator()` is compile-time — without the flag, a GPU-less build host silently
    compiles the no-GPU branch.
  - `max.driver` enumerates the iGPU: `accelerator_count()=1`, arch `sm_87`, api cuda.
  - `nvidia-smi` works in-container on JP7.2 (driver 595.78, CUDA 13.2); MAX's bundled ptxas
    path applies (driver ≥580), so MMF-005's JP6 escape-hatch question remains untested here.
  - `max serve` first failed at CLI import: MMF-017 (runtime Mojo JIT needs gcc).
  - With gcc: default-ish config (`--max-batch-size 1 --max-length 2048
    --device-memory-utilization 0.5`) → model worker **SIGKILLed (OOM)** during graph-capture
    warmup; VMM=0 alone also killed (MMF-006 — unified-memory double-counting).
  - **END-TO-END GPU SERVING WORKS** with `MODULAR_DEVICE_CONTEXT_MEMORY_MANAGER_VMM=0` +
    `--no-device-graph-capture` + `--device-memory-utilization 0.2 --max-length 512`:
    `/v1/chat/completions` returned 37 tokens in ~4 s (**~9.2 tok/s incl. prefill**,
    SmolLM2-135M bf16). To our knowledge the first documented `max serve` success on a Jetson.
  - Isolation: re-enabling graph capture + VMM with the same conservative memory settings
    reproduces the #6961 call-site failure as `CUDA_ERROR_OUT_OF_MEMORY` (MMF-004 confirmed;
    error code differs from GB10's `CUDA_ERROR_INVALID_DEVICE`).
  - Compile cache: cold pipeline init 398 s → warm 18.8 s with `XDG_CACHE_HOME` persisted
    (MMF-018).
  - Telemetry: `max serve` POSTs to `telemetry.modular.com/v1/metrics`; DNS failure in the
    container produces full tracebacks in the serve log (MMF-008 evidence; non-fatal).

- **2026-08-23 · same Orin Nano · `mojo/llm` max-serve layer (single-service deploy):**
  full `wendy run` deployment of the template's max-serve container: model download + cold
  compile on a fresh volume, then `/v1/chat/completions` at **~10 tok/s** (SmolLM2-135M bf16,
  GPU, incl. network + prefill). GPU auto-detected via `max.driver.accelerator_count()`
  (`WENDY_HAS_GPU` is a build arg, not a runtime env, in service containers). The two-service
  group form is blocked by the WendyOS issues in Appendix W, not by MAX.

- **2026-08-23 · same Orin Nano · `mojo/simple-api` template (third Mojo port):** deployed
  via `wendy run`, all endpoints verified on-device (GET `/`, `/health`, POST `/items` with
  JSON body parse/escape). Lands `common/mojo/wendynet` as a real package (Listener/Request
  structs, Content-Length body handling, minimal JSON helpers). Slim final image (~90 MB).

- **2026-08-23 · same Orin Nano · `mojo/gpu-hello` template (first Mojo port shipped):**
  deployed via `wendy run` — AOT sm_87 build selected from injected `WENDY_PLATFORM`/
  `WENDY_DEVICE_TYPE` build args, slim no-SDK final image. On-device report:
  `vector_add n=1048576 errors=0 first_launch_us=62166 steady_us=239` ·
  `matmul n=512 errors=0 time_s=0.00184 gflops=145.7` (naive kernel, fp32) · status OK,
  served by the pure-Mojo HTTP layer. Steady-state kernel launch+sync of ~240 µs and
  ~146 GFLOPs naive matmul are healthy sm_87 numbers.

- **2026-08-24 · same Orin Nano · CLI 2026.08.22-053704 / agent 2026.08.22-032001 ·
  `mojo/llm` two-service group (Appendix W re-test + full verification):**
  - Default group (no network entitlement, the `python/llm` shape): containers get an
    **empty netns (lo only)** — no egress, no ingress; max-serve crash-loops on HF repo
    validation *with all weights cached* → new MMF-019. Single-service app on the same
    device unaffected.
  - App-level `{"type":"network","mode":"host"}` + fresh create: egress and LAN ingress both
    work; entitlement now shipped in the template's wendy.json (Appendix W #1 workaround).
  - ~256 MiB group memory cap (W2) not reproducible on agent 2026.08.22 — model compiles,
    serves, and Open WebUI boots fully; appears fixed agent-side.
  - **Group verified end-to-end**: browser/API → Open WebUI :9010 → loopback → max-serve
    :9011 → GPU; 37-token completion in 2.4 s (**~15 tok/s** incl. prefill + LAN,
    SmolLM2-135M bf16). Template bugs found + fixed along the way: open-webui ignores the
    `PORT` env var (bound its default 8080; fixed with explicit `--port` — latent in
    `python/llm`, masked there because its default PORT *is* 8080) and Open WebUI's HF cache
    was ephemeral (now `HF_HOME=/data/hf-cache` on the persist volume).
  - **MMF-019 workaround verified**: no-network fresh create + warm volume + entrypoint
    auto-`HF_HUB_OFFLINE=1` → pipeline init 20.6 s, server ready, fully offline.
  - Cache note: first group-form start re-compiled ~216 s despite the solo run's warm volume
    (cache key appears sensitive to container/env changes); subsequent group restarts are
    warm (13.7–20.6 s).

- **2026-08-24 · same Orin Nano · `mojo/camera-feed` template (fourth Mojo port) · Logitech
  Brio 101 (UVC):**
  - `wendycam` V4L2 FFI stack proven end-to-end on hardware: enumeration via QUERYCAP
    correctly filters the UVC metadata node (exactly `/dev/video0` "Brio 101" listed);
    hand-packed ioctl/struct ABI validated by a C-oracle conformance test before deploy;
    capture probe: 30/30 valid JPEGs at 640x480.
  - **Template verified**: all endpoints (`/`, `/cameras`, `/debug`, `/logs`, `/assets`) +
    WS stream — 60/60 complete JPEG frames at **29.9 fps 1280x720** (~37 KB/frame, full
    camera rate), `switch_camera` round-trip, concurrent browser + scripted clients from
    a single-threaded poll(2) loop. No GStreamer, no Python, ~110 MB image.
  - Found along the way: MMF-020 (extern symbol collisions fail opaquely) and Appendix
    W#6 (`wendy init` does not substitute `.mojo` files; CLI fix proposed in WendyAgent).

- **2026-08-24/25 · same Orin Nano · `mojo/audio` template (fifth Mojo port) · Brio 101 mic:**
  - `wendyaudio` (libasound via `OwnedDLHandle`, `snd_pcm_set_params` so no hw_params struct
    ABI, `/proc/asound` enumeration) verified end-to-end: real-time 16 kHz capture from the
    Brio (probe: 32k samples in 2.008 s; template WS stream measured ~16.0 kHz with live
    room-noise amplitudes), `switch_microphone` acks, wav playback to completion (null
    sink), all endpoints at python/audio parity. Single-threaded poll loop, non-blocking
    ALSA, no GStreamer.
  - Two ALSA-on-Jetson behaviors worth knowing (handled in the template): clockless APE
    I2S capture PCMs **open cleanly but never produce a frame** — GStreamer's preroll
    failure masks this in the python sibling, so the Mojo port adds an explicit
    first-data-within-400 ms check before accepting a device; and HDMI audio sinks with no
    display attached accept the open but never consume — playback aborts with a log after
    ~5 s of zero progress instead of hanging.
  - `OwnedDLHandle` works well as an FFI strategy (`h.call["sym", ret](args)`), and dodges
    the MMF-020 extern-collision problem entirely since symbols resolve at runtime.

- **2026-08-26 · same Orin Nano · `mojo/fullstack` template (sixth Mojo port) · Brio 101:**
  - The FastAPI fullstack backend rebuilt on wendynet in one poll(2) loop, verified
    end-to-end on-device: cars CRUD at exact sibling parity (201/200/404/422/204 status
    behavior, `{"detail":"Car not found"}` bodies, `updated_at` stamping) on SQLite through
    the new **`wendydb`** package (`libsqlite3.so.0` via `OwnedDLHandle`; prepare/bind/step
    with `SQLITE_TRANSIENT` — clean FFI story, no issues, container test suite green);
    `/api/system` from `/proc` + `uname(2)`/`statvfs(2)`; static React SPA byte-identical
    to the python sibling; 145 MB image, no Python/SDK at runtime.
  - **`/api/gpu` runs a real Mojo matmul kernel in-process** (gpu-hello-style AOT
    `--target-accelerator sm_87` cross-compile selected by the same Dockerfile `case` on
    `WENDY_PLATFORM`): first request returns in ~0.6 s including CUDA context creation
    (no JIT — the kernel is AOT), reporting `Orin (nvgpu)`, the gpu-thermal zone, and a
    verified 512³ naive matmul at **122 GFLOPS**, then cached. Where the python sibling
    shells out to nvidia-smi, the Mojo port proves the GPU by using it.
  - Streams at parity: camera WS 14.5 fps sustained (camera-limited — the verified
    `mojo/camera-feed` app measured 12.1 fps the same evening on the same Brio; auto-
    exposure in dim light halves the sensor rate vs the earlier 29.9 fps daytime run);
    audio WS steady-state 19.7 chunks/s ≈ real-time 16 kHz after the first-connect APE
    preroll walk (Appendix note in the audio entry above applies unchanged).

- **2026-08-26 · Docker linux/arm64 (Apple Silicon host) · `mojo/ros2-talker-listener` +
  `wendydds` (seventh Mojo port) · CycloneDDS 0.10.5 / ROS 2 Humble:**
  - **Pure-Mojo ROS 2 pub/sub works with no ROS installation and no C shim**: `wendydds`
    dlopens `libddsc.so.0` and hand-packs the `dds_topic_descriptor_t` for
    `std_msgs::msg::dds_::String_` — layout constants conformance-tested against the real
    headers by a C oracle (wendycam-style), the idlc-emitted XTypes
    TypeInformation/TypeObject blobs embedded verbatim, `rt/` topic mangling, ROS default
    QoS (reliable 100 ms / keep-last 10 / volatile). Descriptor-based topics mean
    CycloneDDS serializes the C-shaped sample itself, so the anticipated hand-CDR layer
    (and swift-ros2's C sertype bridge) is not needed for plain message types.
  - **Interop matrix (containers, domain 0): all green.** Mojo talker →
    `ros2 topic echo /chatter std_msgs/msg/String` under default `rmw_fastrtps_cpp`
    (cross-vendor RTPS) ✓ · same under `rmw_cyclonedds_cpp` ✓ · `ros2 topic pub` → Mojo
    listener ✓. Loopback suite: 3-sample round trip incl. unicode + empty string.
  - Two Mojo-FFI gotchas found and worked around (see MMF-020 addendum + porting notes):
    same-symbol/different-shape `external_call`s fail LLVM lowering even without stdlib
    involvement, and ASAP destruction frees a `List` whose raw address was just taken
    unless the value is referenced after the FFI call that consumes the pointer.
  - **On-device (same Orin Nano, WendyOS 0.18.2): verified.** Deployed as a two-service
    group via `wendy init --branch` → `wendy run`; the listener hears every 1 Hz message
    ~20–25 ms after publication, zero drops observed over minutes of logs. Notably the
    group's **per-service** `network: host` entitlements worked on a fresh create with
    agent 2026.08.22 (new data point for Appendix W#1, which had only app-level placement
    working on 2026.08.18).

- **2026-08-31 · Apple Silicon Mac · macOS 26.6.2 · MAX 26.5.0 (pip, Python 3.14.3 venv) ·
  mac-llm spike (MMF-013) + `mojo/voice-ai-pipecat` default-model validation:**
  - `pip install modular` clean on macOS arm64; `max.driver` enumerates the Metal GPU
    (`accelerator_count()=1`); `max serve` defaults to `gpu[0]`.
  - `HuggingFaceTB/SmolLM2-135M-Instruct` (bf16, GPU): compile 46 s cold, ~107 tok/s decode,
    TTFT 0.09–0.5 s. Default-temperature output from the 135M model is incoherent but greedy
    (`temperature=0`) output is clean — model quality, not Metal numerics.
  - `Qwen/Qwen2.5-1.5B-Instruct` (bf16, GPU): weights 4.3 min download, compile ~39 s,
    ~24.3 tok/s decode, TTFT 0.20 s, coherent output. `/v1/models` +
    `/v1/chat/completions` (streaming, usage) verified — the exact surface the voice
    template's health watcher and LLM leg use, with the same explicit
    batch/length/memory-utilization flags the templates bake in.
  - Whisper `--custom-architectures` experiment (MMF-003): decided by wheel inspection, no
    server run needed — no speech `PipelineTask`, no `/v1/audio/*` route, in-tree module is
    graph-only with no arch/tokenizer/context. Structural serving-layer gap.

Mojo 1.0 porting notes (language changes hit during the spikes, for template authors): `fn`
removed (use `def`); stdlib now under `std.*`; `def` no longer implicitly raises (`def main()
raises:`); `DLHandle` → `OwnedDLHandle`; `UnsafePointer` → `Pointer`, heap buffers via
`List` (`unsafe_uninit_length=`), pointer `+` → `unsafe_offset()`; `List` is not implicitly
copyable (`return out^`); `len(String)` removed (`byte_length()` / `codepoints()` /
`graphemes()`), plain string slicing replaced by keyword forms (`s[byte=...]`); SIMD shift
RHS must match the operand type exactly (`x >> UInt32(k)`); `alias` deprecated → `comptime`
(warning), and `comptime` initializers cannot call raising functions; libc externs the stdlib
already declares (`open`, `write`) must not be re-declared with different signatures via
`external_call` (MMF-020) — use `openat`/`send` or match the stdlib's shapes, and keep one
argument shape per extern across your own calls too (MMF-020 addendum); `Int(ptr)` gives the
raw address, but ASAP destruction frees a `List` right after its last named use — reference
it *after* the FFI call that consumes its address, or keep it as a struct field.
