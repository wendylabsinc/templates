"""Exercise native launcher setup persistence and child ownership without models."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "mojo/mac-llm/launcher.py"
spec = importlib.util.spec_from_file_location("mac_llm_launcher", SOURCE)
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


def test_runtime_caches_are_app_local(tmp_path):
    env = launcher.runtime_environment(tmp_path)
    assert env["MAX_SERVE_HOST"] == "127.0.0.1"
    for key in ("UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR", "HF_HOME", "HF_HUB_CACHE", "XDG_CACHE_HOME", "DATA_DIR"):
        assert Path(env[key]).is_relative_to(tmp_path)


def test_warm_environment_reuses_successful_install(tmp_path):
    directory = tmp_path / "envs/max"
    (directory / "bin").mkdir(parents=True)
    (directory / "bin/python").touch()
    (directory / ".wendy-installed.json").write_text(json.dumps({"python": "3.14", "package": "modular==26.5.0"}))

    class NeverInstall:
        def install(self, *args):
            pytest.fail("warm startup reinstalled dependencies")

    assert launcher.ensure_environment(NeverInstall(), "uv", tmp_path, "max", "3.14", "modular==26.5.0", {}) == directory


@pytest.mark.parametrize("failed_service", ["max", "webui"])
def test_either_child_exit_fails_app_and_cleans_up_sibling(tmp_path, monkeypatch, failed_service):
    monkeypatch.setenv("WENDY_APP_ID", "chat-test")
    monkeypatch.setattr(launcher.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(launcher.shutil, "which", lambda _: "/unused/uv")
    monkeypatch.setattr(launcher.signal, "signal", lambda *_: None)
    children = []
    original_spawn = launcher.Supervisor.spawn

    def spawn(self, name, args, env, cwd):
        # Simulate MAX worker and WebUI server without package installation.
        child = original_spawn(self, name, [sys.executable, "-c", "import time; time.sleep(0.3)" if name == failed_service else "import time; time.sleep(60)"], env, cwd)
        children.append(child)
        return child

    monkeypatch.setattr(launcher.Supervisor, "spawn", spawn)
    monkeypatch.setattr(launcher, "ensure_environment", lambda *args: tmp_path)
    monkeypatch.setattr(launcher, "wait_for_max", lambda *_: None)
    with pytest.raises(RuntimeError, match="exited"):
        launcher.main()
    assert len(children) == 2
    assert all(child.poll() is not None for child in children)


def test_shutdown_terminates_child_process_group(tmp_path):
    supervisor = launcher.Supervisor()
    child_pid = tmp_path / "child-pid"
    code = "import subprocess, sys, time; from pathlib import Path; p=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(60)"
    child = supervisor.spawn("worker", [sys.executable, "-c", code, str(child_pid)], os.environ, tmp_path)
    try:
        deadline = time.monotonic() + 5
        while not child_pid.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert child_pid.exists()
        pid = int(child_pid.read_text())
    finally:
        supervisor.stop()
    assert child.poll() is not None
    # A reparented child can briefly remain a zombie; it must not be running.
    status = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    assert not status or status.startswith("Z")
