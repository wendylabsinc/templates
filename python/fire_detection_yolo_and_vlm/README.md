# {{.APP_ID}}

Fire/water/gauge detection demo: YOLO models + Qwen3-VL VLM. Two services, deployed separately.

## Deploy

The VLM service must be running before the detector starts (the detector calls it on `127.0.0.1:8090`).

```sh
# 1. VLM first — first build downloads ~5 GB Qwen3-VL weights, takes a while
cd vlm
sudo wendy run --device <jetson>

# 2. Detector — once VLM is up
cd ..
sudo wendy run --device <jetson>
```

Both must land on the same Jetson with `network: host` so the detector can reach the VLM at `127.0.0.1:8090`.

## Why two `wendy run` calls

Both services need the `gpu` entitlement, and the detector also needs `camera`/`audio`/`bluetooth`. The Wendy compose path (`compose.yml`) only synthesizes `network` and `persist` entitlements today — it doesn't pass through GPU or device permissions. So we deploy each service from its own directory with its own `wendy.json`.
