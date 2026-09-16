# {{.APP_ID}}

Vulkan GPU diagnostics for WendyOS devices. It runs two compute shaders on the
device GPU and **verifies every result against a CPU reference**, then serves a
report over HTTP.

Unlike a plain benchmark, this reports correctness alongside throughput. A GPU
can dispatch shaders successfully and still compute wrong numbers; a benchmark
that only prints GFLOPS would report those confidently.

## Requirements

- A reachable WendyOS device and Wendy CLI access
- A GPU exposing a DRM render node (`/dev/dri/renderD*`)
- Network access during the first build to install the Vulkan SDK and Mesa
- A userspace Vulkan driver in the image new enough for the target GPU (see
  **Driver versions** below)

The project declares GPU and host-network entitlements. GPU access requires
exactly one line of configuration:

```json
"entitlements": [ { "type": "gpu" } ]
```

The Wendy CLI injects the device's render node into the container. No driver
installation on the host and no device mapping are required.

## Run and verify

```sh
wendy run
```

Open `http://<device-hostname>:{{.PORT}}`. The page reports the GPU identity and
a PASS/FAIL result for each kernel. The raw JSON is at `/report.json`:

```json
{
  "device": "Adreno623",
  "vendor": "Qualcomm",
  "type": "INTEGRATED_GPU",
  "vecadd": { "elements": 1048576, "gb_per_s": 2.52, "mismatches": 0, "pass": true },
  "matmul": { "size": 256, "gflops": 4.95, "mismatches": 0, "pass": true },
  "pass": true
}
```

`mismatches` is the number that matters. `vecadd` checks all 1,048,576 elements;
`matmul` recomputes the full 256x256 product in double precision and compares
within tolerance. Non-zero mismatches mean the GPU is producing incorrect
results, which is a real condition on some hardware and driver combinations.

## Configuration

| Variable | Default | Purpose |
|---|---:|---|
| `APP_ID` | required | Application identifier |
| `PORT` | `9021` | HTTP listener and readiness probe |

## How it works

- `vecadd.comp` and `matmul.comp` are GLSL compute shaders, compiled to SPIR-V
  at build time with `glslc`.
- `main.cpp` creates a Vulkan instance, selects the first physical device, runs
  each kernel, and compares the output against a CPU reference.
- `entrypoint.sh` pins the Vulkan loader to the GPU vendor's ICD. The Mesa
  package ships ICDs for every vendor; enumerating ones with no matching
  hardware is noise at best.
- The report is written once at startup and served as a static file.

## Driver versions

The container brings its own userspace Vulkan driver -- the `gpu` entitlement
passes the kernel render node, not the driver. The driver must be new enough for
the target GPU, and a mismatch fails in ways that are hard to read:

| Mesa | Adreno A623 (SA8775P / QCS8300) |
|---|---|
| 22.3 (Debian bookworm) | segfault in `vkCreateInstance` |
| 25.0.7 (Debian trixie) | `VK_ERROR_INCOMPATIBLE_DRIVER` |
| 26.2.2 (Debian sid) | works |

This template uses `debian:sid-slim` for that reason. On a target with an older
GPU, an older base will do.

## Extend it

- Add kernels by writing a `.comp` file, compiling it in the Dockerfile, and
  calling `makeKernel` / `dispatch` in `main.cpp`. Keep the CPU verification.
- Change problem sizes with the `N` and `M` constants in `main.cpp`.

## Operations and troubleshooting

```sh
wendy device logs {{.APP_ID}} --tail 50
wendy device apps stop {{.APP_ID}}
```

If the report contains `"error": "no Vulkan device found"`, check that the `gpu`
entitlement is declared and that the device exposes `/dev/dri/renderD*`
(`wendy device hardware list`). The entrypoint logs the render node it received.

If the process exits without producing a report, the driver is likely too old
for the GPU -- see **Driver versions**.
