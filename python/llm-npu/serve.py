#!/usr/bin/env python3
"""Qwen3-4B on the Hexagon NPU, behind a small chat page.

This process is PID 1 and owns the DSP. It opens its port before doing any work, then
works out which board it is on, fetches that board's model once, and proves the model
answers on the DSP. Only then does it report ready. It never signals a running Genie
process (see MAX_ANSWER_TOKENS).

genie-t2t-run is one-shot, so every turn reloads the model. The DSP session is
exclusive, so everything that touches the DSP is serialised.
"""

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_DIR = "/app"
DATA_DIR = "/data"
SKELS = "/app/skel"
PORT = {{.PORT}}

# Qualcomm AI Hub's Genie export of Qwen3-4B. Context binaries are compiled per SoC, so
# each board has its own bundle, pinned by release and hash. Release v0.64.0 is built
# with QAIRT 2.45, which the 2.47 runtime in this image loads.
MODEL_NAME = "qwen3_4b"
MODEL_RELEASE = "v0.64.0"
MODEL_BASE_URL = ("https://qaihub-public-assets.s3.us-west-2.amazonaws.com/"
                  "qai-hub-models/models")
BOARDS = {
    "V73": {"name": "IQ-9075", "chip": "qcs9075",
            "sha256": "4f2fa3b52849587eb325304ed50dd20d4ad0ffb73c40e13695d31dbdf4e54dd8"},
    "V75": {"name": "IQ-8275", "chip": "qcs8275",
            "sha256": "41d054bb1a8a51ef3dd7af293cd729519742b236acb84f0d0732bc0aa10e9d2b"},
}

# Nothing here ever signals a running Genie process: SIGTERM to a live genie-t2t-run
# wedged an IQ-9075's DSP until a power cycle. The Agent waits 30 seconds for an NPU app
# to exit, so instead a turn is kept short enough to finish inside that window. A full
# 200-token turn with a full history measured 15-17 s on an IQ-9075.
MAX_ANSWER_TOKENS = 200
# Far beyond any capped turn, so a run this long is stuck rather than slow.
STUCK_AFTER_SECONDS = 120
DELIVERY_SECONDS = 2  # how long a stop waits for a finished answer to reach its client

# Each turn re-sends the conversation, because genie-t2t-run keeps nothing between runs.
# Six messages keep the model aware of what was just said without the wait creeping up.
HISTORY_TURNS = 6
HISTORY = []
HISTORY_LOCK = threading.Lock()

SYSTEM = ("You are a helpful assistant running entirely on a Qualcomm device. "
          "Answer briefly.")
CAPPED_CONFIG = "genie_config_capped.json"  # beside the bundle's, whose paths are relative
ANSWER = re.compile(r"\[BEGIN\]:(.*?)\[END\]", re.S)
TPS = re.compile(r"tps-prompt:([0-9.]+) tps-generate:([0-9.]+)")
ARCH = re.compile(r"Hexagon Architecture (V[0-9]+)")
# With --log verbose, Genie's log entries land between the tokens of the answer.
GENIE_LOG = re.compile(r"\n*Genie:\s+[0-9.]+ms \[[^\]]*\][^\n]*\n*")

STOPPING = threading.Event()
DSP_LOCK = threading.Lock()  # one DSP user at a time
CHILD_LOCK = threading.Lock()  # guards _child against a stop that arrives mid-start
_child = None
TURNS_CHANGED = threading.Condition()
_turns_open = 0  # requests that have not finished sending their answer

STATE_LOCK = threading.Lock()
STATE = {"state": "starting", "bytes": 0, "total": 0, "pct": 0,
         "tokens_per_second": None, "board": None, "accelerator": None}


class Stopping(Exception):
    """The app was asked to stop, so no new work may start."""


class PrepareError(Exception):
    """The app cannot serve on this board. The message is shown to the user."""


def set_state(**changes):
    with STATE_LOCK:
        STATE.update(changes)


def get_state():
    with STATE_LOCK:
        return dict(STATE)


def report_path():
    return os.path.join(APP_DIR, "report.txt")


def append_report(text):
    with open(report_path(), "a") as fh:
        fh.write(text.rstrip("\n") + "\n")


def run_child(cmd, **kwargs):
    """Run a process that may touch the DSP, tracked so a stop can wait for it.

    Returns (returncode, stdout). A run that looks stuck is reported and left alone,
    because signalling it risks wedging the DSP for everything after it.
    """
    global _child
    with CHILD_LOCK:
        if STOPPING.is_set():
            raise Stopping()
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, errors="replace", **kwargs)
        _child = proc
    try:
        try:
            out, _ = proc.communicate(timeout=STUCK_AFTER_SECONDS)
        except subprocess.TimeoutExpired:
            set_state(state="failed: a DSP process is stuck; reboot the device")
            print("a DSP process is stuck; leaving it alone", flush=True)
            out, _ = proc.communicate()
        return proc.returncode, out or ""
    finally:
        with CHILD_LOCK:
            _child = None


def run_genie(prompt, verbose=False):
    """One genie-t2t-run, holding the DSP for its whole life."""
    with DSP_LOCK:
        prompt_file = "/tmp/q.txt"
        with open(prompt_file, "w") as fh:
            fh.write(prompt)
        cmd = ["genie-t2t-run", "-c", CAPPED_CONFIG, "--prompt_file", prompt_file]
        if verbose:
            cmd += ["--log", "verbose"]
        env = dict(os.environ, ADSP_LIBRARY_PATH=SKELS)
        return run_child(cmd, cwd=model_dir(), env=env)


def answer_in(output):
    """The generated text in Genie's output, without its log lines, or None."""
    found = ANSWER.search(output)
    if found is None:
        return None
    return GENIE_LOG.sub("", found.group(1)).strip()


def clean(text):
    """Keep chat text from closing its own turn with the template's control tokens."""
    return text.replace("<|im_start|>", "").replace("<|im_end|>", "")


def chat_prompt(system, messages):
    """Qwen3's chat template. The empty think block is the bundle's own sample prompt's
    way of asking for an answer without the model's reasoning."""
    parts = ["<|im_start|>system\n", clean(system), "<|im_end|>\n"]
    for msg in messages:
        parts += ["<|im_start|>", msg["role"], "\n", clean(msg["text"]), "<|im_end|>\n"]
    parts.append("<|im_start|>assistant\n<think>\n\n</think>\n\n")
    return "".join(parts)


# ---- getting ready ----------------------------------------------------------------

def model_dir():
    chip = get_state().get("chip")
    return os.path.join(DATA_DIR, "%s-%s-%s" % (MODEL_NAME, chip, MODEL_RELEASE))


def fetch_with_resume(url, part, total):
    """Download to part, picking up where an interrupted attempt stopped."""
    have = os.path.getsize(part) if os.path.exists(part) else 0
    failures = 0
    while have < total:
        if STOPPING.is_set():
            raise Stopping()
        request = urllib.request.Request(url, headers={"Range": "bytes=%d-" % have})
        try:
            with urllib.request.urlopen(request, timeout=60) as resp:
                if have and resp.status != 206:
                    have = 0  # the server ignored the range; start over, don't append
                with open(part, "ab" if have else "wb") as fh:
                    while True:
                        if STOPPING.is_set():
                            raise Stopping()
                        chunk = resp.read(1 << 20)
                        if not chunk:
                            break
                        fh.write(chunk)
                        have += len(chunk)
                        failures = 0
                        set_state(bytes=have, pct=min(100, round(100 * have / total)))
        except (urllib.error.URLError, OSError) as err:
            failures += 1
            if failures >= 6:
                raise PrepareError("could not download the model: %s" % err)
            time.sleep(5)
    if have != total:
        raise PrepareError("downloaded %d bytes of the model, expected %d" % (have, total))


def verify_sha256(path, expected):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            if STOPPING.is_set():
                raise Stopping()
            block = fh.read(1 << 22)
            if not block:
                break
            digest.update(block)
    if digest.hexdigest() != expected:
        os.remove(path)  # a retry must fetch it again, not trust these bytes
        raise PrepareError("the downloaded model failed its checksum")


def extract(archive, dest):
    """Unpack into a temporary directory and rename, so a partial model is never used.

    zipfile checks each member's CRC while reading it.
    """
    tmp = dest + ".tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    with zipfile.ZipFile(archive) as zf:
        for member in zf.infolist():
            if member.is_dir():
                continue
            if STOPPING.is_set():
                raise Stopping()
            target = os.path.join(tmp, os.path.basename(member.filename))
            with zf.open(member) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
    os.rename(tmp, dest)


def ensure_model(board):
    """Fetch and unpack this board's bundle, once."""
    dest = model_dir()
    if os.path.isfile(os.path.join(dest, "genie_config.json")):
        return
    name = "%s-genie-w4a16-qualcomm_%s.zip" % (MODEL_NAME, board["chip"])
    url = "%s/%s/releases/%s/%s" % (MODEL_BASE_URL, MODEL_NAME, MODEL_RELEASE, name)
    archive = dest + ".zip"
    if not os.path.exists(archive):
        with urllib.request.urlopen(
                urllib.request.Request(url, method="HEAD"), timeout=60) as resp:
            total = int(resp.headers["Content-Length"])
        # The archive and the unpacked copy coexist for a moment.
        free = shutil.disk_usage(DATA_DIR).free
        if free < 2.5 * total:
            raise PrepareError("%s has %.1f GB free; the model needs %.1f GB while unpacking"
                               % (DATA_DIR, free / 1e9, 2.5 * total / 1e9))
        set_state(state="downloading the model", total=total)
        part = archive + ".part"
        fetch_with_resume(url, part, total)
        os.rename(part, archive)
    set_state(state="verifying the model")
    verify_sha256(archive, board["sha256"])
    set_state(state="unpacking the model")
    extract(archive, dest)
    os.remove(archive)


def write_capped_config():
    """The bundle's config plus the answer cap, leaving Qualcomm's file as shipped."""
    with open(os.path.join(model_dir(), "genie_config.json")) as fh:
        config = json.load(fh)
    config["dialog"]["max-num-tokens"] = MAX_ANSWER_TOKENS
    with open(os.path.join(model_dir(), CAPPED_CONFIG), "w") as fh:
        json.dump(config, fh, indent=4)


def warm_up():
    """Prove the model answers on the DSP, and measure it."""
    set_state(state="loading the model onto the DSP")
    prompt = chat_prompt("You are a helpful assistant.",
                         [{"role": "user", "text": "Write a paragraph about the ocean."}])
    code, out = run_genie(prompt, verbose=True)
    # A verbose run logs about 15 MB. The end of it is what a diagnosis needs.
    with open(os.path.join(APP_DIR, "genie.log"), "w") as fh:
        fh.write(out[-(1 << 18):])
    answer = answer_in(out) or ""
    append_report("\n=== model warm-up on the DSP ===\n  rc=%s" % code)
    if code != 0 or not answer:
        errors = re.findall(r"Failed to create device: [0-9]+|Device Creation failure", out)
        tail = "\n".join("  " + line for line in out.splitlines()[-25:])
        append_report(tail)
        raise PrepareError("the model did not answer on the DSP (rc=%s%s)"
                           % (code, ", " + errors[0] if errors else ""))
    append_report("  answer: %s" % answer[:160].replace("\n", " "))
    rates = TPS.findall(out)
    if rates:
        prompt_rate, generate_rate = rates[-1]
        append_report("  tps-prompt:%s tps-generate:%s" % (prompt_rate, generate_rate))
        set_state(tokens_per_second=round(float(generate_rate), 1))


def prepare():
    try:
        open(report_path(), "w").close()  # the container layer outlives a restart
        set_state(state="checking the NPU")
        with DSP_LOCK:
            code, out = run_child(["sh", os.path.join(APP_DIR, "report.sh")])
        append_report(out)
        found = ARCH.search(out)
        if not found:
            raise PrepareError("no Hexagon DSP answered the platform validator")
        arch = found.group(1)
        set_state(accelerator="Hexagon %s NPU" % arch)
        board = BOARDS.get(arch)
        if board is None:
            raise PrepareError("no model bundle for a Hexagon %s DSP in this app" % arch)
        set_state(board=board["name"], chip=board["chip"])
        append_report("=== model ===\n  %s %s for %s" % (MODEL_NAME, MODEL_RELEASE, board["chip"]))
        ensure_model(board)
        write_capped_config()
        warm_up()
        if get_state()["state"].startswith("failed"):
            return  # a stuck run was reported while warm_up waited on it
        set_state(state="ready")
        print("ready: %s on %s" % (MODEL_NAME, board["name"]), flush=True)
    except Stopping:
        pass
    except Exception as err:  # the page must show why, whatever went wrong
        message = "failed: %s" % err
        set_state(state=message)
        try:
            append_report("\n=== startup failed ===\n  %s" % err)
        except OSError:
            pass
        print(message, flush=True)


# ---- answering --------------------------------------------------------------------

def ask(question):
    """Run one turn. Returns (http status, text)."""
    with HISTORY_LOCK:
        recent = HISTORY[-HISTORY_TURNS:]
    messages = recent + [{"role": "user", "text": question}]
    try:
        code, out = run_genie(chat_prompt(SYSTEM, messages))
    except Stopping:
        return 503, "the app is stopping"
    answer = answer_in(out)
    if code != 0 or not answer:
        return 500, "no answer (rc=%s):\n%s" % (code, out[-400:])
    # Only a real answer joins the thread: a failed turn would otherwise teach the model
    # that empty replies are acceptable.
    with HISTORY_LOCK:
        HISTORY.append({"role": "user", "text": question})
        HISTORY.append({"role": "assistant", "text": answer})
        del HISTORY[:-40]
    return 200, answer


class Handler(BaseHTTPRequestHandler):
    def _send(self, body, ctype="text/plain; charset=utf-8", status=200):
        raw = body if isinstance(body, bytes) else body.encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the client went away, for example a closed browser tab

    def _file(self, name, ctype):
        try:
            with open(os.path.join(APP_DIR, name), "rb") as fh:
                self._send(fh.read(), ctype)
        except OSError:
            self.send_error(404)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path == "/":
            return self._file("index.html", "text/html; charset=utf-8")
        if url.path == "/tailwind.js":
            return self._file("tailwind.js", "text/javascript; charset=utf-8")
        if url.path == "/wendy-logo.svg":
            return self._file("wendy-logo.svg", "image/svg+xml")
        if url.path == "/report.txt":
            return self._file("report.txt", "text/plain; charset=utf-8")
        if url.path == "/history":
            with HISTORY_LOCK:
                return self._send(json.dumps(HISTORY), "application/json")
        if url.path == "/reset":
            with HISTORY_LOCK:
                HISTORY.clear()
            return self._send('{"ok":true}', "application/json")
        if url.path == "/status":
            return self._send(get_state()["state"])
        if url.path == "/progress":
            state = get_state()
            state.pop("chip", None)
            return self._send(json.dumps(state), "application/json")
        if url.path == "/ask":
            state = get_state()["state"]
            if STOPPING.is_set():
                return self._send("the app is stopping", status=503)
            if state != "ready":
                return self._send("not ready: " + state, status=503)
            question = urllib.parse.parse_qs(url.query).get("q", [""])[0].strip()
            if not question:
                return self._send("usage: /ask?q=your+question", status=400)
            turn_opened()
            try:
                status, text = ask(question)
                return self._send(text, status=status)
            finally:
                turn_closed()
        self.send_error(404)

    def log_message(self, fmt, *args):  # quieter than the default access log
        pass


# ---- stopping ---------------------------------------------------------------------

def turn_opened():
    global _turns_open
    with TURNS_CHANGED:
        _turns_open += 1


def turn_closed():
    global _turns_open
    with TURNS_CHANGED:
        _turns_open -= 1
        TURNS_CHANGED.notify_all()


def drain():
    """Wait until nothing is using the DSP and any answer is sent, so the process can exit.

    PID 1 exiting kills everything left in the container, so this waits as long as a
    child runs. A capped turn ends inside the Agent's window. One that does not makes the
    Agent report a failed stop, which beats wedging the DSP, and the app exits when the
    turn ends.
    """
    with CHILD_LOCK:
        child = _child
    if child is not None:
        print("stopping: waiting for the DSP process to finish", flush=True)
        child.wait()
    # Exiting ends the handler threads, which would lose the answer just generated. The
    # limit stops a stalled client from holding up the exit.
    with TURNS_CHANGED:
        TURNS_CHANGED.wait_for(lambda: _turns_open == 0, timeout=DELIVERY_SECONDS)


def main():
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: STOPPING.set())
    # Serve before doing any work: the first run downloads 2.5 GB, and a page that is
    # unreachable for minutes is indistinguishable from a broken one.
    server = ThreadingHTTPServer(("", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print("chat on :%d  (/ for the page, /ask?q=... for plain text)" % PORT, flush=True)
    threading.Thread(target=prepare, daemon=True).start()
    STOPPING.wait()
    drain()
    print("stopped", flush=True)


if __name__ == "__main__":
    main()
