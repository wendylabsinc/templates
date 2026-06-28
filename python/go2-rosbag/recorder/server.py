"""Go2 topic recorder — list every DDS topic the Go2 exposes and record an
mcap rosbag of them all.

Runs inside a ROS 2 Humble + CycloneDDS + Unitree-messages container (see
entrypoint.sh, which binds DDS to the robot interface before launching this).
Exposes a tiny web UI + JSON API on PORT (default 7000, host networking).

  GET  /                     control UI
  GET  /api/topics           live `ros2 topic list -t`
  GET  /api/status           recording state + current bag size
  GET  /api/bags             recorded bags on the persist volume
  POST /api/record/start     start `ros2 bag record -a` (mcap)
  POST /api/record/stop      SIGINT the recorder so it finalizes cleanly
  GET  /download?bag=NAME&fmt=mcap|tar
"""
import os
import re
import signal
import subprocess
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse

DATA = Path("/data")
PORT = int(os.environ.get("PORT", "7000"))
AUTO_RECORD = os.environ.get("AUTO_RECORD", "0") == "1"

app = FastAPI(title="go2-rosbag")

# Single in-flight recorder.
_rec = {"proc": None, "path": None, "started": None}


def _dir_size(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


_TOPIC_RE = re.compile(r"^\s*\*\s+(\S+)\s+\[([^\]]+)\]")
_COUNT_RE = re.compile(r"(\d+)\s+(?:publisher|subscriber)")


def list_topics() -> dict:
    """Every topic on the DDS graph, with publisher/subscriber counts so the
    UI can show whether a topic is actually usable.

    `ros2 topic list -v` splits into "Published topics:" (≥1 publisher) and
    "Subscribed topics:" (≥1 subscriber). We merge them and classify:
      live     — has publisher(s): real data flowing (recordable)
      service  — no publisher but something subscribes: a callable endpoint
                 (e.g. /api/*/request). Listening != hardware guaranteed.
      idle     — advertised only.
    """
    try:
        out = subprocess.run(
            ["ros2", "topic", "list", "-v"],
            capture_output=True, text=True, timeout=25,
        )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e), "topics": []}

    info: dict = {}
    section = None
    for line in out.stdout.splitlines():
        s = line.strip().lower()
        if s.startswith("published topics"):
            section = "pubs"
            continue
        if s.startswith("subscribed topics"):
            section = "subs"
            continue
        m = _TOPIC_RE.match(line)
        if not m or section is None:
            continue
        name, typ = m.group(1), m.group(2)
        cm = _COUNT_RE.search(line)
        cnt = int(cm.group(1)) if cm else 1
        rec = info.setdefault(name, {"name": name, "type": typ, "pubs": 0, "subs": 0})
        if not rec["type"]:
            rec["type"] = typ
        rec[section] = cnt

    # Fallback to the plain list if -v gave us nothing (older distros).
    if not info:
        try:
            plain = subprocess.run(["ros2", "topic", "list", "-t"],
                                   capture_output=True, text=True, timeout=20)
            for line in plain.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                if " [" in line and line.endswith("]"):
                    name, typ = line.split(" [", 1)
                    info[name] = {"name": name, "type": typ[:-1], "pubs": 0, "subs": 0}
                else:
                    info[line] = {"name": line, "type": "", "pubs": 0, "subs": 0}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e), "topics": []}

    topics = []
    for name in sorted(info):
        r = info[name]
        r["status"] = "live" if r["pubs"] > 0 else ("service" if r["subs"] > 0 else "idle")
        topics.append(r)
    live = sum(1 for t in topics if t["status"] == "live")
    return {"ok": True, "count": len(topics), "live": live, "topics": topics}


def is_recording() -> bool:
    p = _rec["proc"]
    return p is not None and p.poll() is None


@app.get("/api/topics")
def api_topics() -> dict:
    return list_topics()


@app.get("/api/status")
def api_status() -> dict:
    rec = is_recording()
    size = 0
    if _rec["path"] and Path(_rec["path"]).exists():
        size = _dir_size(Path(_rec["path"]))
    return {
        "recording": rec,
        "bag": Path(_rec["path"]).name if _rec["path"] else None,
        "started": _rec["started"],
        "size_mb": round(size / 1e6, 1),
    }


@app.post("/api/record/start")
def api_start() -> dict:
    if is_recording():
        raise HTTPException(409, "already recording")
    ts = time.strftime("%Y%m%d_%H%M%S")
    path = DATA / f"bag_{ts}"
    # -a: all topics; -s mcap: single self-contained file (opens in Foxglove).
    proc = subprocess.Popen(
        ["ros2", "bag", "record", "-a", "-s", "mcap", "-o", str(path)],
        start_new_session=True,  # own process group so we can SIGINT it cleanly
    )
    _rec.update(proc=proc, path=str(path), started=ts)
    return {"ok": True, "bag": path.name}


@app.post("/api/record/stop")
def api_stop() -> dict:
    if not is_recording():
        return {"ok": True, "stopped": False}
    p = _rec["proc"]
    try:
        # SIGINT (Ctrl-C) → rosbag2 finalizes metadata.yaml + flushes the mcap.
        os.killpg(os.getpgid(p.pid), signal.SIGINT)
        p.wait(timeout=20)
    except Exception:  # noqa: BLE001
        try:
            p.terminate()
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True, "stopped": True, "bag": Path(_rec["path"]).name if _rec["path"] else None}


@app.get("/api/bags")
def api_bags() -> dict:
    bags = []
    if DATA.exists():
        for d in sorted(DATA.iterdir(), reverse=True):
            if not d.is_dir():
                continue
            mcaps = list(d.glob("*.mcap"))
            bags.append({
                "name": d.name,
                "size_mb": round(_dir_size(d) / 1e6, 1),
                "mcap": mcaps[0].name if mcaps else None,
            })
    return {"count": len(bags), "bags": bags}


@app.get("/download")
def download(bag: str, fmt: str = "mcap"):
    # Guard against path traversal — only direct children of /data.
    d = (DATA / bag).resolve()
    if d.parent != DATA.resolve() or not d.is_dir():
        raise HTTPException(404, "no such bag")
    if fmt == "mcap":
        mcaps = list(d.glob("*.mcap"))
        if not mcaps:
            raise HTTPException(404, "no mcap in bag (still recording?)")
        return FileResponse(mcaps[0], media_type="application/octet-stream",
                            filename=f"{bag}.mcap")
    # fmt=tar → stream a tar.gz of the whole bag dir (for `ros2 bag play`)
    import io
    import tarfile

    def gen():
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            tf.add(d, arcname=bag)
        buf.seek(0)
        yield from buf

    return StreamingResponse(gen(), media_type="application/gzip", headers={
        "Content-Disposition": f'attachment; filename="{bag}.tar.gz"'})


PAGE = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Go2 Topic Recorder</title>
<style>
  :root{--bg:#0c0e12;--panel:#14171d;--ink:#e7ebf0;--muted:#9aa6b2;--line:#262c36;
    --amber:#ffb429;--teal:#2dd4bf;--bad:#ff6b6b;}
  *{box-sizing:border-box;} body{margin:0;background:var(--bg);color:var(--ink);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;line-height:1.5;}
  .wrap{max-width:880px;margin:0 auto;padding:26px 20px 60px;}
  h1{font-size:23px;font-weight:800;margin:0 0 2px;} .sub{color:var(--muted);font-size:14px;margin:0 0 20px;}
  .bar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;background:var(--panel);
    border:1px solid var(--line);border-radius:14px;padding:16px;margin-bottom:18px;}
  button{border:none;border-radius:10px;padding:11px 18px;font-weight:700;font-size:14px;cursor:pointer;}
  .rec{background:linear-gradient(92deg,#ff6b6b,#ff9a8b);color:#0c0e12;}
  .stop{background:linear-gradient(92deg,var(--teal),#7af0e0);color:#0c0e12;}
  button:disabled{opacity:.45;cursor:not-allowed;}
  .pill{margin-left:auto;font-size:13px;color:var(--muted);}
  .dot{display:inline-block;width:9px;height:9px;border-radius:50%;background:#444;margin-right:6px;vertical-align:middle;}
  .dot.on{background:#ff6b6b;box-shadow:0 0 8px #ff6b6b;animation:pulse 1s infinite;}
  @keyframes pulse{50%{opacity:.4;}}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px 18px;margin-bottom:18px;}
  .card h2{font-size:15px;margin:0 0 10px;display:flex;align-items:center;gap:8px;}
  .count{color:var(--muted);font-weight:400;font-size:13px;}
  table{width:100%;border-collapse:collapse;font-size:13px;}
  td{padding:5px 6px;border-bottom:1px solid rgba(255,255,255,.05);vertical-align:top;}
  td.t{color:#cfe9e3;font-family:ui-monospace,Menlo,monospace;}
  td.ty{color:var(--muted);font-family:ui-monospace,Menlo,monospace;}
  a{color:var(--teal);}
  .bag{display:flex;gap:12px;align-items:center;padding:8px 0;border-bottom:1px solid rgba(255,255,255,.05);font-size:14px;}
  .bag .nm{font-family:ui-monospace,Menlo,monospace;}
  .bag .sz{color:var(--muted);}
  .links{margin-left:auto;display:flex;gap:10px;}
  td.av{white-space:nowrap;width:1%;padding-right:12px;}
  .b{font-size:11px;font-weight:700;padding:2px 8px;border-radius:999px;white-space:nowrap;}
  .b.live{background:rgba(45,212,191,.16);color:#9ff0e3;}
  .b.service{background:rgba(255,180,41,.16);color:#ffd591;}
  .b.idle{background:rgba(255,255,255,.06);color:#9aa6b2;}
  .cnt{color:var(--muted);font-size:11px;margin-left:7px;}
  .legend{font-size:12px;color:var(--muted);margin:0 0 12px;line-height:2;}
</style></head><body><div class="wrap">
  <h1>🐕 Go2 Topic Recorder</h1>
  <p class="sub">Every DDS topic the Go2 exposes — list them and record an mcap rosbag (opens directly in Foxglove).</p>

  <div class="bar">
    <button class="rec" id="startBtn" onclick="start()">● Start recording</button>
    <button class="stop" id="stopBtn" onclick="stop()" disabled>■ Stop &amp; save</button>
    <span class="pill" id="status"><span class="dot" id="dot"></span>idle</span>
  </div>

  <div class="card">
    <h2>Topics <span class="count" id="tcount"></span> <a href="#" onclick="loadTopics();return false" style="margin-left:auto;font-size:13px;">↻ refresh</a></h2>
    <div class="legend">
      <span class="b live">● live</span> publishing data — recordable &nbsp;·&nbsp;
      <span class="b service">○ service</span> endpoint listening — callable (e.g. <code>/api/*/request</code>); doesn't guarantee the hardware is attached &nbsp;·&nbsp;
      <span class="b idle">idle</span> advertised only
    </div>
    <table id="topics"><tbody><tr><td>loading…</td></tr></tbody></table>
  </div>

  <div class="card">
    <h2>Recorded bags <a href="#" onclick="loadBags();return false" style="margin-left:auto;font-size:13px;">↻</a></h2>
    <div id="bags">—</div>
  </div>
</div>
<script>
  async function j(u,m){const r=await fetch(u,{method:m||"GET"});return r.json();}
  async function loadTopics(){
    const d=await j("/api/topics");
    document.getElementById("tcount").textContent=d.ok?("· "+d.count+" topics · "+(d.live||0)+" live"):("· error: "+(d.error||""));
    document.getElementById("topics").innerHTML="<tbody>"+(d.topics||[]).map(t=>{
      const lbl=t.status==="live"?"● live":(t.status==="service"?"○ service":"idle");
      const cnt=t.status==="live"?(t.pubs+" pub"):(t.subs>0?(t.subs+" sub"):"");
      return "<tr><td class='av'><span class='b "+t.status+"'>"+lbl+"</span>"+
        (cnt?"<span class='cnt'>"+cnt+"</span>":"")+"</td>"+
        "<td class='t'>"+t.name+"</td><td class='ty'>"+(t.type||"")+"</td></tr>";
    }).join("")+"</tbody>";
  }
  async function loadBags(){
    const d=await j("/api/bags");
    document.getElementById("bags").innerHTML=(d.bags&&d.bags.length)?d.bags.map(b=>
      "<div class='bag'><span class='nm'>"+b.name+"</span><span class='sz'>"+b.size_mb+" MB</span>"+
      "<span class='links'>"+(b.mcap?"<a href='/download?bag="+b.name+"&fmt=mcap'>⬇ .mcap</a>":"")+
      "<a href='/download?bag="+b.name+"&fmt=tar'>⬇ .tar.gz</a></span></div>").join(""):"<span class='sz'>No bags yet.</span>";
  }
  async function refreshStatus(){
    const s=await j("/api/status");
    const dot=document.getElementById("dot"), st=document.getElementById("status");
    document.getElementById("startBtn").disabled=s.recording;
    document.getElementById("stopBtn").disabled=!s.recording;
    if(s.recording){dot.className="dot on";st.innerHTML="<span class='dot on'></span>recording "+s.bag+" — "+s.size_mb+" MB";}
    else{dot.className="dot";st.innerHTML="<span class='dot'></span>idle";}
  }
  async function start(){await j("/api/record/start","POST");refreshStatus();loadBags();}
  async function stop(){await j("/api/record/stop","POST");setTimeout(()=>{refreshStatus();loadBags();},1500);}
  loadTopics();loadBags();refreshStatus();
  setInterval(refreshStatus,2000);
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return PAGE


def _bootstrap():
    # Give DDS discovery a moment, dump the topic list, optionally auto-record.
    time.sleep(4)
    t = list_topics()
    try:
        (DATA / "topics.txt").write_text(
            "\n".join(f"{x['name']}\t{x['type']}" for x in t.get("topics", [])),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        pass
    print(f"[rosbag] discovered {t.get('count', 0)} topics", flush=True)
    if AUTO_RECORD:
        try:
            api_start()
            print("[rosbag] AUTO_RECORD started", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[rosbag] auto-record failed: {e}", flush=True)


if __name__ == "__main__":
    import threading
    threading.Thread(target=_bootstrap, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info", access_log=False)
