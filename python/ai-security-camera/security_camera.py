#!/usr/bin/env python3
"""
AI Security Camera — DeepStream YOLO detector for NVIDIA Jetson.

Ingests one or more IP-camera RTSP streams (plug the camera into the Jetson's
Ethernet port or the same LAN), runs YOLO11n object detection + NvDCF tracking
on the GPU, and raises debounced security events when people or vehicles appear.

Serves a single web app on :{{.PORT}}
  GET /            live dashboard (MJPEG preview + rolling event log)
  GET /stream      MJPEG stream with bounding boxes
  GET /events      recent security events as JSON
  GET /events/<f>  saved event snapshot (JPEG)
  GET /snapshot    current annotated frame as a single JPEG
  GET /health      readiness/health probe
  GET /metrics     Prometheus metrics

Configuration (all optional, sane defaults):
  CAMERA_URLS       comma-separated RTSP URLs (overrides cameras.json)
  ALERT_CLASSES     comma-separated class names to alert on
                    (default: person,bicycle,car,motorcycle,bus,truck)
  ALERT_CONFIDENCE  minimum confidence to count a detection (default: 0.5)
  EVENT_COOLDOWN    seconds between repeat events for the same camera+class
                    (default: 15)
"""

import os
import sys
import gi
import gc
import re
import json
import time
import logging
import threading
from queue import Queue
from datetime import datetime, timezone
from collections import defaultdict, deque

import numpy as np
import cv2

# DeepStream environment must be set before GStreamer is initialized.
os.environ.setdefault('EGL_PLATFORM', 'device')
os.environ.setdefault(
    'LD_LIBRARY_PATH',
    '/opt/nvidia/deepstream/deepstream-7.1/lib:/usr/lib/gstreamer-1.0/deepstream:'
    '/usr/lib/aarch64-linux-gnu/gstreamer-1.0/deepstream:/usr/lib/aarch64-linux-gnu:/usr/lib',
)
os.environ.setdefault(
    'GST_PLUGIN_PATH',
    '/usr/lib/gstreamer-1.0/deepstream:/usr/lib/aarch64-linux-gnu/gstreamer-1.0/deepstream:'
    '/usr/lib/aarch64-linux-gnu/gstreamer-1.0',
)
os.environ.setdefault('GST_DEBUG', '1')

gi.require_version('Gst', '1.0')
from gi.repository import GLib, Gst  # noqa: E402

import pyds  # noqa: E402
from flask import Flask, Response, jsonify, send_from_directory  # noqa: E402
from prometheus_client import Counter, Gauge, generate_latest, REGISTRY  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
)
logger = logging.getLogger('ai-security-camera')

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
APP_PORT = int(os.environ.get('APP_PORT', '{{.PORT}}'))
DATA_DIR = os.environ.get('DATA_DIR', '/data')
EVENTS_DIR = os.path.join(DATA_DIR, 'events')
CAMERAS_FILE = '/app/cameras.json'
NVINFER_CONFIG = '/app/nvinfer_config.txt'
TRACKER_CONFIG = '/app/tracker_config.yml'

ALERT_CLASSES = set(
    c.strip() for c in os.environ.get(
        'ALERT_CLASSES', 'person,bicycle,car,motorcycle,bus,truck'
    ).split(',') if c.strip()
)
ALERT_CONFIDENCE = float(os.environ.get('ALERT_CONFIDENCE', '0.5'))
EVENT_COOLDOWN = float(os.environ.get('EVENT_COOLDOWN', '15'))
MAX_SAVED_EVENTS = int(os.environ.get('MAX_SAVED_EVENTS', '500'))

# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------
frames_processed = Counter('security_frames_processed_total', 'Frames processed', ['camera'])
detections_counter = Counter('security_detections_total', 'Detections by class', ['camera', 'class_'])
events_counter = Counter('security_events_total', 'Security events raised', ['camera', 'class_'])
fps_gauge = Gauge('security_fps', 'Frames per second', ['camera'])
cameras_online = Gauge('security_cameras_online', 'Cameras currently delivering frames')

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------
latest_frame_queue = Queue(maxsize=1)        # most recent annotated frame (BGR ndarray)
mjpeg_client_count = 0
mjpeg_client_lock = threading.Lock()

recent_events = deque(maxlen=200)            # rolling in-memory event log for the UI
events_lock = threading.Lock()

# Per (camera, class) timestamp of the last raised event, for debouncing.
_last_event_at = defaultdict(float)
_fps_state = defaultdict(lambda: {'count': 0, 'window_start': time.time()})

pipeline_status = {'state': 'starting', 'detail': 'initializing', 'started_at': time.time()}


# ---------------------------------------------------------------------------
# Web application
# ---------------------------------------------------------------------------
DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>AI Security Camera</title>
  <style>
    :root { color-scheme: dark; }
    * { box-sizing: border-box; }
    body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
           background: #0b0e14; color: #e6e6e6; }
    header { padding: 14px 20px; background: #11151f; border-bottom: 1px solid #1e2430;
             display: flex; align-items: center; gap: 12px; }
    header h1 { font-size: 16px; margin: 0; font-weight: 600; }
    .dot { width: 10px; height: 10px; border-radius: 50%; background: #f5a623; }
    .dot.live { background: #2ecc71; } .dot.error { background: #e74c3c; }
    .status { font-size: 13px; color: #9aa4b2; }
    .wrap { display: grid; grid-template-columns: 2fr 1fr; gap: 16px; padding: 16px; }
    @media (max-width: 900px) { .wrap { grid-template-columns: 1fr; } }
    .card { background: #11151f; border: 1px solid #1e2430; border-radius: 10px; overflow: hidden; }
    .card h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .05em; color: #9aa4b2;
               margin: 0; padding: 12px 14px; border-bottom: 1px solid #1e2430; }
    .video { background: #000; display: flex; align-items: center; justify-content: center; min-height: 320px; }
    .video img { width: 100%; height: auto; display: block; }
    #events { max-height: 70vh; overflow-y: auto; }
    .event { display: flex; gap: 10px; padding: 10px 14px; border-bottom: 1px solid #1a1f2b; align-items: center; }
    .badge { font-size: 11px; font-weight: 700; padding: 2px 8px; border-radius: 999px;
             background: #e74c3c22; color: #ff7b72; text-transform: uppercase; white-space: nowrap; }
    .badge.vehicle { background: #3498db22; color: #6cb6ff; }
    .event .meta { font-size: 13px; } .event .meta .t { color: #9aa4b2; font-size: 12px; }
    .event img { width: 80px; height: 48px; object-fit: cover; border-radius: 4px; margin-left: auto; }
    .empty { padding: 20px 14px; color: #9aa4b2; font-size: 13px; }
  </style>
</head>
<body>
  <header>
    <span id="dot" class="dot"></span>
    <h1>AI Security Camera</h1>
    <span id="status" class="status">connecting…</span>
  </header>
  <div class="wrap">
    <div class="card">
      <h2>Live preview</h2>
      <div class="video"><img id="live" src="/stream" alt="live preview"/></div>
    </div>
    <div class="card">
      <h2>Security events</h2>
      <div id="events"><div class="empty">No events yet.</div></div>
    </div>
  </div>
  <script>
    const VEHICLES = new Set(["car","truck","bus","motorcycle","bicycle"]);
    async function refresh() {
      try {
        const h = await (await fetch('/health')).json();
        const dot = document.getElementById('dot'), st = document.getElementById('status');
        dot.className = 'dot ' + (h.pipeline === 'live' ? 'live' : (h.pipeline === 'error' ? 'error' : ''));
        st.textContent = h.pipeline === 'live'
          ? `${h.cameras_online}/${h.cameras_total} camera(s) live`
          : `pipeline: ${h.pipeline} — ${h.detail || ''}`;
      } catch (e) {}
      try {
        const ev = (await (await fetch('/events')).json()).events || [];
        const el = document.getElementById('events');
        if (!ev.length) { el.innerHTML = '<div class="empty">No events yet.</div>'; return; }
        el.innerHTML = ev.map(e => `
          <div class="event">
            <span class="badge ${VEHICLES.has(e.class) ? 'vehicle' : ''}">${e.class}</span>
            <div class="meta"><div>${e.camera} · ${e.count}× (${(e.confidence*100).toFixed(0)}%)</div>
              <div class="t">${new Date(e.timestamp).toLocaleString()}</div></div>
            ${e.snapshot ? `<img src="/events/${e.snapshot}" alt=""/>` : ''}
          </div>`).join('');
      } catch (e) {}
    }
    refresh(); setInterval(refresh, 3000);
  </script>
</body>
</html>"""


def build_web_app():
    app = Flask(__name__)

    @app.after_request
    def cors(resp):
        resp.headers['Access-Control-Allow-Origin'] = '*'
        return resp

    @app.route('/')
    def index():
        return Response(DASHBOARD_HTML, mimetype='text/html')

    @app.route('/health')
    def health():
        return jsonify({
            'status': 'healthy',
            'pipeline': pipeline_status['state'],
            'detail': pipeline_status['detail'],
            'cameras_total': len(CAMERAS),
            'cameras_online': int(cameras_online._value.get()),
            'timestamp': datetime.now(timezone.utc).isoformat(),
        })

    @app.route('/events')
    def events():
        with events_lock:
            return jsonify({'count': len(recent_events), 'events': list(recent_events)})

    @app.route('/events/<path:filename>')
    def event_snapshot(filename):
        return send_from_directory(EVENTS_DIR, filename)

    @app.route('/snapshot')
    def snapshot():
        try:
            frame = latest_frame_queue.queue[0]
        except IndexError:
            return Response('no frame yet', status=503)
        ok, jpeg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if not ok:
            return Response('encode failed', status=500)
        return Response(jpeg.tobytes(), mimetype='image/jpeg')

    @app.route('/metrics')
    def metrics():
        return Response(generate_latest(REGISTRY), mimetype='text/plain')

    @app.route('/stream')
    def stream():
        def generate():
            global mjpeg_client_count
            with mjpeg_client_lock:
                mjpeg_client_count += 1
            try:
                while True:
                    try:
                        frame = latest_frame_queue.get(timeout=2.0)
                    except Exception:
                        continue
                    ok, jpeg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
                    if not ok:
                        continue
                    yield (b'--frame\r\n'
                           b'Content-Type: image/jpeg\r\n\r\n' + jpeg.tobytes() + b'\r\n')
            finally:
                with mjpeg_client_lock:
                    mjpeg_client_count -= 1

        return Response(generate(), mimetype='multipart/x-mixed-replace; boundary=frame')

    return app


def start_web_server():
    """Start Flask in a daemon thread so readiness on :{{.PORT}} passes immediately."""
    app = build_web_app()
    t = threading.Thread(
        target=lambda: app.run(host='0.0.0.0', port=APP_PORT, debug=False, threaded=True),
        daemon=True,
    )
    t.start()
    logger.info(f"Web dashboard on http://0.0.0.0:{APP_PORT}")


# ---------------------------------------------------------------------------
# Security event handling
# ---------------------------------------------------------------------------
def record_event(camera, class_name, count, confidence, frame):
    """Debounced security event with an optional saved snapshot."""
    key = (camera, class_name)
    now = time.time()
    if now - _last_event_at[key] < EVENT_COOLDOWN:
        return
    _last_event_at[key] = now

    snapshot_name = None
    if frame is not None:
        try:
            os.makedirs(EVENTS_DIR, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')
            snapshot_name = f"{stamp}_{camera}_{class_name}.jpg"
            cv2.imwrite(os.path.join(EVENTS_DIR, snapshot_name), frame,
                        [cv2.IMWRITE_JPEG_QUALITY, 85])
            _prune_saved_events()
        except Exception as e:
            logger.warning(f"Could not save snapshot: {e}")
            snapshot_name = None

    event = {
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'camera': camera,
        'class': class_name,
        'count': count,
        'confidence': round(confidence, 3),
        'snapshot': snapshot_name,
    }
    with events_lock:
        recent_events.appendleft(event)
    events_counter.labels(camera=camera, class_=class_name).inc()
    logger.info(f"🚨 EVENT: {count}× {class_name} on '{camera}' ({confidence*100:.0f}%)")


def _prune_saved_events():
    try:
        files = sorted(
            (f for f in os.listdir(EVENTS_DIR) if f.endswith('.jpg')),
        )
        excess = len(files) - MAX_SAVED_EVENTS
        for f in files[:max(0, excess)]:
            try:
                os.remove(os.path.join(EVENTS_DIR, f))
            except OSError:
                pass
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# DeepStream pipeline
# ---------------------------------------------------------------------------
def get_frame_data_from_buffer(gst_buffer, frame_meta):
    """Copy a frame out of the GPU buffer as a BGR ndarray.

    CRITICAL on Jetson: unmap the surface after copying or GPU memory leaks
    quickly. See deepstream-vision/detector for the original notes.
    """
    buffer_hash = hash(gst_buffer)
    batch_id = frame_meta.batch_id
    frame_copy = None
    try:
        frame_copy = pyds.get_nvds_buf_surface(buffer_hash, batch_id)
        if frame_copy is None:
            return None
        np_frame = np.array(frame_copy, copy=True)
        pyds.unmap_nvds_buf_surface(buffer_hash, batch_id)
        frame_copy = None

        total_pixels = np_frame.size // 4  # RGBA
        if total_pixels == 1920 * 1080:
            h, w = 1080, 1920
        elif total_pixels == 1280 * 720:
            h, w = 720, 1280
        elif total_pixels == 640 * 360:
            h, w = 360, 640
        else:
            h, w = frame_meta.source_frame_height, frame_meta.source_frame_width
        np_frame = np_frame.reshape((h, w, 4))
        return cv2.cvtColor(np_frame, cv2.COLOR_RGBA2BGR)
    except Exception as e:
        logger.warning(f"Frame extraction error: {e}")
        try:
            if frame_copy is not None:
                pyds.unmap_nvds_buf_surface(buffer_hash, batch_id)
        except Exception:
            pass
        return None


def osd_sink_pad_buffer_probe(pad, info, user_data):
    stream_names = user_data['stream_names']
    gst_buffer = info.get_buffer()
    if not gst_buffer:
        return Gst.PadProbeReturn.OK

    batch_meta = pyds.gst_buffer_get_nvds_batch_meta(hash(gst_buffer))
    l_frame = batch_meta.frame_meta_list

    while l_frame is not None:
        try:
            frame_meta = pyds.NvDsFrameMeta.cast(l_frame.data)
        except StopIteration:
            break

        camera = stream_names.get(frame_meta.source_id, f"camera_{frame_meta.source_id}")
        frames_processed.labels(camera=camera).inc()
        _update_fps(camera)

        # Only pay for the GPU->CPU copy when someone is watching or an event needs a snapshot.
        with mjpeg_client_lock:
            want_frame = mjpeg_client_count > 0
        frame_data = get_frame_data_from_buffer(gst_buffer, frame_meta) if want_frame else None

        # Walk detections.
        l_obj = frame_meta.obj_meta_list
        per_class_count = defaultdict(int)
        per_class_conf = defaultdict(float)
        boxes = []
        while l_obj is not None:
            try:
                obj = pyds.NvDsObjectMeta.cast(l_obj.data)
            except StopIteration:
                break
            class_name = obj.obj_label or f"class_{obj.class_id}"
            conf = float(obj.confidence)
            detections_counter.labels(camera=camera, class_=class_name).inc()
            if class_name in ALERT_CLASSES and conf >= ALERT_CONFIDENCE:
                per_class_count[class_name] += 1
                per_class_conf[class_name] = max(per_class_conf[class_name], conf)
            boxes.append((int(obj.rect_params.left), int(obj.rect_params.top),
                          int(obj.rect_params.width), int(obj.rect_params.height),
                          class_name, conf))
            try:
                l_obj = l_obj.next
            except StopIteration:
                break

        annotated = _annotate(frame_data, boxes) if frame_data is not None else None

        # Raise debounced events for alert classes seen this frame.
        for class_name, count in per_class_count.items():
            snap = annotated
            if snap is None:
                # Grab a frame just for the snapshot even if nobody is streaming.
                grabbed = get_frame_data_from_buffer(gst_buffer, frame_meta)
                snap = _annotate(grabbed, boxes) if grabbed is not None else None
            record_event(camera, class_name, count, per_class_conf[class_name], snap)

        if annotated is not None:
            _publish_frame(annotated)

        # Periodic GC to release lingering NVMM buffers.
        n = int(frames_processed.labels(camera=camera)._value.get())
        if n % 500 == 0 and n > 0:
            gc.collect()

        if frame_data is not None:
            del frame_data

        try:
            l_frame = l_frame.next
        except StopIteration:
            break

    return Gst.PadProbeReturn.OK


def _annotate(frame, boxes):
    if frame is None:
        return None
    out = frame.copy()
    for x, y, w, h, class_name, conf in boxes:
        alert = class_name in ALERT_CLASSES and conf >= ALERT_CONFIDENCE
        color = (0, 0, 255) if alert else (0, 200, 0)
        cv2.rectangle(out, (x, y), (x + w, y + h), color, 2)
        label = f"{class_name} {conf:.2f}"
        (tw, th), bl = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
        cv2.rectangle(out, (x, y - th - bl), (x + tw, y), color, -1)
        cv2.putText(out, label, (x, y - bl), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
    return out


def _publish_frame(frame):
    if latest_frame_queue.full():
        try:
            latest_frame_queue.get_nowait()
        except Exception:
            pass
    try:
        latest_frame_queue.put_nowait(frame)
    except Exception:
        pass


def _update_fps(camera):
    s = _fps_state[camera]
    s['count'] += 1
    now = time.time()
    if now - s['window_start'] >= 5.0:
        fps_gauge.labels(camera=camera).set(s['count'] / (now - s['window_start']))
        s['count'] = 0
        s['window_start'] = now
    cameras_online.set(sum(
        1 for st in _fps_state.values() if now - st['window_start'] < 10.0
    ))


def bus_call(bus, message, loop):
    t = message.type
    if t == Gst.MessageType.EOS:
        logger.info("End-of-stream")
        loop.quit()
    elif t == Gst.MessageType.WARNING:
        err, dbg = message.parse_warning()
        logger.warning(f"{err}: {dbg}")
    elif t == Gst.MessageType.ERROR:
        err, dbg = message.parse_error()
        logger.error(f"{err}: {dbg}")
        pipeline_status.update(state='error', detail=str(err))
        loop.quit()
    return True


def load_cameras():
    """Resolve the camera list, in priority order:

    1. CAMERA_URLS env (comma-separated RTSP URLs) — explicit override.
    2. Enabled entries in cameras.json — explicit config.
    3. Auto-discovery (ONVIF WS-Discovery + RTSP probe) when enabled and nothing
       explicit is configured. Controlled by the `discovery` block in
       cameras.json or the DISCOVERY env (auto|on|off, default auto).
    """
    env = os.environ.get('CAMERA_URLS', '').strip()
    if env:
        urls = [u.strip() for u in env.split(',') if u.strip()]
        return [{'name': f'camera-{i+1}', 'url': u, 'enabled': True} for i, u in enumerate(urls)]

    cfg = {}
    try:
        with open(CAMERAS_FILE) as f:
            cfg = json.load(f)
    except (OSError, ValueError) as e:
        logger.warning(f"Could not read {CAMERAS_FILE}: {e}")

    configured = [c for c in cfg.get('cameras', []) if c.get('enabled', True)]

    mode = os.environ.get('DISCOVERY', '').strip().lower()
    disc_cfg = cfg.get('discovery', {})
    if not mode:
        mode = 'on' if disc_cfg.get('enabled') else 'auto'

    # 'auto' discovers only when nothing is explicitly configured; 'on' always
    # discovers and merges; 'off' never discovers.
    should_discover = mode == 'on' or (mode == 'auto' and not configured)
    if should_discover:
        pipeline_status.update(state='discovering', detail='searching the network for cameras')
        logger.info("Discovering cameras on the network…")
        try:
            from discovery import discover_cameras
            discovered = discover_cameras(scan_554=disc_cfg.get('scan_port_554', True))
        except Exception as e:
            logger.warning(f"Discovery failed: {e}")
            discovered = []
        # Avoid duplicating cameras already configured by IP.
        configured_ips = {re.search(r'@([0-9.]+)', c['url']).group(1)
                          for c in configured if re.search(r'@([0-9.]+)', c['url'])}
        for cam in discovered:
            m = re.search(r'@([0-9.]+)', cam['url'])
            if not m or m.group(1) not in configured_ips:
                configured.append(cam)

    return configured


CAMERAS = []


def run_pipeline():
    global CAMERAS
    Gst.init(None)

    CAMERAS = load_cameras()
    if not CAMERAS:
        pipeline_status.update(state='error', detail='no cameras configured')
        logger.error("No cameras configured. Set CAMERA_URLS or edit cameras.json.")
        return
    stream_names = {i: c['name'] for i, c in enumerate(CAMERAS)}
    logger.info(f"Monitoring {len(CAMERAS)} camera(s): {[c['name'] for c in CAMERAS]}")
    logger.info(f"Alert classes: {sorted(ALERT_CLASSES)} @ conf>={ALERT_CONFIDENCE}")

    pipeline = Gst.Pipeline()
    streammux = Gst.ElementFactory.make("nvstreammux", "stream-muxer")
    pgie = Gst.ElementFactory.make("nvinfer", "primary-inference")
    tracker = Gst.ElementFactory.make("nvtracker", "tracker")
    nvvidconv = Gst.ElementFactory.make("nvvideoconvert", "convertor")
    nvosd = Gst.ElementFactory.make("nvdsosd", "onscreendisplay")
    sink = Gst.ElementFactory.make("fakesink", "fake-sink")
    if not all([pipeline, streammux, pgie, tracker, nvvidconv, nvosd, sink]):
        pipeline_status.update(state='error', detail='failed to create GStreamer elements')
        logger.error("Failed to create pipeline elements (DeepStream plugins missing?)")
        return

    nvvidconv.set_property('copy-hw', 2)  # JetPack 6.2 buffer-copy workaround
    streammux.set_property('width', 1920)
    streammux.set_property('height', 1080)
    streammux.set_property('batch-size', len(CAMERAS))
    streammux.set_property('batched-push-timeout', 40000)
    streammux.set_property('live-source', True)
    pgie.set_property('config-file-path', NVINFER_CONFIG)
    tracker.set_property('ll-lib-file',
                         '/opt/nvidia/deepstream/deepstream-7.1/lib/libnvds_nvmultiobjecttracker.so')
    tracker.set_property('ll-config-file', TRACKER_CONFIG)
    tracker.set_property('tracker-width', 640)
    tracker.set_property('tracker-height', 384)
    tracker.set_property('display-tracking-id', 1)
    sink.set_property('sync', False)
    sink.set_property('enable-last-sample', False)

    for el in (streammux, pgie, tracker, nvvidconv, nvosd, sink):
        pipeline.add(el)

    if not (streammux.link(pgie) and pgie.link(tracker) and tracker.link(nvvidconv)
            and nvvidconv.link(nvosd) and nvosd.link(sink)):
        pipeline_status.update(state='error', detail='failed to link pipeline')
        logger.error("Failed to link pipeline")
        return

    for i, cam in enumerate(CAMERAS):
        source = Gst.ElementFactory.make("uridecodebin", f"source-{i}")
        conv = Gst.ElementFactory.make("nvvideoconvert", f"nvvidconv-{i}")
        caps_filter = Gst.ElementFactory.make("capsfilter", f"filter-{i}")
        if not all([source, conv, caps_filter]):
            logger.error(f"Failed to create source elements for {cam['name']}")
            continue
        source.set_property('uri', cam['url'])
        conv.set_property('copy-hw', 2)
        caps_filter.set_property('caps', Gst.Caps.from_string("video/x-raw(memory:NVMM), format=RGBA"))

        pipeline.add(source)
        pipeline.add(conv)
        pipeline.add(caps_filter)
        if not conv.link(caps_filter):
            logger.error(f"Failed to link converter for {cam['name']}")
            continue
        sinkpad = streammux.request_pad_simple(f"sink_{i}")
        if caps_filter.get_static_pad("src").link(sinkpad) != Gst.PadLinkReturn.OK:
            logger.error(f"Failed to link {cam['name']} to streammux")
            continue

        def on_pad_added(src, pad, converter=conv, name=cam['name']):
            caps = pad.query_caps(None)
            if not caps or caps.is_empty():
                return
            if caps.get_structure(0).get_name().startswith("video/"):
                sp = converter.get_static_pad("sink")
                if sp and not sp.is_linked() and pad.link(sp) == Gst.PadLinkReturn.OK:
                    logger.info(f"✓ Linked video for {name}")

        def on_source_setup(decodebin, src_elem, name=cam['name']):
            if src_elem.get_factory().get_name() == "rtspsrc":
                src_elem.set_property('protocols', 0x4)     # TCP only (reliable)
                src_elem.set_property('latency', 500)        # 500ms jitter buffer
                src_elem.set_property('timeout', 10000000)   # 10s connect timeout (us)
                src_elem.set_property('retry', 5)
                src_elem.set_property('drop-on-latency', True)
                logger.info(f"Configured RTSP source for {name} (TCP, 500ms latency)")

        source.connect("pad-added", on_pad_added)
        source.connect("source-setup", on_source_setup)
        logger.info(f"Added camera '{cam['name']}': {cam['url']}")

    osd_sink_pad = nvosd.get_static_pad("sink")
    osd_sink_pad.add_probe(Gst.PadProbeType.BUFFER, osd_sink_pad_buffer_probe,
                           {'stream_names': stream_names})

    loop = GLib.MainLoop()
    bus = pipeline.get_bus()
    bus.add_signal_watch()
    bus.connect("message", bus_call, loop)

    pipeline_status.update(state='building', detail='building TensorRT engine (first run is slow)')
    logger.info("Starting pipeline (first run builds the TensorRT engine — this can take "
                "several minutes; it is cached to /data/engines afterwards)…")
    if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
        pipeline_status.update(state='error', detail='pipeline failed to start')
        logger.error("Unable to set pipeline to PLAYING")
        return

    pipeline_status.update(state='live', detail='running')
    try:
        loop.run()
    except KeyboardInterrupt:
        pass
    finally:
        logger.info("Stopping pipeline")
        pipeline.set_state(Gst.State.NULL)


def main():
    os.makedirs(EVENTS_DIR, exist_ok=True)
    # Web server first so the readiness probe on :{{.PORT}} passes before the slow
    # first-run TensorRT engine build.
    start_web_server()
    run_pipeline()


if __name__ == "__main__":
    main()
