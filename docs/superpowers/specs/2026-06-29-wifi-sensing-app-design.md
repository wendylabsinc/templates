# WiFi-Sensing App for WendyOS — Design

**Date:** 2026-06-29
**Status:** Approved (pending spec review)
**Inspiration:** [ruvnet/ruview](https://github.com/ruvnet/ruview) — WiFi CSI spatial-intelligence system

## Summary

A Wendy fullstack app (`python/wifi-sensing/`) that turns WiFi Channel State
Information (CSI) into spatial sensing — presence, motion, breathing rate, and
(experimental) heart rate — plus a live per-link CSI waterfall. It ingests a
real CSI stream from ESP32-S3/C6 sensor boards on the LAN, runs a classical DSP
pipeline on-device (CPU only), and serves a React dashboard. One container, runs
on any Jetson/Pi WendyOS device.

This is the **backend "sensing server" half** of a ruview-style system. ruview
captures CSI on cheap ESP32 boards and streams it to a server for fusion and
inference; a WendyOS device (Linux/Docker, onboard WiFi chip that does not export
CSI) is the natural home for that server, not the capture front-end.

### Scope decisions

- **CSI source:** real ESP32-S3/C6 sensors streaming over the network.
- **Transport:** UDP by default (matches Espressif `esp-csi` examples, lowest
  latency on a LAN). Ingest sits behind an interface so MQTT/TCP can be added
  later without touching processing code.
- **Payload:** Espressif `CSI_DATA` CSV line, one record per UDP datagram.
- **Analytics (v1):** presence + motion, breathing rate, heart rate
  (experimental), per-link CSI waterfall — all via classical DSP
  (numpy/scipy), with a clean seam to drop in ruview's pretrained models later.
- **Approach:** extend the existing `python/fullstack` template (FastAPI +
  React/Vite + shadcn). Not a Rust port of ruview (rejected: much larger lift,
  no Wendy Rust fullstack scaffold).

### Non-goals (v1)

- No ESP32 firmware work — sensors are assumed to emit `CSI_DATA` over UDP.
- No neural-network inference (ruview's pose/DensePose/vitals models) — left as
  a documented seam in `dsp.py`.
- No phase-based analytics — v1 uses amplitude only; phase sanitization is a
  later seam.
- No MQTT/TCP transport in v1 — only the pluggable interface that allows it.

## Architecture

```
ESP32-S3/C6 sensors ──UDP CSI_DATA──▶  ┌─────────────────────────────────────┐
  (on the LAN)                          │  WendyOS Docker app                  │
                                        │                                      │
                                        │  ingest → buffer → DSP → pipeline    │
                                        │                              │       │
                                        │  FastAPI  ◀──────────────────┘       │
                                        │    ├── REST (status, sensors, calib) │
                                        │    └── WS /ws/stream (live analytics)│
                                        │  React/Vite dashboard (served static)│
                                        └─────────────────────────────────────┘
                                              ▲ browser opens via postStart hook
```

Single container, host networking, CPU-only. Built from the `python/fullstack`
template with the camera/audio/gpu machinery removed.

## Backend components (`app/lib/csi/`)

Each module has one responsibility and a clean interface so it is testable in
isolation.

### `ingest.py`
- Abstract `CSISource`: async iterator yielding raw payloads (bytes).
- `UDPCSISource`: asyncio `DatagramProtocol` bound to `CSI_UDP_PORT`.
- This interface is the seam where MQTT/TCP plug in later. The synthetic test
  sender (`tools/csi_sender.py`) targets this same wire format.

### `parser.py`
- Parses an Espressif `CSI_DATA` CSV payload into a `CSIFrame`:
  `link_id` (from sensor MAC), `timestamp`, `rssi`, `channel`, complex
  subcarrier array.
- The CSI array is `len` `int8` values, interleaved imag/real pairs per
  subcarrier. Per subcarrier: `amplitude = sqrt(re^2 + im^2)`,
  `phase = atan2(im, re)`. v1 uses amplitude.
- Defensive: malformed/short lines are counted (`malformed` counter) and
  dropped, never crash ingest. Column order is centralized in one place so
  adapting to a specific firmware build is a single-file change.

### `buffer.py`
- Per-link numpy ring buffers of recent timestamped frames.
- Resampling helpers to a fixed analysis rate.
- Per-link isolation: distinct sensor MACs never cross-contaminate.

### `dsp.py`
- Pure functions:
  - **Subcarrier selection** — drop null/pilot subcarriers, rank by SNR/variance.
  - **Presence/motion** — amplitude variance of selected subcarriers over a
    short window vs. a calibrated empty-room baseline → `occupied: bool`,
    `motion: float` (0–1).
  - **Vitals** — detrend → bandpass → FFT peak on the most stable subcarrier
    (or PCA first component): breathing 0.1–0.5 Hz (6–30 BPM), heart 0.8–2.0 Hz
    (~48–120 BPM), each with a confidence from peak prominence. Heart rate is
    tagged `experimental`.
  - **Waterfall** — downsampled amplitude matrix for display.

### `pipeline.py`
- Orchestrates: drains ingest → routes each `CSIFrame` to its link buffer →
  every ~1 s pulls each link's window, resamples, runs `dsp` → emits one
  `AnalyticsFrame` to all subscribers.
- Holds the calibration baseline and current config.
- When motion is high, vitals report low confidence / `null` rather than
  garbage.

## Routes (`app/routes/sensing.py`)

- `GET /api/status` — pipeline/ingest health, packet rates, uptime.
- `GET /api/sensors` — per-link list: MAC, RSSI, packets/sec, last-seen,
  channel, malformed-frame count.
- `GET /api/config`, `PUT /api/config` — read/tune DSP parameters.
- `POST /api/calibrate` — capture empty-room baseline (persisted to `/data`).
- `WS /ws/stream` — pushes `AnalyticsFrame`: presence, motion, breathing, heart
  rate (+ confidences), and a per-link waterfall slice.

## CSI_DATA wire format (default parser target)

Default assumption: each ESP32 sends one `CSI_DATA` CSV line per UDP datagram:

```
CSI_DATA,<type>,<mac>,<rssi>,<rate>,<sig_mode>,<mcs>,<bw>,...,<channel>,...,<timestamp>,<ant>,<sig_len>,<rx_state>,<len>,[<int8 csi array>]
```

- `link_id` = sensor MAC (multiple sensors = multiple independent links).
- The exact column layout is centralized in `parser.py` for easy adaptation to a
  specific firmware build.

## Data flow (steady state)

1. ESP32 → UDP datagram → `UDPCSISource` yields raw bytes.
2. `parser` → `CSIFrame`, or drop + increment `malformed`.
3. `pipeline` routes the frame to that link's ring `buffer` (timestamped).
4. Every ~1 s, `pipeline` pulls each link's window, resamples, runs `dsp`:
   - Presence/motion over a short window (~4 s) vs. baseline.
   - Breathing/heart over a longer sliding window (~30 s).
   - Waterfall downsample.
5. `pipeline` emits one `AnalyticsFrame`; all WS clients receive it.

### Windowing & rate

- `CSI_ANALYSIS_RATE_HZ` (default ~20 Hz resampled), `PRESENCE_WINDOW_S`
  (~4 s), `VITALS_WINDOW_S` (~30 s), `MOTION_THRESHOLD` — all env-configurable
  so tuning to sensor placement needs no code change.
- Vitals require a roughly stationary subject; high motion → low confidence /
  `null`.

## Frontend (React/Vite + shadcn)

Reuses the template's sidebar, cards, charts, and badges. Camera/gpu/audio pages
removed and replaced with three views.

### Pages

1. **Live (default)** — presence card (occupied/empty + time-in-state), motion
   gauge (0–1 + sparkline), breathing card (BPM + confidence), heart-rate card
   (BPM + confidence, **"Experimental"** badge + caveat tooltip). Cards gray out
   with a reason when confidence is too low or no data is arriving.
2. **Sensors** — one row per CSI link: RSSI, packets/sec, last-seen, channel,
   malformed-frame count. Hosts the **Calibrate** button (confirm dialog →
   `POST /api/calibrate`). Placement/health debugging view.
3. **CSI Waterfall** — live canvas heatmap of subcarrier amplitude over time per
   selected link, with a link selector.

### Data layer

- `use-sensing-stream.ts` wraps `WS /ws/stream` (auto-reconnect, mirrors the
  template's `use-backend-health` pattern) and fans `AnalyticsFrame` out to
  pages.
- Connection status (connected / reconnecting / no sensors) in the site header.

### Serving

FastAPI serves the built static frontend on a single port (same as template);
the `postStart` hook opens one browser tab.

## Entitlements, config & Docker

### `wendy.json`

- `network` → `mode: "host"` — required to receive UDP CSI datagrams from LAN
  sensors.
- `persist` → `/data` — calibration baseline + tunable config (survives
  restarts).
- `readiness.tcpSocket` on `{{.PORT}}`, and the `postStart` open-browser hook.
- Camera/audio/gpu entitlements dropped (CPU-only DSP).

### Config (env, with defaults)

- `CSI_UDP_PORT` (default 5566) — where ESP32 sensors send.
- `CSI_TRANSPORT` (default `udp`) — pluggable-ingest selector.
- `CSI_ANALYSIS_RATE_HZ`, `PRESENCE_WINDOW_S`, `VITALS_WINDOW_S`,
  `MOTION_THRESHOLD` — DSP tuning.
- New `CSI_UDP_PORT` template variable in `template.json` alongside
  `APP_ID`/`PORT`.

### Dockerfile

Two-stage like the template, but a much leaner backend stage (no GStreamer):

- Stage 1: `node:22-slim` builds the Vite frontend.
- Stage 2: `debian:bookworm-slim` (or `python:3.x-slim`) with `numpy`, `scipy`,
  `fastapi`, `uvicorn`. Entry stays `uvicorn app:api`; frontend copied to
  `./static`; same `{{.PORT}}` expose; `WENDY_DEBUG`/`debugpy` path kept.

`requirements.txt`:

```
fastapi==0.135.3
uvicorn[standard]
numpy
scipy
```

### Files added / changed vs. template

- **New:** `app/lib/csi/{ingest,parser,buffer,dsp,pipeline}.py`,
  `app/routes/sensing.py`, frontend sensing pages + `use-sensing-stream.ts`,
  `tools/csi_sender.py` (synthetic UDP test/replay sender).
- **Rewritten:** `app/__init__.py` (wire `sensing` router + pipeline startup,
  drop GStreamer), `wendy.json`, `Dockerfile`, `requirements.txt`,
  `template.json`, frontend `App.tsx`/sidebar/pages.
- **Removed:** camera/audio/gpu routes + `lib/gst_sink.py` and their frontend
  pages.

## Testing strategy

### Unit tests (`pytest`, in `tests/`)

- **parser** — valid / truncated / garbage / wrong-column-count lines → correct
  `CSIFrame` or clean drop + counter increment; verify imag/real de-interleave
  and amplitude math against hand-computed values.
- **dsp** — synthetic signals with known ground truth:
  - Flat/low-variance → `occupied=False`, motion ≈ 0.
  - High-variance → `occupied=True`, motion high.
  - 0.25 Hz sine → breathing ≈ 15 BPM (±tolerance), high confidence.
  - 1.2 Hz sine → heart ≈ 72 BPM.
  - Noise-only → low confidence / `null`, no false reading.
- **buffer** — ring eviction, resampling to fixed rate, per-link isolation.

### Integration test

- Start the app, run `tools/csi_sender.py` to emit synthetic `CSI_DATA` UDP
  frames (presence + 15 BPM breathing), connect a WS client, assert the emitted
  `AnalyticsFrame` reports occupied + breathing ≈ 15 BPM within N seconds.

### Manual / on-device

- `tools/csi_sender.py` exercises the full app + dashboard with no ESP32 needed,
  then real sensors swap in by pointing them at `CSI_UDP_PORT`.
- `wendy run` on the target device; confirm dashboard opens; point a real ESP32
  at it; sanity-check waterfall + presence.

## Future seams (documented, not built)

- MQTT/TCP transports behind `CSISource`.
- Phase-based analytics (unwrapping/sanitization) in `dsp.py`.
- ruview pretrained model inference replacing/augmenting classical DSP.
- Multi-link fusion (ruview's multi-band/multi-sensor fusion).
