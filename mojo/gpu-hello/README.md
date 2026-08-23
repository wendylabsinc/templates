# gpu-hello (Mojo)

Pure-Mojo GPU diagnostics for Jetson-class devices: runs a verified vector-add and a naive
512³ matmul on the device GPU via Modular's `DeviceContext`/`TileTensor` APIs, then serves
the results as plain text on `{{.PORT}}` (`/` = full report, `/health` = liveness).

The first template in the Mojo + MAX port series — its job is to prove, on a freshly
provisioned device, that the Modular stack can light up the GPU at all, and to put a number
(GFLOPs, launch latency) on it. Companion findings doc: `docs/mojo-max-port-findings.md`.

## How it works

- The Dockerfile AOT cross-compiles `main.mojo` for the target GPU architecture using the
  build args the wendy CLI injects: Jetson Thor → `sm_110`, other Jetson (Orin family) →
  `sm_87`, anything else → CPU-only build that reports "gpu: not available".
- The final image is `debian:bookworm-slim` + the binary + the ~4 MB of Mojo runtime `.so`
  files it links (~110 MB total, no Python, no SDK). The CUDA driver is injected by the
  host at runtime via the `gpu` entitlement.
- The HTTP layer is hand-rolled on libc sockets — Mojo 1.0 has no stdlib networking
  (findings doc, MMF-011).

## Run

```sh
wendy init --template gpu-hello --language mojo my-gpu-hello
cd my-gpu-hello
wendy run
```

Verified 2026-08-23 on a Jetson Orin Nano (JetPack 7.2, MAX 26.5.0 / Mojo 1.0.0).
