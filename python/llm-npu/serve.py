#!/usr/bin/env python3
"""Chat front end for the Genie demo.

genie-t2t-run is one-shot, so every turn reloads the model and costs a few seconds
before generation starts. The DSP session is exclusive, so requests are serialised.
"""

import json
import os
import re
import subprocess
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BUNDLE = "/data/genie_bundle"
CONFIG = os.path.join(BUNDLE, "genie_config.json")
SKELS = "/app/skel"
REPORT = "/app/report.txt"
STATUS = "/app/status"

# Total bytes of the eight files fetch_bundle pulls, so the page can show real progress
# rather than a spinner. Only used for the percentage; nothing depends on it being exact.
BUNDLE_BYTES = 2665531556

# Without the Llama 3 header tokens the model ignores the turn structure and runs to
# the context limit instead of answering.
# The conversation, oldest first, as {"role": "user"|"assistant", "text": str}.
# Server-side rather than per-browser on purpose: a refresh keeps the thread, and
# in a demo everyone watching sees the same conversation.
HISTORY = []
HISTORY_LOCK = threading.Lock()

# Each turn re-sends the whole conversation -- genie-t2t-run is one-shot and keeps
# nothing between runs -- so history is not free. Prompt processing runs at about
# 165 tok/s, so a long thread is paid for on every question. Six messages keeps
# the model aware of what was just said without the wait creeping up.
HISTORY_TURNS = 6

SYSTEM = ("You are a helpful assistant running entirely on a Qualcomm device. "
          "Answer briefly.")


def build_prompt(question):
    """Llama 3.2's chat template over the recent conversation."""
    parts = ["<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n\n",
             SYSTEM, "<|eot_id|>"]
    with HISTORY_LOCK:
        recent = HISTORY[-HISTORY_TURNS:]
    for msg in recent:
        parts += ["<|start_header_id|>", msg["role"], "<|end_header_id|>\n\n",
                  msg["text"], "<|eot_id|>"]
    parts += ["<|start_header_id|>user<|end_header_id|>\n\n", question, "<|eot_id|>",
              "<|start_header_id|>assistant<|end_header_id|>\n\n"]
    return "".join(parts)
ANSWER = re.compile(r"\[BEGIN\]:(.*?)\[END\]", re.S)


def status():
    """What run.sh is doing. The server starts before the model is downloaded, so the
    page is reachable from the first second rather than several minutes in."""
    try:
        with open(STATUS) as fh:
            return fh.read().strip() or "starting"
    except OSError:
        return "starting"


def downloaded():
    """Bytes present in the bundle, counting partial downloads."""
    total = 0
    try:
        for name in os.listdir(BUNDLE):
            try:
                total += os.path.getsize(os.path.join(BUNDLE, name))
            except OSError:
                pass
    except OSError:
        pass
    return total
LOCK = threading.Lock()

PAGE = """<!doctype html>
<html class="h-full bg-black overflow-hidden">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Llama 3.2 3B on the Hexagon NPU</title>
<script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script>
</head>
<body class="h-full flex flex-col m-0 p-0 text-white font-sans bg-black overflow-hidden">

<div class="flex flex-col h-full p-6 gap-4">

  <div class="flex justify-between items-start shrink-0">
    <div>
      <a href="https://wendy.sh" target="_blank" rel="noopener noreferrer" class="flex items-center gap-2 no-underline">
        <img src="/wendy-logo.svg" alt="Wendy" class="h-6 w-auto drop-shadow-md">
        <h1 class="text-xl font-bold drop-shadow-md">Llama 3.2 3B</h1>
      </a>
      <div class="flex items-center gap-2 mt-1">
        <span id="dot" class="w-2 h-2 rounded-full bg-yellow-500 shadow-sm"></span>
        <span id="dot-text" class="text-xs font-medium uppercase tracking-wider">Starting</span>
      </div>
    </div>
    <div class="text-right text-[10px] uppercase tracking-widest text-white/50 leading-relaxed">
      <div>Qualcomm Dragonwing IQ-8275</div>
      <div>No network required</div>
    </div>
  </div>

  <div id="thread" class="flex-1 min-h-0 overflow-y-auto flex flex-col gap-3 pr-1">
    <div id="empty" class="m-auto text-white/30 text-sm">Ask it something. Everything runs on the board.</div>
  </div>

  <form id="f" class="flex gap-2 shrink-0">
    <input id="q" autocomplete="off" placeholder="Ask something&hellip;"
      class="flex-1 bg-white/5 border border-white/10 rounded-lg px-4 py-3 text-white placeholder-white/30 outline-none focus:border-blue-500/60">
    <button id="b" class="bg-blue-600 hover:bg-blue-500 disabled:opacity-40 px-6 rounded-lg font-medium">Ask</button>
    <button id="reset" type="button" title="Clear the conversation"
      class="bg-white/5 hover:bg-white/10 border border-white/10 px-4 rounded-lg text-white/60 text-sm">Clear</button>
  </form>

  <div class="shrink-0 bg-black/40 backdrop-blur-md p-3 rounded-lg border border-white/10 text-[10px] flex gap-4 uppercase tracking-widest text-white/70">
    <div><span class="font-bold text-white">Accelerator:</span> Hexagon V75 NPU</div>
    <div><span class="font-bold text-white">Model:</span> Llama 3.2 3B</div>
    <div><span class="font-bold text-white">Tokens/sec:</span> <span id="tps">&mdash;</span></div>
    <div><span class="font-bold text-white">Last answer:</span> <span id="took">&mdash;</span></div>
  </div>
</div>

<div id="overlay" class="fixed inset-0 z-20 flex items-center justify-center bg-black transition-opacity duration-500">
  <div class="text-center px-6 w-full max-w-md">
    <div class="relative w-16 h-16 mx-auto mb-4">
      <div class="absolute inset-0 border-4 border-blue-500/20 rounded-full"></div>
      <div class="absolute inset-0 border-4 border-blue-500 border-t-transparent rounded-full animate-spin"></div>
    </div>
    <p id="load-text" class="text-gray-400 font-medium tracking-wide">STARTING</p>
    <div class="w-full bg-white/10 rounded-full h-1 mt-4 overflow-hidden">
      <div id="bar" class="bg-blue-500 h-1 rounded-full transition-all" style="width:0%"></div>
    </div>
  </div>
</div>

<script>
const f=document.getElementById('f'),q=document.getElementById('q'),b=document.getElementById('b'),
      thread=document.getElementById('thread'),dot=document.getElementById('dot'),
      dotText=document.getElementById('dot-text'),overlay=document.getElementById('overlay'),
      loadText=document.getElementById('load-text'),bar=document.getElementById('bar'),
      tpsEl=document.getElementById('tps'),tookEl=document.getElementById('took');

const gb=n=>(n/1073741824).toFixed(2);

// One bubble. Returned so a pending answer can be filled in when it arrives,
// rather than re-rendering the thread and losing the scroll position.
function bubble(role,text){
  const e=document.getElementById('empty'); if(e)e.remove();
  const wrap=document.createElement('div');
  wrap.className = role==='user' ? 'flex justify-end' : 'flex justify-start';
  const el=document.createElement('div');
  el.className = (role==='user'
      ? 'bg-blue-600 text-white rounded-2xl rounded-br-sm'
      : 'bg-white/5 border border-white/10 text-white rounded-2xl rounded-bl-sm')
    + ' px-4 py-3 max-w-[78%] whitespace-pre-wrap leading-relaxed';
  el.textContent=text;
  wrap.appendChild(el);
  thread.appendChild(wrap);
  thread.scrollTop=thread.scrollHeight;
  return el;
}

// History lives on the device, so a reload rejoins the conversation.
(async()=>{
  try{ for(const m of await (await fetch('/history')).json()) bubble(m.role,m.text); }
  catch(err){}
})();

document.getElementById('reset').onclick=async()=>{
  await fetch('/reset');
  thread.innerHTML='<div id="empty" class="m-auto text-white/30 text-sm">Ask it something. Everything runs on the board.</div>';
};

async function poll(){
  let j;
  try{ j=await (await fetch('/progress')).json(); }catch(e){ setTimeout(poll,2000); return; }
  if(j.tokens_per_second) tpsEl.textContent=j.tokens_per_second;
  if(j.state==='ready'){
    dot.className='w-2 h-2 rounded-full bg-green-500 shadow-sm';
    dotText.textContent='Ready';
    b.disabled=false;
    overlay.style.opacity='0';
    setTimeout(()=>overlay.style.display='none',500);
    return;
  }
  b.disabled=true;
  if(j.state.startsWith('downloading')){
    loadText.textContent='DOWNLOADING MODEL — '+gb(j.bytes)+' / '+gb(j.total)+' GiB';
    bar.style.width=j.pct+'%';
  }else{
    loadText.textContent=j.state.toUpperCase();
  }
  setTimeout(poll,2000);
}
poll();

f.onsubmit=async e=>{
  e.preventDefault();
  const text=q.value.trim();
  if(!text)return;
  b.disabled=true;
  q.value='';
  bubble('user',text);
  const pending=bubble('assistant','Thinking…');
  pending.classList.add('text-white/40');
  const t=Date.now();
  try{
    const r=await fetch('/ask?q='+encodeURIComponent(text));
    pending.textContent=(await r.text()).trim();
  }catch(err){ pending.textContent='error: '+err; }
  pending.classList.remove('text-white/40');
  thread.scrollTop=thread.scrollHeight;
  tookEl.textContent=((Date.now()-t)/1000).toFixed(1)+'s';
  b.disabled=false;
  q.focus();
};
</script>
</body>
</html>
"""


# The running genie process, so stop() can cancel a turn in flight. One at a
# time -- the DSP session is exclusive -- so a single slot is enough.
_current = None
_current_lock = threading.Lock()


def ask(question):
    """Run one turn. Returns the generated text, or an error string."""
    global _current
    with open("/tmp/q.txt", "w") as fh:
        fh.write(build_prompt(question))

    env = dict(os.environ, ADSP_LIBRARY_PATH=SKELS)
    proc = subprocess.Popen(
        ["genie-t2t-run", "-c", CONFIG, "--prompt_file", "/tmp/q.txt"],
        cwd=BUNDLE, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True,
    )
    with _current_lock:
        _current = proc
    try:
        out, _ = proc.communicate(timeout=600)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        return "timed out"
    finally:
        with _current_lock:
            _current = None

    # A negative return code means it was signalled, i.e. stop() cancelled it.
    if proc.returncode is not None and proc.returncode < 0:
        return "stopped"

    found = ANSWER.search(out or "")
    if not found:
        return "no answer in output:\n" + (out or "")[-400:]
    answer = found.group(1).strip()
    # Only a real answer joins the thread: a cancelled or failed turn would
    # otherwise teach the model that empty replies are acceptable.
    with HISTORY_LOCK:
        HISTORY.append({"role": "user", "text": question})
        HISTORY.append({"role": "assistant", "text": answer})
        del HISTORY[:-40]
    return answer

def _tokens_per_second():
    """The throughput measured by run.sh at start-up, if it got that far."""
    try:
        with open(REPORT) as fh:
            found = re.search(r"tps-generate:([0-9.]+)", fh.read())
        return round(float(found.group(1)), 1) if found else None
    except OSError:
        return None

class Handler(BaseHTTPRequestHandler):
    def _send(self, body, ctype="text/plain; charset=utf-8"):
        raw = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path == "/":
            return self._send(PAGE, "text/html; charset=utf-8")
        if url.path == "/history":
            with HISTORY_LOCK:
                return self._send(json.dumps(HISTORY), "application/json")
        if url.path == "/reset":
            with HISTORY_LOCK:
                HISTORY.clear()
            return self._send('{"ok":true}', "application/json")
        if url.path == "/wendy-logo.svg":
            try:
                with open("/app/wendy-logo.svg", "rb") as fh:
                    raw = fh.read()
            except OSError:
                return self.send_error(404)
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            return self.wfile.write(raw)
        if url.path == "/report.txt":
            try:
                with open(REPORT) as fh:
                    return self._send(fh.read())
            except OSError as err:
                return self._send("no report: %s" % err)
        if url.path == "/status":
            return self._send(status())
        if url.path == "/progress":
            have = downloaded()
            pct = min(100, round(100 * have / BUNDLE_BYTES)) if BUNDLE_BYTES else 0
            return self._send(
                '{"state":%s,"bytes":%d,"total":%d,"pct":%d,"tokens_per_second":%s}'
                % (json.dumps(status()), have, BUNDLE_BYTES, pct,
                   json.dumps(_tokens_per_second())),
                "application/json",
            )
        if url.path == "/ask":
            if status() != "ready":
                return self._send("still preparing: " + status())
            question = urllib.parse.parse_qs(url.query).get("q", [""])[0].strip()
            if not question:
                return self._send("usage: /ask?q=your+question")
            # One DSP session at a time; a second concurrent run would fail anyway.
            with LOCK:
                return self._send(ask(question))
        self.send_error(404)

    def log_message(self, fmt, *args):  # quieter than the default access log
        pass


if __name__ == "__main__":
    print("chat on :{{.PORT}}  (/ for the page, /ask?q=... for plain text)", flush=True)
    ThreadingHTTPServer(("", {{.PORT}}), Handler).serve_forever()
