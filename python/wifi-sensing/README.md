# WiFi Sensing

Turn WiFi Channel State Information (CSI) into spatial sensing — **presence,
motion, breathing rate, and (experimental) heart rate** — with no cameras and no
wearables. Inspired by [ruvnet/ruview](https://github.com/ruvnet/ruview).

This is the **sensing-server half** of a ruview-style system: cheap ESP32 boards
capture CSI and stream it over the LAN; this app (running on a WendyOS device)
ingests the stream, runs a classical DSP pipeline on-device (CPU only), and
serves a live dashboard.

```
ESP32-S3/C6 sensors ──UDP CSI_DATA──▶  WendyOS app (ingest → DSP → dashboard)
```

## What it does

| Output | How | Notes |
| --- | --- | --- |
| Presence / occupancy | Amplitude variance vs. a calibrated empty-room baseline | Robust |
| Motion intensity | Sample-to-sample change energy ratio | 0–1, stays low for breathing |
| Breathing rate | Windowed FFT, 0.1–0.5 Hz band | Needs a still subject |
| Heart rate | Windowed FFT, 0.8–2.0 Hz band | **Experimental / best-effort** |
| CSI waterfall | Per-link subcarrier amplitude heatmap | Great for sensor placement |

## Sensors: CSI over UDP

Point your ESP32 CSI sensors at the device's `CSI_UDP_PORT` (default **5566**).
The default parser expects the Espressif `esp-csi` **`CSI_DATA`** CSV line, one
record per UDP datagram. The trailing `[...]` array is `int8` interleaved
imag/real pairs per subcarrier.

The firmware-specific column layout lives in one place — `app/lib/csi/parser.py`
— so adapting to your build is a single-file change. The ingest transport is
pluggable (`app/lib/csi/ingest.py`); MQTT/TCP can be added behind the same
interface.

## No hardware yet? Use the synthetic sender

`tools/csi_sender.py` emits `CSI_DATA` UDP frames with a breathing-modulated
amplitude, so you can run the whole app and dashboard with no ESP32:

```bash
python tools/csi_sender.py --host <device-ip> --port 5566 --bpm 15
```

Real sensors swap in by pointing them at the same port.

## Calibration

Open the **Sensors** page and click **Calibrate empty room** while the room is
empty and still. This captures the baseline CSI variance (persisted to `/data`)
used for presence and motion thresholds.

## Configuration (env vars)

| Var | Default | Meaning |
| --- | --- | --- |
| `CSI_UDP_PORT` | `5566` | UDP port sensors send to |
| `CSI_ANALYSIS_RATE_HZ` | `20` | Resampled analysis rate |
| `PRESENCE_WINDOW_S` | `4` | Presence/motion window |
| `VITALS_WINDOW_S` | `30` | Breathing/heart-rate window |
| `MOTION_THRESHOLD` | `1.5` | Occupancy variance multiplier |

## Develop & test

```bash
# backend tests
python -m pytest tests/ -v

# frontend
cd frontend && npm install && npm run build
```

## Deploy to a device

```bash
wendy run
```

`wendy.json` requests `network` (host mode, to receive UDP from the LAN) and a
`persist` volume at `/data` (calibration baseline). The dashboard opens
automatically via the `postStart` hook.

## Query it with an LLM (MCP)

`mcp/server.py` is an [MCP](https://modelcontextprotocol.io) server that exposes
the live sensing data as LLM-callable tools, so you can ask "is anyone home?",
"who's moving?", "what's the breathing rate?", "which sensors are online?".

Tools: `home_status`, `presence`, `vitals`, `sensors`, `raw_frame`. It reads a
sensing WebSocket (`SENSING_WS`, default `ws://localhost:3001/ws/sensing`) and
normalizes either this app's stream **or** a ruview stream into one shape.

```bash
pip install -r requirements-mcp.txt

# stdio (for Claude Desktop / Claude Code):
SENSING_WS=ws://<device>:3001/ws/sensing python mcp/server.py
# remote / in-container streamable-HTTP on :8000/mcp:
python mcp/server.py --http
```

Add to **Claude Code**:
```bash
claude mcp add wifi-sensing -e SENSING_WS=ws://<device>:3001/ws/sensing \
  -- python /path/to/wifi-sensing/mcp/server.py
```
Or **Claude Desktop** (`claude_desktop_config.json`):
```json
{ "mcpServers": { "wifi-sensing": {
    "command": "python",
    "args": ["/path/to/wifi-sensing/mcp/server.py"],
    "env": { "SENSING_WS": "ws://<device>:3001/ws/sensing" } } } }
```

The `network` entitlement in `wendy.json` already covers the MCP server reaching
the stream (and being reachable in `--http` mode).

## Limitations & seams

v1 uses CSI **amplitude** only and classical DSP — it is honest about what runs
well on an edge device. Documented future seams: phase-based analytics, MQTT/TCP
transports, multi-link fusion, and dropping in ruview's pretrained models in
place of the classical estimators (`app/lib/csi/dsp.py`).
