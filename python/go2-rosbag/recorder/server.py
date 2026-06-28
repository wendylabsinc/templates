"""Go2 topic recorder + inspector — discover every DDS topic the Go2 exposes,
inspect each one (message schema, a live sample, publish rate, pubs/subs),
grab ready-to-use code snippets, and record an mcap rosbag of all topics or a
chosen subset.

Runs inside a ROS 2 Humble + CycloneDDS + Unitree-messages container (see
entrypoint.sh, which binds DDS to the robot interface before launching this).
Exposes a web UI + JSON API on PORT (default 7000, host networking).

  GET  /                     control + inspector UI
  GET  /api/topics           live `ros2 topic list -t`
  GET  /api/topic?name=/x    per-topic detail: type, message schema, pubs/subs, one-shot sample
  GET  /api/hz?name=/x        measured publish rate (sampled ~5s)
  GET  /api/status           recording state + current bag size
  GET  /api/bags             recorded bags on the persist volume
  POST /api/record/start     body {"topics":[...]} records that subset; no body / [] records all (-a)
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
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse

DATA = Path("/data")
PORT = int(os.environ.get("PORT", "7000"))
AUTO_RECORD = os.environ.get("AUTO_RECORD", "0") == "1"

# Names accepted from the client before being passed to ros2 as argv. We don't
# shell-interpolate (argv, not a shell string), but still reject anything that
# isn't a plain ROS name so a leading '-' can't be misread as a CLI flag.
_TOPIC_RE = re.compile(r"^/[A-Za-z0-9_/]+$")
_TYPE_RE = re.compile(r"^[A-Za-z0-9_]+(/[A-Za-z0-9_]+)+$")
_SAMPLE_CAP = 4000  # echo output can be huge (e.g. point clouds) — truncate

app = FastAPI(title="go2-rosbag")

# Single in-flight recorder.
_rec = {"proc": None, "path": None, "started": None}


def _dir_size(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def _run(cmd, timeout):
    """Run a ros2 CLI command in its own session. `ros2 topic hz/echo` run
    forever; on timeout we SIGINT the whole group (clean stop) and return
    whatever was printed so far. Returns (stdout, finished_within_timeout)."""
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, start_new_session=True,
    )
    try:
        out, _ = proc.communicate(timeout=timeout)
        return out, True
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGINT)
        except Exception:  # noqa: BLE001
            pass
        try:
            out, _ = proc.communicate(timeout=3)
        except Exception:  # noqa: BLE001
            out = ""
        return out, False


def list_topics() -> dict:
    try:
        out = subprocess.run(
            ["ros2", "topic", "list", "-t"],
            capture_output=True, text=True, timeout=20,
        )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e), "topics": []}
    topics = []
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        # "/topic [pkg/msg/Type]"
        if " [" in line and line.endswith("]"):
            name, typ = line.split(" [", 1)
            topics.append({"name": name.strip(), "type": typ[:-1]})
        else:
            topics.append({"name": line, "type": ""})
    topics.sort(key=lambda t: t["name"])
    return {"ok": True, "count": len(topics), "topics": topics}


def is_recording() -> bool:
    p = _rec["proc"]
    return p is not None and p.poll() is None


@app.get("/api/topics")
def api_topics() -> dict:
    return list_topics()


@app.get("/api/topic")
def api_topic(name: str) -> dict:
    """Everything you need to understand one topic: its type, the full message
    schema, who publishes/subscribes (with QoS), and one live sample."""
    if not _TOPIC_RE.match(name):
        raise HTTPException(400, "bad topic name")
    typ_out, _ = _run(["ros2", "topic", "type", name], 10)
    typ = typ_out.strip().splitlines()[0].strip() if typ_out.strip() else ""

    schema = ""
    if _TYPE_RE.match(typ):
        s_out, _ = _run(["ros2", "interface", "show", typ], 10)
        schema = s_out.strip()

    info_out, _ = _run(["ros2", "topic", "info", name, "-v"], 10)

    sample, got = _run(["ros2", "topic", "echo", name, "--once"], 6)
    sample = sample.strip()
    if not sample:
        sample = "(no message received within 6s — topic may be idle)"
    elif len(sample) > _SAMPLE_CAP:
        sample = sample[:_SAMPLE_CAP] + "\n… (truncated)"

    return {
        "name": name,
        "type": typ,
        "schema": schema or "(type has no resolvable definition)",
        "info": info_out.strip(),
        "sample": sample,
    }


@app.get("/api/hz")
def api_hz(name: str) -> dict:
    """Measured publish rate, sampled for ~5s."""
    if not _TOPIC_RE.match(name):
        raise HTTPException(400, "bad topic name")
    out, _ = _run(["ros2", "topic", "hz", name], 5)
    hz = None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("average rate:"):
            try:
                hz = float(line.split(":", 1)[1].strip())
            except Exception:  # noqa: BLE001
                pass
    if hz is None:
        return {"name": name, "hz": None, "note": "no messages in 5s (idle)"}
    return {"name": name, "hz": round(hz, 2)}


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
async def api_start(request: Request) -> dict:
    if is_recording():
        raise HTTPException(409, "already recording")
    # Optional JSON body {"topics": ["/a", "/b"]} — record just those; empty /
    # absent body records everything (-a).
    topics = None
    try:
        body = await request.json()
        if isinstance(body, dict):
            topics = body.get("topics")
    except Exception:  # noqa: BLE001
        topics = None
    sel = []
    if topics:
        sel = [t for t in topics if isinstance(t, str) and _TOPIC_RE.match(t)]
        if not sel:
            raise HTTPException(400, "no valid topics in selection")

    ts = time.strftime("%Y%m%d_%H%M%S")
    path = DATA / f"bag_{ts}"
    # -s mcap: single self-contained file (opens in Foxglove). -a: all topics.
    cmd = ["ros2", "bag", "record", "-s", "mcap", "-o", str(path)]
    cmd += sel if sel else ["-a"]
    proc = subprocess.Popen(cmd, start_new_session=True)  # own group for clean SIGINT
    _rec.update(proc=proc, path=str(path), started=ts)
    return {"ok": True, "bag": path.name, "topics": (len(sel) if sel else "all")}


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
    --amber:#ffb429;--teal:#2dd4bf;--bad:#ff6b6b;--chip:#1c2230;}
  *{box-sizing:border-box;} body{margin:0;background:var(--bg);color:var(--ink);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;line-height:1.5;}
  .wrap{max-width:920px;margin:0 auto;padding:26px 20px 60px;}
  h1{font-size:23px;font-weight:800;margin:0 0 2px;} .sub{color:var(--muted);font-size:14px;margin:0 0 20px;}
  .bar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;background:var(--panel);
    border:1px solid var(--line);border-radius:14px;padding:16px;margin-bottom:18px;}
  button{border:none;border-radius:10px;padding:10px 16px;font-weight:700;font-size:14px;cursor:pointer;}
  .rec{background:linear-gradient(92deg,#ff6b6b,#ff9a8b);color:#0c0e12;}
  .sel{background:linear-gradient(92deg,var(--amber),#ffd27a);color:#0c0e12;}
  .stop{background:linear-gradient(92deg,var(--teal),#7af0e0);color:#0c0e12;}
  button:disabled{opacity:.45;cursor:not-allowed;}
  .pill{margin-left:auto;font-size:13px;color:var(--muted);}
  .dot{display:inline-block;width:9px;height:9px;border-radius:50%;background:#444;margin-right:6px;vertical-align:middle;}
  .dot.on{background:#ff6b6b;box-shadow:0 0 8px #ff6b6b;animation:pulse 1s infinite;}
  @keyframes pulse{50%{opacity:.4;}}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px 18px;margin-bottom:18px;}
  .card h2{font-size:15px;margin:0 0 10px;display:flex;align-items:center;gap:8px;}
  .count{color:var(--muted);font-weight:400;font-size:13px;}
  .tools{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px;}
  input.flt{flex:1;min-width:160px;background:#0a0c10;border:1px solid var(--line);color:var(--ink);
    border-radius:9px;padding:9px 12px;font-size:14px;}
  .mini{font-size:12px;color:var(--muted);background:var(--chip);border:1px solid var(--line);
    border-radius:8px;padding:6px 10px;font-weight:600;}
  .grp{margin:8px 0 2px;color:var(--amber);font-size:12px;font-weight:700;font-family:ui-monospace,Menlo,monospace;
    cursor:pointer;user-select:none;}
  .grp .gc{color:var(--muted);font-weight:400;}
  .row{display:flex;gap:10px;align-items:flex-start;flex-wrap:wrap;padding:6px 4px;border-bottom:1px solid rgba(255,255,255,.05);}
  .row input[type=checkbox]{margin-top:4px;}
  .nm{font-family:ui-monospace,Menlo,monospace;color:#cfe9e3;cursor:pointer;font-size:13px;word-break:break-all;}
  .nm:hover{text-decoration:underline;}
  .ty{color:var(--muted);font-family:ui-monospace,Menlo,monospace;font-size:12px;margin-left:auto;text-align:right;white-space:nowrap;}
  .detail{flex-basis:100%;margin:8px 0 4px;background:#0a0c10;border:1px solid var(--line);border-radius:10px;padding:12px;}
  .detail h3{margin:10px 0 4px;font-size:12px;color:var(--amber);text-transform:uppercase;letter-spacing:.04em;}
  .detail h3:first-child{margin-top:0;}
  pre{margin:0;white-space:pre-wrap;word-break:break-word;font-family:ui-monospace,Menlo,monospace;
    font-size:12px;color:#d6e2ea;max-height:280px;overflow:auto;background:#06080b;border-radius:8px;padding:10px;}
  .dbtn{font-size:12px;font-weight:600;background:var(--chip);border:1px solid var(--line);color:var(--ink);
    border-radius:8px;padding:5px 9px;margin:0 6px 6px 0;cursor:pointer;}
  .hz{color:var(--teal);font-size:12px;font-family:ui-monospace,Menlo,monospace;margin-left:8px;}
  a{color:var(--teal);}
  .bag{display:flex;gap:12px;align-items:center;padding:8px 0;border-bottom:1px solid rgba(255,255,255,.05);font-size:14px;}
  .bag .nmb{font-family:ui-monospace,Menlo,monospace;} .bag .sz{color:var(--muted);}
  .links{margin-left:auto;display:flex;gap:10px;}
</style></head><body><div class="wrap">
  <h1>🐕 Go2 Topic Recorder &amp; Inspector</h1>
  <p class="sub">Discover every DDS topic the Go2 exposes — inspect its schema, see a live sample,
    grab usage snippets, and record an mcap rosbag (all topics or just the ones you pick). Opens in Foxglove.</p>

  <div class="bar">
    <button class="rec" id="startBtn" onclick="recordAll()">● Record all</button>
    <button class="sel" id="selBtn" onclick="recordSel()" disabled>● Record selected (0)</button>
    <button class="stop" id="stopBtn" onclick="stop()" disabled>■ Stop &amp; save</button>
    <span class="pill" id="status"><span class="dot" id="dot"></span>idle</span>
  </div>

  <div class="card">
    <h2>Topics <span class="count" id="tcount"></span>
      <a href="#" onclick="loadTopics();return false" style="margin-left:auto;font-size:13px;">↻ refresh</a></h2>
    <div class="tools">
      <input class="flt" id="flt" placeholder="filter by name or type…" oninput="render()">
      <span class="mini" id="selcount">0 selected</span>
      <button class="dbtn" onclick="clearSel()">clear</button>
    </div>
    <div id="topics"><div style="color:var(--muted)">loading…</div></div>
  </div>

  <div class="card">
    <h2>Recorded bags <a href="#" onclick="loadBags();return false" style="margin-left:auto;font-size:13px;">↻</a></h2>
    <div id="bags">—</div>
  </div>
</div>
<script>
  let TOPICS=[], COLLAPSED={}, SEL=new Set(), OPEN=new Set();
  async function j(u,m,b){const o={method:m||"GET"};if(b){o.headers={"Content-Type":"application/json"};o.body=JSON.stringify(b);}const r=await fetch(u,o);return r.json();}
  function esc(s){return (s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");}
  function enc(s){return s.replace(/\\\\/g,"\\\\\\\\").replace(/'/g,"\\\\'");}
  function grpOf(n){const i=n.lastIndexOf("/");return i<=0?"/":n.slice(0,i);}
  function idOf(n){return "d_"+btoa(unescape(encodeURIComponent(n))).replace(/[^a-zA-Z0-9]/g,"");}
  function pkgMsg(t){const p=(t||"").split("/");return p.length>=2?[p[0],p[p.length-1]]:["pkg","Msg"];}

  async function loadTopics(){
    const d=await j("/api/topics");
    document.getElementById("tcount").textContent=d.ok?("· "+d.count+" topics"):("· error: "+(d.error||""));
    TOPICS=d.topics||[]; render();
  }
  function render(){
    const q=document.getElementById("flt").value.trim().toLowerCase();
    const list=TOPICS.filter(t=>!q||t.name.toLowerCase().includes(q)||(t.type||"").toLowerCase().includes(q));
    const groups={};
    list.forEach(t=>{(groups[grpOf(t.name)]=groups[grpOf(t.name)]||[]).push(t);});
    const keys=Object.keys(groups).sort();
    let html="";
    if(!keys.length){html="<div style='color:var(--muted)'>no topics match.</div>";}
    keys.forEach(g=>{
      const col=COLLAPSED[g];
      html+="<div class='grp' onclick=\\"toggleGrp('"+enc(g)+"')\\">"+(col?"▸":"▾")+" "+esc(g)+" <span class='gc'>("+groups[g].length+")</span></div>";
      if(col)return;
      groups[g].forEach(t=>{
        const id=idOf(t.name);
        const ck=SEL.has(t.name)?"checked":"";
        const op=OPEN.has(t.name);
        html+="<div class='row'>"+
          "<input type='checkbox' "+ck+" onchange=\\"toggleSel('"+enc(t.name)+"')\\">"+
          "<span class='nm' onclick=\\"toggleDetail('"+enc(t.name)+"')\\">"+esc(t.name)+"</span>"+
          "<span class='ty'>"+esc(t.type||"")+"</span>"+
          "<div class='detail' id='"+id+"' style='display:"+(op?"block":"none")+"'></div>"+
        "</div>";
      });
    });
    document.getElementById("topics").innerHTML=html;
    OPEN.forEach(n=>fillDetail(n));
    updateSelUI();
  }
  function toggleGrp(g){COLLAPSED[g]=!COLLAPSED[g];render();}
  function toggleSel(n){if(SEL.has(n))SEL.delete(n);else SEL.add(n);updateSelUI();}
  function clearSel(){SEL.clear();render();}
  function updateSelUI(){
    document.getElementById("selcount").textContent=SEL.size+" selected";
    const b=document.getElementById("selBtn");
    b.textContent="● Record selected ("+SEL.size+")";
    b.disabled=(SEL.size===0)||(window._recording===true);
  }
  function toggleDetail(n){
    const el=document.getElementById(idOf(n));
    if(OPEN.has(n)){OPEN.delete(n);if(el){el.style.display="none";}return;}
    OPEN.add(n);if(el){el.style.display="block";fillDetail(n);}
  }
  async function fillDetail(n){
    const el=document.getElementById(idOf(n));
    if(!el||el.dataset.loaded==="1")return; el.dataset.loaded="1";
    el.innerHTML="<span style='color:var(--muted)'>inspecting "+esc(n)+" …</span>";
    const d=await j("/api/topic?name="+encodeURIComponent(n));
    const pm=pkgMsg(d.type), pkg=pm[0], msg=pm[1];
    const stub="import rclpy\\nfrom rclpy.node import Node\\nfrom "+pkg+".msg import "+msg+"\\n\\n"+
      "class Sub(Node):\\n    def __init__(self):\\n        super().__init__('sub')\\n"+
      "        self.create_subscription("+msg+", '"+n+"', self.cb, 10)\\n"+
      "    def cb(self, m): self.get_logger().info(str(m))\\n\\n"+
      "rclpy.init(); rclpy.spin(Sub())";
    const hzid="hz_"+idOf(n);
    el.innerHTML=
      "<h3>type</h3><pre>"+esc(d.type||"(unknown)")+"</pre>"+
      "<h3>schema <span class='hz' id='"+hzid+"'></span>"+
        "<button class='dbtn' style='margin-left:8px' onclick=\\"measHz('"+enc(n)+"','"+hzid+"')\\">measure rate</button></h3>"+
      "<pre>"+esc(d.schema)+"</pre>"+
      "<h3>publishers / subscribers</h3><pre>"+esc(d.info)+"</pre>"+
      "<h3>live sample (echo --once)</h3><pre>"+esc(d.sample)+"</pre>"+
      "<h3>use it</h3>"+
      "<button class='dbtn' data-cp=\\""+encodeURIComponent("ros2 topic echo "+n)+"\\" onclick='cp(this)'>copy: ros2 topic echo</button>"+
      "<button class='dbtn' data-cp=\\""+encodeURIComponent("ros2 topic info "+n+" -v")+"\\" onclick='cp(this)'>copy: ros2 topic info</button>"+
      "<button class='dbtn' data-cp=\\""+encodeURIComponent(stub)+"\\" onclick='cp(this)'>copy: rclpy subscriber</button>";
  }
  async function measHz(n,hzId){
    const el=document.getElementById(hzId); if(el)el.textContent=" measuring…";
    const d=await j("/api/hz?name="+encodeURIComponent(n));
    if(el)el.textContent=(d.hz!=null)?(" "+d.hz+" Hz"):(" "+(d.note||"idle"));
  }
  function cp(b){navigator.clipboard.writeText(decodeURIComponent(b.dataset.cp));
    const o=b.textContent;b.textContent="copied ✓";setTimeout(()=>{b.textContent=o;},900);}

  async function loadBags(){
    const d=await j("/api/bags");
    document.getElementById("bags").innerHTML=(d.bags&&d.bags.length)?d.bags.map(b=>
      "<div class='bag'><span class='nmb'>"+esc(b.name)+"</span><span class='sz'>"+b.size_mb+" MB</span>"+
      "<span class='links'>"+(b.mcap?"<a href='/download?bag="+encodeURIComponent(b.name)+"&fmt=mcap'>⬇ .mcap</a>":"")+
      "<a href='/download?bag="+encodeURIComponent(b.name)+"&fmt=tar'>⬇ .tar.gz</a></span></div>").join(""):"<span class='sz'>No bags yet.</span>";
  }
  async function refreshStatus(){
    const s=await j("/api/status");
    window._recording=s.recording;
    const st=document.getElementById("status");
    document.getElementById("startBtn").disabled=s.recording;
    document.getElementById("stopBtn").disabled=!s.recording;
    updateSelUI();
    if(s.recording){st.innerHTML="<span class='dot on'></span>recording "+esc(s.bag||"")+" — "+s.size_mb+" MB";}
    else{st.innerHTML="<span class='dot'></span>idle";}
  }
  async function recordAll(){await j("/api/record/start","POST",{});refreshStatus();loadBags();}
  async function recordSel(){if(!SEL.size)return;await j("/api/record/start","POST",{topics:[...SEL]});refreshStatus();loadBags();}
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
            ts = time.strftime("%Y%m%d_%H%M%S")
            path = DATA / f"bag_{ts}"
            proc = subprocess.Popen(
                ["ros2", "bag", "record", "-a", "-s", "mcap", "-o", str(path)],
                start_new_session=True,
            )
            _rec.update(proc=proc, path=str(path), started=ts)
            print("[rosbag] AUTO_RECORD started", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[rosbag] auto-record failed: {e}", flush=True)


if __name__ == "__main__":
    import threading
    threading.Thread(target=_bootstrap, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info", access_log=False)
