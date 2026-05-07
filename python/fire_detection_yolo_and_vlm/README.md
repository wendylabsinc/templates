# {{.APP_ID}}

Fire/water/gauge detection demo: YOLO models + Qwen3-VL VLM. Two services in one compose project.

## Deploy

```sh
sudo wendy run --device <jetson>
```

That's it. Builds and deploys both services in one shot. The VLM image first build pulls the Qwen3-VL-2B weights from Hugging Face (~5 GB) — slow once, cached after.

## What's in compose.yml

| Service | Port | Image base | Entitlements |
|---|---|---|---|
| `vlm` | 8090 | `ubuntu:22.04` + Jetson AI Lab pytorch | `gpu`, host network |
| `detector` | 5702 | `ultralytics/ultralytics:latest-jetson-jetpack6` | `gpu`, `camera`, `audio`, `bluetooth`, host network, persist mounts |

`gpu` / `camera` / `audio` / `bluetooth` come from the `x-wendy-entitlements:` block on each service — a Wendy compose extension. The `x-` prefix is the Compose-spec namespace for custom fields, so `docker compose up` (locally on macOS, etc.) silently ignores them.

The detector talks to the VLM at `127.0.0.1:8090` over host networking. Both must land on the same Jetson.

## Apps that appear on the device

After `wendy run`, `wendy device apps list` shows:

```
{{.APP_ID}}-vlm
{{.APP_ID}}-detector
```

(The `<projectDirName>-<serviceName>` naming is what `compose.go:269` synthesizes per service.)
