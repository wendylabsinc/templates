#!/usr/bin/python3
"""Supervise app-local MAX and Open WebUI on Apple Silicon."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

MAX_VERSION = "26.5.0"
WEBUI_VERSION = "0.9.5"
DEFAULT_MODEL = "HuggingFaceTB/SmolLM2-135M-Instruct"
MAX_PORT = 11435


class Supervisor:
    def __init__(self):
        self.stopping = threading.Event()
        self.children = []
        self.readers = []

    def spawn(self, name, args, env, cwd):
        print(f"[{name}] starting", flush=True)
        child = subprocess.Popen(
            args, cwd=cwd, env=env, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
            start_new_session=True,
        )
        self.children.append(child)

        def forward():
            with child.stdout:
                for line in child.stdout:
                    print(f"[{name}] {line}", end="", flush=True)

        reader = threading.Thread(target=forward, daemon=True)
        reader.start()
        self.readers.append(reader)
        return child

    def install(self, args, env, cwd):
        child = self.spawn("setup", args, env, cwd)
        while child.poll() is None:
            if self.stopping.wait(0.1):
                raise InterruptedError("shutdown during setup")
        if child.returncode:
            raise RuntimeError(f"setup exited with status {child.returncode}")
        self.children.remove(child)

    def stop(self):
        self.stopping.set()
        # Each child owns a process group, including MAX's worker processes.
        # Signal both services before waiting for either of them.
        for child in self.children:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 3
        for child in self.children:
            try:
                child.wait(timeout=max(0.01, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                pass
        for child in self.children:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()
        for reader in self.readers:
            reader.join(timeout=1)


def runtime_environment(root):
    env = dict(os.environ)
    for key, relative in {
        "UV_CACHE_DIR": "cache/uv", "UV_PYTHON_INSTALL_DIR": "python",
        "HF_HOME": "models/huggingface", "XDG_CACHE_HOME": "cache",
        "DATA_DIR": "open-webui",
    }.items():
        directory = root / relative
        directory.mkdir(parents=True, exist_ok=True)
        env[key] = str(directory)
    env["PYTHONUNBUFFERED"] = "1"
    # MAX 26.5 exposes the bind host through settings, not a --host flag.
    env["MAX_SERVE_HOST"] = "127.0.0.1"
    env["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
    # Explicit local cache locations also override inherited per-cache paths.
    env["HF_HUB_CACHE"] = str(root / "models/huggingface/hub")
    env["SENTENCE_TRANSFORMERS_HOME"] = str(root / "models/sentence-transformers")
    return env


def ensure_environment(supervisor, uv, root, name, python, package, env):
    directory = root / "envs" / name
    stamp = directory / ".wendy-installed.json"
    expected = {"python": python, "package": package}
    try:
        if json.loads(stamp.read_text()) == expected and (directory / "bin/python").exists():
            print(f"[setup] reusing {name} environment", flush=True)
            return directory
    except (OSError, ValueError):
        pass
    directory.parent.mkdir(parents=True, exist_ok=True)
    if not (directory / "bin/python").exists():
        supervisor.install([uv, "venv", "--managed-python", "--python", python, str(directory)], env, root)
    supervisor.install([uv, "pip", "install", "--python", str(directory / "bin/python"), package], env, root)
    stamp.write_text(json.dumps(expected))
    return directory


def wait_for_max(supervisor, child, url):
    # The CLI owns the readiness budget. Keep reporting startup through logs
    # while model downloads/compilation continue beyond its initial deadline.
    report_at = time.monotonic() + 30
    while not supervisor.stopping.is_set():
        if child.poll() is not None:
            raise RuntimeError(f"MAX exited before becoming healthy (status {child.returncode})")
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            pass
        if time.monotonic() >= report_at:
            print("[max] still loading the model; waiting for /health", flush=True)
            report_at = time.monotonic() + 30
        supervisor.stopping.wait(1)
    raise InterruptedError("shutdown during model startup")


def main():
    app_id = os.environ.get("WENDY_APP_ID", "{{.APP_ID}}")
    if not app_id or app_id in (".", "..") or "/" in app_id or "\\" in app_id:
        raise ValueError("WENDY_APP_ID must be a directory name")
    root = Path.home() / "Library/Application Support" / app_id / "runtime"
    root.mkdir(parents=True, exist_ok=True)
    # Retain the lock handle for the launcher's lifetime.
    with (root / "launcher.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        supervisor = Supervisor()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: supervisor.stopping.set())
        try:
            uv = shutil.which("uv") or next((str(p) for p in (Path("/opt/homebrew/bin/uv"), Path("/usr/local/bin/uv")) if p.is_file()), None)
            if not uv:
                raise RuntimeError("uv is missing; deploy with Brewfile.wendy to install it on the target")
            env = runtime_environment(root)
            max_env = ensure_environment(supervisor, uv, root, "max-26.5.0-py314", "3.14", f"modular=={MAX_VERSION}", env)
            webui_env = ensure_environment(supervisor, uv, root, "webui-0.9.5-py311", "3.11", f"open-webui=={WEBUI_VERSION}", env)
            model = os.environ.get("MAX_MODEL", "{{.MAX_MODEL}}") or DEFAULT_MODEL
            started = time.monotonic()
            max_child = supervisor.spawn("max", [
                str(max_env / "bin/max"), "serve", "--model", model,
                "--devices=gpu", "--port", str(MAX_PORT),
                "--max-batch-size", "1", "--max-length", "2048",
                "--device-memory-utilization", "0.2",
            ], env, root)
            wait_for_max(supervisor, max_child, f"http://127.0.0.1:{MAX_PORT}/health")
            print(f"[max] healthy after {time.monotonic() - started:.1f}s; starting browser chat", flush=True)
            secret = root / "webui-secret"
            if not secret.exists():
                descriptor = os.open(secret, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "w") as output:
                    output.write(secrets.token_urlsafe(32))
            webui_vars = dict(env, WEBUI_SECRET_KEY=secret.read_text(),
                OPENAI_API_BASE_URL=f"http://127.0.0.1:{MAX_PORT}/v1",
                OPENAI_API_BASE_URLS=f"http://127.0.0.1:{MAX_PORT}/v1",
                OPENAI_API_KEY="local-max", OPENAI_API_KEYS="local-max",
                ENABLE_OLLAMA_API="False", ENABLE_OPENAI_API="True",
                ENABLE_PERSISTENT_CONFIG="False", WEBUI_AUTH="True",
            )
            webui_child = supervisor.spawn("webui", [str(webui_env / "bin/open-webui"), "serve", "--host", "0.0.0.0", "--port", "{{.PORT}}"], webui_vars, root)
            while not supervisor.stopping.wait(0.2):
                for name, child in (("MAX", max_child), ("Open WebUI", webui_child)):
                    if child.poll() is not None:
                        raise RuntimeError(f"{name} exited (status {child.returncode}); stopping the app")
            return 0
        except InterruptedError:
            return 0
        finally:
            supervisor.stop()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(f"[launcher] {error}", file=sys.stderr, flush=True)
        sys.exit(1)
