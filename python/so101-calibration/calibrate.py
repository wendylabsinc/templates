#!/usr/bin/env python3
"""
SO-101 calibration: a browser-driven, headless-friendly Wendy app.

Calibration records, per joint, where "zero/middle" is (a homing offset) and how
far it travels (range min/max), so raw servo ticks (0-4095) can be turned into a
normalized, arm-independent joint frame: the space LeRobot models and datasets
use.

Wendy containers are headless (no terminal), so this replaces LeRobot's
interactive `lerobot-calibrate` with a small web flow: you move the arm by hand
and click capture buttons. Torque is never enabled: the app only pings servos
and reads their present position, so the arm stays limp and back-drivable.

Procedure (open http://<device>:<SO101_CALIB_HTTP_PORT>):
  1. Move the arm to its neutral "middle" rest pose  -> click  Set home
  2. Click  Start range capture, sweep every joint through its full travel,
     then click  Stop range capture
  3. Click  Save  -> writes a LeRobot-convention calibration JSON to /data

Output (LeRobot per-motor shape) at SO101_DATA_DIR/<SO101_CALIB_ID>.json:
  { "<joint>": {"id", "drive_mode", "homing_offset", "range_min", "range_max"} }

Env:
  SO101_CALIB_PORT      serial device; "auto" (default) probes /dev/serial/by-id/*,
                        /dev/ttyACM* and /dev/ttyUSB* for answering servos
  SO101_BAUD            1000000
  SO101_SERVO_IDS       1,2,3,4,5,6
  SO101_JOINT_NAMES     shoulder_pan,shoulder_lift,elbow_flex,wrist_flex,wrist_roll,gripper
  SO101_GRIPPER_JOINT   gripper (normalized 0..100 instead of -100..100)
  SO101_CALIB_HTTP_PORT 8090
  SO101_DATA_DIR        /data   (persist volume)
  SO101_CALIB_ID        follower
"""

import json
import os
import threading
import time

from flask import Flask, Response, jsonify
from scservo_sdk import COMM_SUCCESS, PacketHandler, PortHandler

import port_detect

CONFIGURED_PORT = port_detect.configured_port(os.environ.get(port_detect.ENV_NAME))
BAUD = int(os.environ.get("SO101_BAUD", "1000000"))
SERVO_IDS = [int(x) for x in os.environ.get("SO101_SERVO_IDS", "1,2,3,4,5,6").split(",") if x.strip()]
JOINTS = [j.strip() for j in os.environ.get(
    "SO101_JOINT_NAMES",
    "shoulder_pan,shoulder_lift,elbow_flex,wrist_flex,wrist_roll,gripper").split(",") if j.strip()]
GRIPPER = os.environ.get("SO101_GRIPPER_JOINT", "gripper")
HTTP_PORT = int(os.environ.get("SO101_CALIB_HTTP_PORT", "8090"))
DATA_DIR = os.environ.get("SO101_DATA_DIR", "/data")
CALIB_ID = os.environ.get("SO101_CALIB_ID", "follower")
# While no usable port is found (arm unplugged or unpowered), probe again this often.
RETRY_SECONDS = float(os.environ.get("SO101_PORT_RETRY_SECONDS", "5"))
# A joint's range is "sufficient" once it has been swept at least this many raw
# ticks (4096 ticks = a full turn). The gripper travels less, so it has its own,
# smaller floor. Tune per your arm if a joint is mechanically limited.
MIN_RANGE = int(os.environ.get("SO101_MIN_RANGE_TICKS", "1000"))
MIN_GRIPPER = int(os.environ.get("SO101_MIN_GRIPPER_TICKS", "300"))
# Spurious servo reads spike straight to the encoder boundary (0 / 4095). A real
# joint moved by hand can't teleport, so during a sweep we reject any reading
# that jumps more than MAX_JUMP ticks from the last accepted one. If we reject
# RESYNC_AFTER in a row, the joint genuinely did move fast, so re-sync to it.
MAX_JUMP = int(os.environ.get("SO101_MAX_JUMP_TICKS", "500"))
RESYNC_AFTER = int(os.environ.get("SO101_RESYNC_AFTER", "5"))
# A properly-homed SO-101 joint never legitimately sits within EDGE ticks of the
# 0/4095 encoder boundary (the real extreme we saw was ~142). Reads that land in
# that band are spurious wraparound/garbage, so drop them outright during a sweep.
EDGE = int(os.environ.get("SO101_EDGE_TICKS", "25"))

ADDR_PRESENT_POSITION = 56
HALF_TURN = 2048  # 12-bit encoder midpoint (STS3215)

app = Flask(__name__)
NAME_TO_ID = dict(zip(JOINTS, SERVO_IDS))


def log(message):
    print(message, flush=True)


class Calibrator:
    def __init__(self):
        self.packet = PacketHandler(0)  # STS/SMS protocol
        self.port = None              # PortHandler once a port is open
        self.port_dev = None          # e.g. /dev/ttyACM0
        self.port_msg = "looking for the arm's serial port..."
        self.port_auto = CONFIGURED_PORT is None
        self.ok = False
        self.bus = threading.Lock()   # one servo transaction at a time
        self.lock = threading.Lock()
        self.homing = {}              # joint -> homing_offset (raw_middle - 2048)
        self.raw_min = {}             # joint -> raw min seen during sweep
        self.raw_max = {}             # joint -> raw max seen during sweep
        self.last_good = {}           # joint -> last accepted raw (glitch filter)
        self.sweeping = False
        threading.Thread(target=self._connect_loop, daemon=True).start()
        threading.Thread(target=self._sweep_loop, daemon=True).start()

    def _ping_port(self, dev):
        # Opens dev, pings each servo ID and closes it again. A ping only reads
        # (the servo replies with its ID and model number): nothing is written.
        port = PortHandler(dev)
        if not port.openPort():
            raise OSError("could not open the port")
        try:
            if not port.setBaudRate(BAUD):
                raise OSError(f"{BAUD} baud is not supported")
            found = []
            for sid in SERVO_IDS:
                _model, comm, _err = self.packet.ping(port, sid)
                if comm == COMM_SUCCESS:
                    found.append(sid)
            return found
        finally:
            port.closePort()

    def _open(self, dev):
        port = PortHandler(dev)
        try:
            if port.openPort() and port.setBaudRate(BAUD):
                return port, None
            return None, f"could not open {dev} at {BAUD} baud"
        except Exception as exc:  # noqa: BLE001
            return None, f"could not open {dev}: {exc}"

    def _connect_loop(self):
        # Pick the port (configured, or the one where servos answer), open it and
        # stop. Until then, probe again every RETRY_SECONDS so plugging in or
        # powering the arm after start-up is picked up without a restart.
        last = None
        while not self.ok:
            with self.bus:
                choice = port_detect.select_port(CONFIGURED_PORT, self._ping_port, SERVO_IDS)
                message = choice.message
                if choice.device is not None:
                    port, err = self._open(choice.device)
                    if port is not None:
                        self.port, self.port_dev, self.ok = port, choice.device, True
                    else:
                        message = err
            if not self.ok:
                message += f" (retrying every {RETRY_SECONDS:g}s)"
            self.port_msg = message
            if message != last:
                log(("serial port: " if self.ok else "serial port not ready: ") + message)
                last = message
            if not self.ok:
                time.sleep(RETRY_SECONDS)

    def read_raw(self, joint):
        sid = NAME_TO_ID.get(joint)
        if sid is None or not self.ok:
            return None
        with self.bus:
            try:
                v, comm, err = self.packet.read2ByteTxRx(self.port, sid, ADDR_PRESENT_POSITION)
            except Exception:  # noqa: BLE001
                return None
        if comm != COMM_SUCCESS or err != 0:
            return None
        return v & 0x0FFF

    def _sweep_loop(self):
        miss = {}
        while True:
            if self.sweeping:
                for j in JOINTS:
                    raw = self.read_raw(j)
                    if raw is None:
                        continue
                    if raw < EDGE or raw > 4095 - EDGE:
                        continue  # encoder-boundary glitch, never a real homed pose
                    lg = self.last_good.get(j)
                    if lg is not None and abs(raw - lg) > MAX_JUMP:
                        miss[j] = miss.get(j, 0) + 1
                        if miss[j] < RESYNC_AFTER:
                            continue  # reject a glitch spike to the encoder boundary
                    miss[j] = 0
                    self.last_good[j] = raw
                    with self.lock:
                        self.raw_min[j] = raw if j not in self.raw_min else min(self.raw_min[j], raw)
                        self.raw_max[j] = raw if j not in self.raw_max else max(self.raw_max[j], raw)
            time.sleep(0.02)

    def set_home(self):
        with self.lock:
            for j in JOINTS:
                raw = self.read_raw(j)
                if raw is not None:
                    self.homing[j] = raw - HALF_TURN
        return {"homing_offset": dict(self.homing)}

    def normalized(self, joint, raw):
        # homed frame, then -> [-100,100] (or [0,100] for the gripper)
        if joint not in self.homing or raw is None:
            return None
        homed = raw - self.homing[joint]
        rmin = self.raw_min.get(joint)
        rmax = self.raw_max.get(joint)
        if rmin is None or rmax is None or rmax <= rmin:
            return None
        lo = rmin - self.homing[joint]
        hi = rmax - self.homing[joint]
        frac = (homed - lo) / (hi - lo)
        frac = max(0.0, min(1.0, frac))
        return round(frac * 100.0, 1) if joint == GRIPPER else round(frac * 200.0 - 100.0, 1)

    def status(self, joint):
        # Per-joint calibration sufficiency. Returns (state, span):
        #   "home"   home not set yet
        #   "sweep"  homed, but range not captured
        #   "narrow" swept too little (< floor), likely not moved end-to-end
        #   "ok"     homed + swept a full-enough range
        if joint not in self.homing:
            return "home", None
        rmin = self.raw_min.get(joint)
        rmax = self.raw_max.get(joint)
        if rmin is None or rmax is None:
            return "sweep", None
        span = rmax - rmin
        floor = MIN_GRIPPER if joint == GRIPPER else MIN_RANGE
        return ("narrow" if span < floor else "ok"), span

    def live(self):
        out = {}
        for j in JOINTS:
            raw = self.read_raw(j)
            with self.lock:
                homed = (raw - self.homing[j]) if (raw is not None and j in self.homing) else None
                state, span = self.status(j)
            out[j] = {
                "raw": raw,
                "homed": homed,
                "norm": self.normalized(j, raw),
                "range": [self.raw_min.get(j), self.raw_max.get(j)],
                "span": span,
                "status": state,
                "homed_set": j in self.homing,
            }
        return out

    def calibration(self):
        # LeRobot per-motor shape; range_* in the homed frame.
        cal = {}
        with self.lock:
            for j in JOINTS:
                if j not in self.homing or j not in self.raw_min or j not in self.raw_max:
                    continue
                ho = self.homing[j]
                cal[j] = {
                    "id": NAME_TO_ID[j],
                    "drive_mode": 0,
                    "homing_offset": int(ho),
                    "range_min": int(self.raw_min[j] - ho),
                    "range_max": int(self.raw_max[j] - ho),
                }
        return cal

    def save(self):
        cal = self.calibration()
        if len(cal) != len(JOINTS):
            return {"error": "incomplete: set home AND sweep every joint first",
                    "have": list(cal.keys())}
        warnings = []
        with self.lock:
            for j in JOINTS:
                state, span = self.status(j)
                if state == "narrow":
                    warnings.append(f"{j}: only {span} ticks swept; move it further and re-save")
        os.makedirs(DATA_DIR, exist_ok=True)
        path = os.path.join(DATA_DIR, f"{CALIB_ID}.json")
        with open(path, "w") as f:
            json.dump(cal, f, indent=2)
        return {"saved": path, "warnings": warnings, "sufficient": not warnings, "calibration": cal}


CAL = None


@app.route("/positions")
def positions():
    return jsonify({
        "ok": CAL.ok,
        "port": CAL.port_dev,
        "port_msg": CAL.port_msg,
        "port_auto": CAL.port_auto,
        "sweeping": CAL.sweeping,
        "joints": CAL.live(),
    })


@app.route("/home")
def http_home():
    return jsonify(CAL.set_home())


@app.route("/sweep/start")
def http_sweep_start():
    with CAL.lock:
        CAL.raw_min.clear()
        CAL.raw_max.clear()
        CAL.last_good.clear()
    CAL.sweeping = True
    return jsonify({"sweeping": True})


@app.route("/sweep/stop")
def http_sweep_stop():
    CAL.sweeping = False
    return jsonify({"sweeping": False, "range": {j: [CAL.raw_min.get(j), CAL.raw_max.get(j)] for j in JOINTS}})


@app.route("/save")
def http_save():
    return jsonify(CAL.save())


@app.route("/")
def index():
    return Response(PAGE.replace("__DATA_DIR__", DATA_DIR), mimetype="text/html")


PAGE = """<!doctype html><html><head><meta charset=utf-8><title>SO-101 calibration</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>body{font-family:ui-monospace,Menlo,monospace;background:#0c0c0c;color:#e8e8e8;margin:20px}
button{font-size:15px;padding:9px 14px;margin:5px}.muted{color:#888}
table{border-collapse:collapse;margin:10px 0}td,th{border:1px solid #444;padding:5px 12px;text-align:right}
th{color:#9cf}td.j{text-align:left;color:#9cf}.bad{color:#f66}.ok{color:#5f5}.warn{color:#fd5}#msg{margin:8px 0;color:#fd5}
#port{margin:6px 0}</style>
<script>
const ST={ok:['ok','ok'],narrow:['warn','narrow'],sweep:['bad','not swept'],home:['bad','set home']};
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'})[c]);}
async function go(p){const r=await(await fetch(p)).json();document.getElementById('msg').textContent=JSON.stringify(r);}
async function poll(){let d;try{d=await(await fetch('/positions')).json();}catch(e){return;}
  let ok=0,tot=0;for(const j in d.joints){tot++;if(d.joints[j].status==='ok')ok++;}
  document.getElementById('port').innerHTML=d.ok?
    `serial port <span class=ok>${esc(d.port)}</span> <span class=muted>(${d.port_auto?'auto-detected':'set by SO101_CALIB_PORT'})</span> · <span class=muted>${esc(d.port_msg)}</span>`:
    `<span class=bad>no arm connected yet:</span> ${esc(d.port_msg)}`;
  document.getElementById('hdr').innerHTML=`bus ${d.ok?'<span class=ok>OK</span>':'<span class=bad>NOT OPEN</span>'} · sweep ${d.sweeping?'<span class=ok>ON</span>':'off'} · `+
    `calibration <span class=${ok===tot?'ok':'bad'}>${ok}/${tot} ${ok===tot?'sufficient':'incomplete'}</span>`;
  let h='<tr><th class=j>joint</th><th>raw</th><th>homed</th><th>norm</th><th>min</th><th>max</th><th>Δ</th><th>status</th></tr>';
  for(const j in d.joints){const x=d.joints[j];const s=ST[x.status]||['bad','?'];
    h+=`<tr><td class=j>${j}${x.homed_set?' ✓':''}</td><td>${x.raw??'—'}</td><td>${x.homed??'—'}</td>`+
       `<td>${x.norm??'—'}</td><td>${x.range[0]??'—'}</td><td>${x.range[1]??'—'}</td>`+
       `<td>${x.span??'—'}</td><td class=${s[0]}>${s[1]}</td></tr>`;}
  document.getElementById('tbl').innerHTML=h;}
setInterval(poll,300);poll();
</script></head><body>
<h2>SO-101 calibration <span class=muted>— move the arm by hand (it stays limp)</span></h2>
<div id=port class=muted>…</div>
<div id=hdr class=muted>…</div>
<ol class=muted>
<li>Move the arm to its neutral middle pose → <b>Set home</b></li>
<li><b>Start range capture</b>, sweep every joint end-to-end, then <b>Stop</b></li>
<li><b>Save</b> → writes the calibration to __DATA_DIR__</li></ol>
<button onclick="go('/home')">Set home (middle)</button>
<button onclick="go('/sweep/start')">Start range capture</button>
<button onclick="go('/sweep/stop')">Stop range capture</button>
<button onclick="go('/save')">Save</button>
<div id=msg></div>
<table id=tbl></table>
</body></html>"""


def main():
    global CAL
    CAL = Calibrator()
    mode = f"{port_detect.ENV_NAME}={CONFIGURED_PORT}" if CONFIGURED_PORT else "auto-detecting the serial port"
    log(f"calibration UI on http://0.0.0.0:{HTTP_PORT} ({mode})")
    app.run(host="0.0.0.0", port=HTTP_PORT, threaded=True)


if __name__ == "__main__":
    main()
