# AI Security Camera

Turn an NVIDIA Jetson into a self-hosted, AI-powered security camera recorder.
Plug an IP camera into the Jetson's Ethernet port (or put it on the same LAN),
and this app pulls the camera's RTSP stream, runs **YOLO11n** object detection +
**NvDCF** tracking on the GPU with **DeepStream 7.1**, and raises debounced
**security events** (with saved snapshots) whenever a person or vehicle appears.

Everything runs locally on the device — no cloud, no vendor app.

```
 IP camera ──RTSP──▶ Jetson (DeepStream YOLO + tracker) ──▶ web dashboard :{{.PORT}}
   (Ethernet)                                                 live preview + events
```

## What you get

- **Live web dashboard** at `http://<device>:{{.PORT}}` — MJPEG preview with bounding
  boxes and a rolling event log with thumbnails.
- **Security events** — debounced alerts for `person`, `car`, `truck`, `bus`,
  `motorcycle`, `bicycle` (configurable). Each event saves an annotated JPEG to
  the persistent volume.
- **Prometheus metrics** at `/metrics`, **events API** at `/events`, single-frame
  snapshot at `/snapshot`.
- **Multi-camera** — point it at several RTSP streams at once (batched inference).
- **Auto-discovery** — finds ONVIF/RTSP cameras on the network (WS-Discovery +
  port-554 sweep) and resolves their RTSP URLs, so you usually only supply the
  login.

## Hardware

- NVIDIA Jetson Orin Nano / AGX Orin running WendyOS (DeepStream 7.1, JetPack 6.x)
- An IP camera that exposes an RTSP stream (Reolink, Amcrest/Dahua, Hikvision, or
  any ONVIF camera)

## Wiring up the camera

This app reads **standard RTSP**, so it works with any ONVIF/RTSP camera. Two
things to get right before it can connect: the camera needs **an IP address the
Jetson can reach**, and its **RTSP server must be enabled**.

### Give the camera an IP

IP cameras expect a DHCP server to hand them an address. How you provide one
depends on how you cable it:

**Option A — Through a switch/router (easiest).** Put the camera and the Jetson
on the same LAN (any cheap unmanaged or PoE switch). The camera gets a normal
DHCP lease from your router; the Jetson reaches it. Done.

**Option B — Camera plugged directly into the Jetson (static IPs).** With a
direct cable there's no DHCP server, so neither end gets an address
automatically. Assign static IPs on both ends of the same subnet:

1. In the camera's settings, set a static IP, e.g. `192.168.50.10/24`.
2. On the Jetson, give the wired interface a matching static IP, e.g.:
   ```bash
   sudo ip addr add 192.168.50.1/24 dev eth0   # use the Jetson's wired iface name
   sudo ip link set eth0 up
   ```
3. Verify reachability: `ping 192.168.50.10`.

Then point `cameras.json` at `rtsp://<user>:<pass>@192.168.50.10:554/...`. The app
runs with `network: host`, so whatever the Jetson's wired interface can reach, the
container can read.

> A PoE camera still needs power. If you're going direct-cable with no PoE
> injector, use a camera with a separate DC (e.g. 12V) input, or put a PoE
> injector inline — the data still passes through to the Jetson.

### Enable RTSP on the camera (one-time)

Many cameras ship with RTSP **disabled** by default (some Reolink models, for
example, only expose a proprietary port until you turn RTSP on). Enable it once in
the camera's settings — typically *Network → Advanced → Server Settings → RTSP /
ONVIF* — then confirm the stream from any machine on the LAN:

```bash
ffprobe -rtsp_transport tcp rtsp://<user>:<pass>@<camera-ip>:554/stream1
```

If `ffprobe` reports a video stream, this app will read it.

Find the RTSP URL for your camera (examples in [`cameras.json`](./cameras.json)):

| Brand              | RTSP URL pattern |
|--------------------|------------------|
| Reolink (main)     | `rtsp://<user>:<pass>@<ip>:554/h264Preview_01_main` |
| Reolink (sub)      | `rtsp://<user>:<pass>@<ip>:554/h264Preview_01_sub` |
| Amcrest / Dahua    | `rtsp://<user>:<pass>@<ip>:554/cam/realmonitor?channel=1&subtype=0` |
| Hikvision          | `rtsp://<user>:<pass>@<ip>:554/Streaming/Channels/101` |
| Generic ONVIF      | `rtsp://<user>:<pass>@<ip>:554/stream1` |

> If your camera only speaks a proprietary protocol (e.g. some Reolink models ship
> with RTSP disabled), enable RTSP/ONVIF in the camera settings, or bridge it to
> RTSP with a tool like [`neolink`](https://github.com/QuantumEntangledAndy/neolink)
> and point this app at the bridge.

## Configure

### Auto-discovery (default)

Out of the box the app **discovers cameras on the network** at startup — you only
need to supply the login. It runs an ONVIF WS-Discovery probe and a port-554
subnet sweep, then auto-detects each camera's RTSP path. Just set the camera
credentials and deploy:

```bash
CAMERA_USER=admin CAMERA_PASS=yourpassword wendy run
```

Discovery finds cameras that already hold an IP (the switch/DHCP case above). A
camera on a bare direct cable needs a static IP first.

### Pinning specific cameras

To target exact cameras instead of discovering, list them in
[`cameras.json`](./cameras.json) with `enabled: true` (this takes priority over
discovery):

```json
{
  "discovery": { "enabled": true, "scan_port_554": true },
  "cameras": [
    { "name": "front-door", "url": "rtsp://admin:pass@192.168.1.108:554/h264Preview_01_main", "enabled": true }
  ]
}
```

### Runtime overrides

| Variable           | Default                                          | Description |
|--------------------|--------------------------------------------------|-------------|
| `CAMERA_URLS`      | _(unset)_                                        | Comma-separated RTSP URLs; highest priority, overrides everything |
| `CAMERA_USER`      | `admin`                                          | Login used to build discovered RTSP URLs |
| `CAMERA_PASS`      | _(empty)_                                        | Password used to build discovered RTSP URLs |
| `DISCOVERY`        | `auto`                                           | `auto` (discover only if nothing is configured), `on` (always discover + merge), `off` |
| `ALERT_CLASSES`    | `person,bicycle,car,motorcycle,bus,truck`        | Classes that raise events |
| `ALERT_CONFIDENCE` | `0.5`                                            | Minimum detection confidence to count |
| `EVENT_COOLDOWN`   | `15`                                             | Seconds between repeat events for the same camera+class |

Resolution order: `CAMERA_URLS` → enabled `cameras.json` entries → auto-discovery.

## Deploy to the Jetson

From this directory, with the Jetson connected (USB-C host mode or LAN):

```bash
# Build, ship, and stream logs to the device:
wendy run

# …or target a specific device:
wendy run --device wendyos-zestful-stork.local
```

`wendy run` builds the Dockerfile, ships the image to the device, and starts it.
When it's ready, the `postStart` hook opens the dashboard in your browser.

> **First run is slow.** DeepStream builds a TensorRT engine from the ONNX model
> the first time it sees your GPU — this takes several minutes. The engine is
> cached to the `/data` persistent volume, so every subsequent start is fast.
> The web dashboard comes up immediately and shows `pipeline: building` until the
> engine is ready.

## Entitlements

See [`wendy.json`](./wendy.json):

- **`gpu`** — DeepStream/TensorRT inference on the Jetson GPU
- **`network` (host)** — reach the camera's RTSP stream and serve the dashboard
- **`persist` `/data`** — cache the TensorRT engine and store event snapshots

## Endpoints

| Path             | Description |
|------------------|-------------|
| `/`              | Live dashboard (preview + events) |
| `/stream`        | MJPEG stream with bounding boxes |
| `/snapshot`      | Current annotated frame (single JPEG) |
| `/events`        | Recent security events (JSON) |
| `/events/<file>` | Saved event snapshot (JPEG) |
| `/health`        | Health + pipeline state |
| `/metrics`       | Prometheus metrics |

## How it works

`security_camera.py` builds a DeepStream pipeline:

```
uridecodebin (RTSP, TCP) ─▶ nvstreammux ─▶ nvinfer (YOLO11n) ─▶ nvtracker (NvDCF)
                                                ─▶ nvvideoconvert ─▶ nvdsosd ─▶ fakesink
```

A buffer probe on the OSD sink pad reads detection metadata, applies the alert
rules, draws boxes for the MJPEG preview, and records debounced events. The YOLO
custom parser library and ONNX model are built in the Docker builder stage from
[DeepStream-Yolo](https://github.com/marcoslucianops/DeepStream-Yolo); the
DeepStream and CUDA runtime libraries are mounted from the host via CDI.

This sample is a focused, security-oriented sibling of
[`deepstream-vision/detector`](../../deepstream-vision/detector) — see that app
for multi-stream tiling and VLM scene descriptions.
