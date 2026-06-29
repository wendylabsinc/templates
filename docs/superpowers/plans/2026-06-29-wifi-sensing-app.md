# WiFi-Sensing App Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A WendyOS Docker app that ingests WiFi CSI from ESP32 sensors over UDP, derives presence/motion/breathing/(experimental) heart rate via classical DSP, and serves a live React dashboard with a CSI waterfall.

**Architecture:** Extend the `python/fullstack` template (FastAPI + React/Vite + shadcn), stripped of GStreamer/camera/audio/gpu. Backend `app/lib/csi/` modules (ingest → buffer → dsp → pipeline) feed a FastAPI router (REST + WebSocket). Frontend shows Live, Sensors, and Waterfall pages over a WS stream.

**Tech Stack:** Python 3.12 (FastAPI 0.135.3, uvicorn, numpy, scipy), asyncio UDP, React 19 + Vite 8 + shadcn/ui, pytest.

## Global Constraints

- App lives at `python/wifi-sensing/`.
- CPU-only — no GPU/GStreamer/camera/audio dependencies.
- `wendy.json`: `network` host mode (UDP from LAN), `persist` `/data`, readiness tcpSocket on `{{.PORT}}`, postStart open-browser.
- FastAPI app object is named `api` in `app/__init__.py`; entry `uvicorn app:api`.
- Frontend built to `./static`, served by FastAPI SPA fallback.
- Config via env: `CSI_UDP_PORT` (default 5566), `CSI_ANALYSIS_RATE_HZ` (default 20), `PRESENCE_WINDOW_S` (default 4), `VITALS_WINDOW_S` (default 30), `MOTION_THRESHOLD` (default 1.5).
- CSI amplitude only in v1 (phase is a future seam).
- TDD: failing test → minimal impl → pass → commit.

---

## File Structure

```
python/wifi-sensing/
  template.json            # adds CSI_UDP_PORT variable
  wendy.json               # network host + persist /data
  Dockerfile               # 2-stage, no GStreamer, + numpy/scipy
  requirements.txt         # fastapi, uvicorn, numpy, scipy
  pytest.ini               # test config
  app/
    __init__.py            # FastAPI `api`, mount sensing router, start pipeline
    config.py              # env-backed Config dataclass
    routes/
      __init__.py
      sensing.py           # REST + WS /ws/stream
      system.py            # (kept from template, unchanged)
    lib/
      __init__.py
      csi/
        __init__.py
        types.py           # CSIFrame, AnalyticsFrame, SensorStat
        parser.py          # parse_csi_data()
        buffer.py          # LinkBuffer, BufferStore
        dsp.py             # presence/motion/vitals/waterfall pure fns
        ingest.py          # CSISource, UDPCSISource
        pipeline.py        # Pipeline orchestration
  tools/
    csi_sender.py          # synthetic UDP CSI_DATA generator
  tests/
    test_parser.py
    test_buffer.py
    test_dsp.py
    test_pipeline.py       # integration: sender → pipeline → AnalyticsFrame
  frontend/                # from template, pages replaced
    src/
      App.tsx              # routes: /live /sensors /waterfall
      hooks/use-sensing-stream.ts
      components/app-sidebar.tsx   # nav items replaced
      components/site-header.tsx   # title + connection status
      pages/live.tsx
      pages/sensors.tsx
      pages/waterfall.tsx
```

---

## Task 1: Scaffold app from fullstack template

**Files:**
- Create dir `python/wifi-sensing/` by copying `python/fullstack/`
- Remove: `app/routes/{camera,audio,gpu,data}.py`, `app/lib/{gst_sink,db,devices}.py`, `frontend/src/pages/{camera,audio,gpu,persistence}.tsx`
- Modify: `requirements.txt`, `Dockerfile`, `wendy.json`, `template.json`

**Interfaces:**
- Produces: a buildable Python package skeleton; `requirements.txt` with numpy+scipy.

- [ ] **Step 1:** `cp -r python/fullstack python/wifi-sensing` then delete the camera/audio/gpu/data routes, `gst_sink.py`, `db.py`, `devices.py`, and their frontend pages.

- [ ] **Step 2:** Rewrite `requirements.txt`:
```
fastapi==0.135.3
uvicorn[standard]
numpy==2.2.1
scipy==1.15.1
```

- [ ] **Step 3:** Rewrite `Dockerfile` stage 2 to drop GStreamer (keep node frontend stage 1):
```dockerfile
# syntax=docker/dockerfile:1.6
# Stage 1 — Build React frontend
FROM node:22-slim AS frontend
WORKDIR /frontend
COPY frontend/package*.json ./
RUN --mount=type=cache,target=/root/.npm npm install
COPY frontend/ ./
RUN npm run build

# Stage 2 — FastAPI backend (CPU-only DSP)
FROM python:3.12-slim-bookworm
WORKDIR /app
ENV PYTHONUNBUFFERED=1

RUN python3 -m venv /app/venv
ENV PATH="/app/venv/bin:$PATH"

COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements.txt

COPY app/ ./app/
COPY --from=frontend /frontend/dist ./static

ARG WENDY_DEVICE_TYPE
ARG WENDY_DEBUG=false
ENV WENDY_DEVICE_TYPE=${WENDY_DEVICE_TYPE}
ENV WENDY_DEBUG=${WENDY_DEBUG}
RUN --mount=type=cache,target=/root/.cache/pip \
    if [ "$WENDY_DEBUG" = "true" ]; then pip install debugpy; fi

EXPOSE {{.PORT}}
CMD ["/app/venv/bin/uvicorn", "app:api", "--host", "0.0.0.0", "--port", "{{.PORT}}"]
```

- [ ] **Step 4:** Rewrite `wendy.json` — network host, persist /data, readiness, postStart (see spec). Rewrite `template.json` to add `CSI_UDP_PORT` (integer, default 5566) and update name/description.

- [ ] **Step 5:** Commit `feat: scaffold wifi-sensing app from fullstack template`.

---

## Task 2: CSI types + parser

**Files:**
- Create: `app/lib/csi/types.py`, `app/lib/csi/parser.py`, `tests/test_parser.py`, `pytest.ini`

**Interfaces:**
- Produces:
  - `CSIFrame` dataclass: `link_id: str`, `timestamp: float`, `rssi: int`, `channel: int`, `amplitudes: np.ndarray` (float, one per subcarrier).
  - `parse_csi_data(payload: bytes | str) -> CSIFrame | None` — returns None on malformed input.

- [ ] **Step 1: Write failing tests** (`tests/test_parser.py`):
```python
import numpy as np
from app.lib.csi.parser import parse_csi_data

# CSI_DATA line: ...,<channel>,...,<len>,[imag,real,imag,real,...]
SAMPLE = ("CSI_DATA,0,aa:bb:cc:dd:ee:ff,-55,11,1,7,1,0,0,0,0,0,1,-90,0,6,1,"
          "12345,0,128,0,8,0,[3,4,0,5,-3,4,6,8]")

def test_parses_link_and_meta():
    f = parse_csi_data(SAMPLE)
    assert f.link_id == "aa:bb:cc:dd:ee:ff"
    assert f.rssi == -55
    assert f.channel == 6

def test_amplitudes_from_imag_real_pairs():
    f = parse_csi_data(SAMPLE)
    # pairs (3,4),(0,5),(-3,4),(6,8) -> 5,5,5,10
    assert np.allclose(f.amplitudes, [5, 5, 5, 10])

def test_malformed_returns_none():
    assert parse_csi_data("garbage") is None
    assert parse_csi_data("CSI_DATA,1,2") is None
    assert parse_csi_data("") is None

def test_odd_array_returns_none():
    bad = SAMPLE.replace("[3,4,0,5,-3,4,6,8]", "[3,4,0]")
    assert parse_csi_data(bad) is None
```

- [ ] **Step 2: Run, verify fail** — `cd python/wifi-sensing && python -m pytest tests/test_parser.py -v` → FAIL (module not found).

- [ ] **Step 3: Implement** `types.py` (CSIFrame dataclass) and `parser.py`. Parser: decode bytes→str, strip, require prefix `CSI_DATA`, split on the `[`; left part CSV gives mac (index 2), rssi (3), channel (16); right part is the int array; de-interleave imag/real → amplitude = hypot(real, imag). Column indices live as named constants at the top of `parser.py`. Any exception or odd-length array → return None.

- [ ] **Step 4: Run, verify pass.**

- [ ] **Step 5: Commit** `feat: add CSI_DATA parser and frame types`.

---

## Task 3: Per-link ring buffer

**Files:**
- Create: `app/lib/csi/buffer.py`, `tests/test_buffer.py`

**Interfaces:**
- Consumes: `CSIFrame` from Task 2.
- Produces:
  - `LinkBuffer(capacity: int)` with `.add(frame)`, `.window(seconds, now)` -> `(times: np.ndarray, amps: np.ndarray[time, subcarrier])`, `.resampled(rate_hz, seconds, now)` -> `np.ndarray[n_samples, subcarrier]`.
  - `BufferStore(capacity)` with `.add(frame)` (routes by `link_id`), `.links() -> list[str]`, `.get(link_id) -> LinkBuffer`, `.stats() -> dict[str, SensorStat]`.
  - `SensorStat` dataclass: `link_id, rssi, channel, packets, last_seen, malformed` (malformed updated by pipeline).

- [ ] **Step 1: Failing tests** — add frames to two link_ids; assert `links()` returns both; assert eviction past capacity; assert `window()` returns only frames within N seconds; assert `resampled()` returns fixed sample count `rate*seconds` with linear interpolation onto a regular grid; assert per-link isolation (link A frames absent from link B window).

- [ ] **Step 2: Verify fail.**

- [ ] **Step 3: Implement** using `collections.deque(maxlen=capacity)` of `(timestamp, amplitudes)` per link; `window` filters `t >= now-seconds`; `resampled` builds `np.linspace(now-seconds, now, rate*seconds)` and `np.interp`s each subcarrier column (pad/truncate subcarrier width to the most recent frame's width).

- [ ] **Step 4: Verify pass.**

- [ ] **Step 5: Commit** `feat: add per-link CSI ring buffer with resampling`.

---

## Task 4: DSP — presence, motion, vitals, waterfall

**Files:**
- Create: `app/lib/csi/dsp.py`, `tests/test_dsp.py`

**Interfaces:**
- Consumes: resampled amplitude matrix `np.ndarray[n_samples, subcarrier]`, `rate_hz`.
- Produces (pure functions):
  - `select_subcarriers(amps) -> np.ndarray` (indices, drops zero-variance/pilot, ranks by variance).
  - `presence_motion(amps, baseline: float | None, threshold: float) -> tuple[bool, float]` (occupied, motion 0..1).
  - `estimate_rate(signal: np.ndarray, rate_hz: float, lo_hz: float, hi_hz: float) -> tuple[float | None, float]` (bpm, confidence 0..1 via peak prominence; None if confidence < 0.15).
  - `vitals(amps, rate_hz, motion) -> dict` with `breathing_bpm`, `breathing_conf`, `heart_bpm`, `heart_conf` (vitals suppressed to None when motion > 0.5).
  - `waterfall(amps, max_cols=64, max_rows=128) -> list[list[float]]` (downsampled for display).
  - `baseline_variance(amps) -> float` (for calibration).

- [ ] **Step 1: Failing tests** (`tests/test_dsp.py`):
```python
import numpy as np
from app.lib.csi import dsp

RATE = 20.0
def make_signal(freq_hz, n_sub=8, secs=30, amp=5.0, noise=0.1):
    t = np.linspace(0, secs, int(RATE*secs), endpoint=False)
    base = amp*np.sin(2*np.pi*freq_hz*t)
    rng = np.random.default_rng(0)
    cols = [base + rng.normal(0, noise, t.size) + 50 for _ in range(n_sub)]
    return np.stack(cols, axis=1)

def test_breathing_15bpm():
    amps = make_signal(0.25)          # 0.25 Hz = 15 BPM
    bpm, conf = dsp.estimate_rate(amps[:,0], RATE, 0.1, 0.5)
    assert abs(bpm - 15) < 1.5 and conf > 0.3

def test_heart_72bpm():
    amps = make_signal(1.2)           # 1.2 Hz = 72 BPM
    bpm, conf = dsp.estimate_rate(amps[:,0], RATE, 0.8, 2.0)
    assert abs(bpm - 72) < 3

def test_presence_true_on_high_variance():
    amps = make_signal(0.25, amp=8.0)
    occ, motion = dsp.presence_motion(amps, baseline=0.05, threshold=1.5)
    assert occ and motion > 0

def test_presence_false_when_flat():
    rng = np.random.default_rng(1)
    amps = rng.normal(50, 0.01, (600, 8))
    occ, motion = dsp.presence_motion(amps, baseline=0.05, threshold=1.5)
    assert not occ

def test_noise_only_low_confidence():
    rng = np.random.default_rng(2)
    amps = rng.normal(50, 0.5, (600, 8))
    bpm, conf = dsp.estimate_rate(amps[:,0], RATE, 0.1, 0.5)
    assert conf < 0.3 or bpm is None

def test_vitals_suppressed_under_motion():
    amps = make_signal(0.25)
    v = dsp.vitals(amps, RATE, motion=0.9)
    assert v["breathing_bpm"] is None
```

- [ ] **Step 2: Verify fail.**

- [ ] **Step 3: Implement.** `presence_motion`: detrend per selected subcarrier, mean variance ratio vs baseline → motion = `clip(log1p(var/baseline)/log1p(threshold*K),0,1)`, occupied = `var > baseline*threshold`. `estimate_rate`: detrend (subtract mean), Hann window, rFFT, restrict to `[lo,hi]`, peak bin → bpm = `peak_freq*60`, confidence = `peak_power / (mean_band_power+eps)` normalized via `tanh`. `vitals`: pick top-variance subcarrier, call `estimate_rate` for both bands, suppress (None) when `motion>0.5`. `waterfall`: stride-downsample rows/cols, round to 3 decimals.

- [ ] **Step 4: Verify pass.**

- [ ] **Step 5: Commit** `feat: add CSI DSP (presence, motion, vitals, waterfall)`.

---

## Task 5: Config + UDP ingest

**Files:**
- Create: `app/config.py`, `app/lib/csi/ingest.py`
- (No standalone unit test — exercised by Task 7 integration test; manual smoke in step 4.)

**Interfaces:**
- Produces:
  - `Config` dataclass (from env) fields: `udp_port:int`, `analysis_rate_hz:float`, `presence_window_s:float`, `vitals_window_s:float`, `motion_threshold:float`, `data_dir:Path`. Classmethod `Config.from_env()`.
  - `CSISource` ABC with `async def frames() -> AsyncIterator[bytes]` and `async def close()`.
  - `UDPCSISource(port)`: `start()` opens datagram endpoint, pushes payloads to an `asyncio.Queue`; `frames()` yields from the queue.

- [ ] **Step 1:** Implement `config.py` reading env with defaults from Global Constraints.

- [ ] **Step 2:** Implement `ingest.py`: `UDPCSISource.start()` uses `loop.create_datagram_endpoint` with a `DatagramProtocol` whose `datagram_received` calls `queue.put_nowait(data)` (drop on full). `frames()` is `while True: yield await queue.get()`.

- [ ] **Step 3:** Smoke: `python -c "from app.config import Config; print(Config.from_env())"` → prints defaults.

- [ ] **Step 4: Commit** `feat: add config and UDP CSI ingest source`.

---

## Task 6: Pipeline orchestration

**Files:**
- Create: `app/lib/csi/pipeline.py` (extend `types.py` with `AnalyticsFrame`)

**Interfaces:**
- Consumes: `CSISource`, `BufferStore`, `dsp`, `Config`.
- Produces:
  - `AnalyticsFrame` dataclass: `timestamp, occupied:bool, motion:float, breathing_bpm, breathing_conf, heart_bpm, heart_conf, sensors:list[SensorStat], waterfall:dict[link_id,list[list[float]]]`. `.to_dict()`.
  - `Pipeline(config)` with: `async def run()` (drain ingest into buffers + count malformed), background `async def _analyze_loop()` (every `1/?`→ ~1 s produce `AnalyticsFrame`, store `latest`), `subscribe() -> asyncio.Queue`, `unsubscribe(q)`, `calibrate() -> float` (sets baseline from current window, persists to `data_dir/baseline.json`), `latest: AnalyticsFrame | None`, `stats()`.

- [ ] **Step 1:** Implement. `run()`: `async for payload in source.frames(): frame=parse_csi_data(payload); if None: malformed++ ; else store.add(frame)`. `_analyze_loop()`: every 1 s, for the primary/aggregate link compute presence_motion over `presence_window_s` resampled window and vitals over `vitals_window_s`; build `AnalyticsFrame`; push to all subscriber queues (drop if full); update `latest`. Load baseline from disk on init if present.

- [ ] **Step 2:** Commit `feat: add sensing pipeline producing AnalyticsFrame`.

(Verified by Task 7's integration test.)

---

## Task 7: FastAPI routes + app wiring

**Files:**
- Create: `app/routes/sensing.py`
- Rewrite: `app/__init__.py`

**Interfaces:**
- Consumes: `Pipeline`, `Config`.
- Produces REST: `GET /api/status`, `GET /api/sensors`, `GET /api/config`, `POST /api/calibrate`; WS `GET /ws/stream`.

- [ ] **Step 1:** Rewrite `app/__init__.py`: create `Config.from_env()`, `Pipeline(config)`, `UDPCSISource(config.udp_port)`; on FastAPI `startup` launch `asyncio.create_task(pipeline.run(source))` and the analyze loop; include `sensing.router` under `/api` and a top-level WS route; keep the SPA static fallback; drop all GStreamer code. Expose `pipeline` to routes via `api.state`.

- [ ] **Step 2:** Implement `sensing.py`: REST handlers reading `request.app.state.pipeline`; `POST /calibrate` calls `pipeline.calibrate()`; `WS /ws/stream` subscribes a queue, sends `pipeline.latest` immediately, then loops `await queue.get()` → `websocket.send_json(frame.to_dict())`, unsubscribes on disconnect.

- [ ] **Step 3: Integration test** (`tests/test_pipeline.py`): start `Pipeline` with a `UDPCSISource` on an ephemeral port; use `tools/csi_sender` helper (Task 8) to send ~30 s worth of 15-BPM-modulated frames quickly (synthetic timestamps); run analyze once; assert `latest.occupied is True` and `abs(latest.breathing_bpm-15)<2`. Use `pytest.mark.asyncio` (add `pytest-asyncio` to a dev-only path or drive the event loop manually with `asyncio.run`).

- [ ] **Step 4: Run** `python -m pytest tests/ -v` → all pass.

- [ ] **Step 5: Commit** `feat: add sensing REST+WS routes and wire pipeline`.

---

## Task 8: Synthetic CSI sender tool

**Files:**
- Create: `tools/csi_sender.py`

**Interfaces:**
- Produces: `build_csi_line(link_id, amps_int8: list[int], rssi, channel) -> str` and a CLI `python tools/csi_sender.py --host H --port P --bpm 15 --rate 20` that emits `CSI_DATA` UDP datagrams with a breathing-modulated amplitude pattern (so devs/tests need no ESP32). Importable by Task 7's test.

- [ ] **Step 1:** Implement `build_csi_line` (reverse of parser; emit int8 imag/real pairs whose magnitude encodes a 50 + sin-modulated amplitude). CLI loop sleeps `1/rate`, modulates by `sin(2*pi*(bpm/60)*t)`.

- [ ] **Step 2:** Smoke: run sender for 2 s against a netcat/UDP listener; confirm lines arrive.

- [ ] **Step 3: Commit** `feat: add synthetic CSI UDP sender for dev and tests`.

---

## Task 9: Frontend — stream hook, nav, pages

**Files:**
- Create: `frontend/src/hooks/use-sensing-stream.ts`, `frontend/src/pages/{live,sensors,waterfall}.tsx`
- Rewrite: `frontend/src/App.tsx`, `frontend/src/components/app-sidebar.tsx`, `frontend/src/components/site-header.tsx`

**Interfaces:**
- Consumes: WS `/ws/stream` emitting `AnalyticsFrame.to_dict()`; REST `/api/sensors`, `/api/status`, `POST /api/calibrate`.
- Produces: `useSensingStream()` → `{ frame: AnalyticsFrame | null, status: "connecting"|"open"|"closed" }`.

- [ ] **Step 1:** `use-sensing-stream.ts`: open `new WebSocket(\`ws://${location.host}/ws/stream\`)`, set status, parse JSON into state, auto-reconnect after 2 s on close (mirror `use-backend-health` lifecycle).

- [ ] **Step 2:** `App.tsx` routes `/` → `/live`, plus `/live`, `/sensors`, `/waterfall`. `app-sidebar.tsx` nav: Live (`ActivityIcon`), Sensors (`RadioIcon`), Waterfall (`WavesIcon`). `site-header.tsx` title "WiFi Sensing" + a connection dot fed by `useSensingStream` status.

- [ ] **Step 3:** `live.tsx`: four `Card`s — Presence (occupied/empty), Motion (0..1 + recharts sparkline of recent motion), Breathing (BPM + confidence bar), Heart Rate (BPM + confidence + `Badge` "Experimental" + tooltip). Show "—" with reason when value null. Pull from `useSensingStream`.

- [ ] **Step 4:** `sensors.tsx`: fetch `/api/sensors` every 2 s into a `Table` (MAC, RSSI, pkts/s, last-seen, channel, malformed). "Calibrate" `Button` + `AlertDialog` → `POST /api/calibrate`, toast result via `sonner`.

- [ ] **Step 5:** `waterfall.tsx`: link `Select`; `<canvas>` that scrolls left, drawing each incoming `frame.waterfall[link]` column as a viridis-ish colormap. Redraw on each frame.

- [ ] **Step 6: Build check** — `cd frontend && npm install && npm run build` → succeeds, `dist/` produced.

- [ ] **Step 7: Commit** `feat: add sensing dashboard (live, sensors, waterfall)`.

---

## Task 10: Verification

- [ ] **Step 1:** `cd python/wifi-sensing && python -m pytest tests/ -v` → all pass.
- [ ] **Step 2:** `cd frontend && npm run build` → succeeds.
- [ ] **Step 3:** Local end-to-end: `uvicorn app:api --port 3001 &`, run `python tools/csi_sender.py --host 127.0.0.1 --port 5566 --bpm 15`, open `http://localhost:3001`, confirm presence flips occupied and breathing ≈ 15 BPM, waterfall animates.
- [ ] **Step 4:** Validate `wendy.json` against schema if `wendy json` available; otherwise JSON-lint.
- [ ] **Step 5: Commit** `chore: verify wifi-sensing app end-to-end`.

## Self-Review

- **Spec coverage:** ingest (T5), parser/format (T2), buffer/windowing (T3), presence/motion/vitals/waterfall (T4), pipeline+calibration+persist (T6), REST+WS (T7), entitlements/Docker/config (T1+T5), dashboard 3 pages (T9), synthetic sender + tests (T4,T7,T8), manual on-device (T10). All spec sections covered.
- **Placeholders:** none — test code and signatures concrete.
- **Type consistency:** `CSIFrame`, `AnalyticsFrame`, `SensorStat`, `Pipeline`, `Config`, `parse_csi_data`, `presence_motion`, `estimate_rate`, `vitals`, `waterfall`, `useSensingStream` used consistently across tasks.
