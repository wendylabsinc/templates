# Camera Project — Multi-Camera Streaming on WendyOS

A fleet of USB webcams on WendyOS Jetson devices, each streaming MJPEG over HTTP,
viewable locally or remotely (via Wendy Cloud tunnels), aggregated in a browser
"camera wall" dashboard, with synchronized recording to disk.

Built end-to-end in this project: a deployable camera template, the devices, the
cloud access path, a dashboard, and a recorder.

---

## 1. Architecture

```
USB webcam ──► usb-camera app (OpenCV → JPEG → MJPEG HTTP :8000)   [on each Jetson]
                     │
        ┌────────────┴───────────────┐
        │ LAN (low latency)          │ Wendy Cloud tunnel (remote, higher latency)
        ▼                            ▼
  http://<device>:8000        wendy cloud tunnel <localport>:8000 --device <name>
        │                            │  → http://localhost:<localport>
        └──────────────┬─────────────┘
                       ▼
        Camera Wall dashboard (browser)   +   record-cameras.sh (ffmpeg → mp4)
```

- **Per device:** the `usb-camera` app opens `/dev/video0` and serves a live
  MJPEG stream on port `8000`.
- **Access:** directly over the LAN (`http://<device-ip>:8000`) **or** through a
  Wendy Cloud tunnel that forwards a local port to the device's `:8000`.
- **Dashboard:** a static HTML page that embeds each camera's MJPEG stream.
- **Recorder:** an ffmpeg script that records every stream to time-synced files.

---

## 2. The `usb-camera` template

A single-container WendyOS app. **Source:** `templates/python/usb-camera/`
(branch `cn/usb-camera-template`).

**What it does:** opens a V4L2 webcam with OpenCV and re-serves frames as
multipart MJPEG. No special drivers and no cloud dependency — plug in any
UVC webcam and stream it.

**Endpoints:**
| path | purpose |
|------|---------|
| `GET /` | live viewer page (MJPEG in an `<img>` + status) |
| `GET /stream` | `multipart/x-mixed-replace` MJPEG |
| `GET /stream/color` | alias of `/stream` (handy for consumers that expect that path) |
| `GET /health` | `{status, frames, fps, device, resolution, error}` |

**Entitlements:** `network: host` + `camera` (grants `/dev/video*`).

**Template variables:**
| var | default | meaning |
|-----|---------|---------|
| `APP_ID` | (required) | app id, e.g. `sh.wendy.usbcam` |
| `PORT` | `8000` | HTTP port |
| `VIDEO_DEVICE` | `/dev/video0` | V4L2 device (or index `0`) |
| `WIDTH` / `HEIGHT` | `1280` / `720` | capture resolution |
| `FPS` | `30` | target frame rate |
| `JPEG_QUALITY` | `80` | MJPEG quality (1–100) |

**Latency tuning (added to the deployed copy):**
- `cv2.CAP_PROP_BUFFERSIZE = 1` — keep only the newest frame (drops ~100–200ms
  of stale-frame lag).
- A **low-latency profile** in use on the fleet: **640×480 / 20fps / q55**
  (~6–8× less data than 720p30/q80).

> These live in the rendered copy at `/private/tmp/wendy-test/sh.wendy.usbcam/`.
> They are **not yet folded into the template branch** — TODO.

---

## 3. Device inventory

WendyOS Jetson **Orin Nano (NVMe)** devkits, arm64, JetPack 7.2. All enrolled in
**Wendy Cloud (Org 2)**.

| Name | Cloud ID | mDNS host | Tunnel port | App | Notes |
|------|----------|-----------|-------------|-----|-------|
| camera-01 | 194 | wendyos-camera-01.local | 8088 | sh.wendy.usbcam | ✅ streaming |
| camera-02 | 195 | wendyos-camera-02.local | 8089 | sh.wendy.usbcam | ✅ streaming |
| camera-03 | 196 | wendyos-camera-03.local | 8090 | sh.wendy.usbcam | ✅ streaming (had a HW issue, fixed) |
| camera-04 | 197 | wendyos-camera-04.local | 8091 | sh.wendy.usbcam | ✅ streaming (needed a webcam attached) |
| camera-05 | 198 | wendyos-camera-05.local | 8092 | sh.wendy.usbcam | ✅ streaming (first low-latency target) |

All currently run the **640×480 / 20fps** low-latency build.

---

## 4. Deploying a camera

From the rendered project dir (or `wendy init --template usb-camera --branch cn/usb-camera-template`):

**Over LAN** (preferred — low latency, fast push):
```sh
cd /private/tmp/wendy-test/sh.wendy.usbcam
wendy run --device wendyos-camera-0X.local -y --detach
```

**Over the cloud** (when the device isn't on your LAN):
```sh
wendy cloud run --device camera-0X -y --detach
```

Then open `http://<device>:8000` (LAN) or set up a tunnel (below).

> The current `wendy` (≥ 2026.06.25) builds `linux/arm64` for WendyOS devices
> correctly. (Earlier we used a patched CLI at `/private/tmp/wendy-fixed` to work
> around a `wendyos/arm64` / `ubuntu/arm64` invalid-platform bug — no longer needed.)

---

## 5. Viewing remotely — Wendy Cloud tunnels

Each camera gets a local port forwarded to its `:8000` through the cloud broker:

```sh
wendy cloud tunnel 8088:8000 --device camera-01
wendy cloud tunnel 8089:8000 --device camera-02
wendy cloud tunnel 8090:8000 --device camera-03
wendy cloud tunnel 8091:8000 --device camera-04
wendy cloud tunnel 8092:8000 --device camera-05
```

Then the stream is at `http://localhost:<port>/`. Keep each `tunnel` process
running while viewing. **After a device reboots, restart its tunnel** (the
session goes stale): `pkill -f "tunnel 8092:8000"; wendy cloud tunnel 8092:8000 --device camera-05 &`

Useful cloud commands:
```sh
wendy cloud discover                      # list enrolled devices
wendy cloud device info  --device camera-01
wendy cloud device logs  --device camera-01   # stream container/agent logs
wendy cloud device sync-time                  # fix device clocks (LAN multicast)
```

---

## 6. The dashboard ("Camera Wall")

`~/camera-dashboard/index.html` — a self-contained page. Serve it:
```sh
cd ~/camera-dashboard && python3 -m http.server 9000
# open http://localhost:9000/
```

- **Left rail:** live thumbnails (sized to fit ~6 without scrolling), each with a
  green/red status dot. Click (or ↑/↓) to select.
- **Center:** big view of the selected camera + Fullscreen.
- **+ Add camera:** name + base URL (a tunnel `http://localhost:8092` or a device
  `http://192.168.0.40:8000`); persists in browser localStorage. New defaults are
  auto-merged on reload; **Reset** restores defaults.
- Status is driven by the MJPEG `<img>` load/error (no `fetch`), so it works
  cross-origin without the cameras needing CORS headers; offline cameras auto-retry.

---

## 7. Recording (synchronized, on this Mac)

`~/camera-dashboard/record-cameras.sh` — pulls each camera's MJPEG via its tunnel
and writes one H.264 `.mp4` per camera (VideoToolbox HW encode), all started
together on a shared clock.

```sh
~/camera-dashboard/record-cameras.sh           # record until Ctrl-C
DURATION=120 ~/camera-dashboard/record-cameras.sh   # fixed length (seconds)
```

Output: `~/camera-dashboard/recordings/<session-timestamp>/<name>.mp4` + a
`session.txt` with the shared start time (also embedded as each file's
`creation_time`). Edit the `CAMERAS` list at the top to add/remove feeds.

> A browser page can't write video files to disk — that's why recording is this
> terminal script, not a dashboard button. (A one-click Record would need a small
> local backend.)

---

## 8. Latency — findings

Measured per-request round-trips **through the cloud tunnel are ~0.5–1.8s**, and
the **same for a 640×480 camera as a 720p one** → the latency is the **cloud
relay**, not the video size.

- **Root cause:** the Wendy Cloud broker is in **`us-central1` (USA)**; the
  cameras are in **Germany (TU Munich)**. Every frame goes Germany → USA → Germany.
- **Biggest fixes (remove the transatlantic hop):**
  1. **View over the LAN** (`http://<camera-ip>:8000`) instead of the cloud tunnel.
  2. **An EU cloud-relay region** (Wendy Cloud infra change) — closer broker.
- **Camera-side tuning** (low-latency profile + `BUFFERSIZE=1`) only reduces
  *bandwidth queuing*, not the relay floor. Worth doing, but secondary.
- **Bigger camera-side option:** MJPEG → **WebRTC/H.264** (congestion-controlled,
  ~10× less bandwidth, sub-200ms) — but still bounded by the relay location.

---

## 9. Gotchas / troubleshooting

- **`No changes detected` on redeploy:** `wendy cloud run` skips if it thinks the
  image is already deployed (a per-project cache — only the *first* device in a
  loop updates). **Fix:** bump `version` in `wendy.json` **uniquely per device**
  (e.g. `0.1.2`, `0.1.3`, …) to force each.
- **`readiness probe timed out after 30s`:** usually harmless — the app binds
  `:8000` a bit after 30s. Verify with `/health` after a moment.
- **App crash-loops / `:8000` connection refused, `Task exited exit_code 0`:**
  **no usable webcam** on that device (no `/dev/video*`). Plug a UVC camera in.
- **`TLS handshake rejected by device (clock skew or cert mismatch)`:** the
  device's clock drifted into the past (no RTC). `wendy cloud device sync-time`
  fixes it **but only over the LAN (Roughtime multicast)** — your Mac must be on
  the same network segment as the device. mTLS/cloud ops fail until the clock is right.
- **Tunnel shows no frames after a device reboot:** the tunnel session is stale —
  restart the `wendy cloud tunnel` for that camera.
- **`.local` names don't resolve in Chrome/Android:** mDNS is best-effort there.
  Use the device IP, or scan a QR that encodes the IP.

---

## 10. Quick reference

```sh
# serve dashboard
cd ~/camera-dashboard && python3 -m http.server 9000   # http://localhost:9000/

# bring up all tunnels
for i in 1 2 3 4 5; do p=$((8087+i)); wendy cloud tunnel $p:8000 --device camera-0$i & done

# health of a feed
curl -s http://localhost:8092/health

# record everything (synced)
~/camera-dashboard/record-cameras.sh

# deploy/redeploy a camera (bump version first to force an update)
cd /private/tmp/wendy-test/sh.wendy.usbcam
wendy cloud run --device camera-0X -y --detach
```

## Files

| path | what |
|------|------|
| `~/camera-dashboard/index.html` | the Camera Wall dashboard |
| `~/camera-dashboard/record-cameras.sh` | synchronized recorder |
| `~/camera-dashboard/recordings/` | recorded sessions |
| `templates/python/usb-camera/` | the template (branch `cn/usb-camera-template`) |
| `/private/tmp/wendy-test/sh.wendy.usbcam/` | rendered deploy copy (low-latency profile) |
