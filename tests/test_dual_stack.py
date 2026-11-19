"""The real template entrypoints must accept USB IPv6 and LAN IPv4 clients."""
import ast
import pathlib
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

@pytest.mark.parametrize('template', ['simple-api', 'camera-feed'])
def test_python_entrypoint_accepts_both_address_families(template, tmp_path):
    if not socket.has_dualstack_ipv6():
        pytest.skip('dual-stack IPv6 unavailable on test host')
    with socket.socket(socket.AF_INET6) as sock:
        sock.bind(('::1', 0))
        port = sock.getsockname()[1]
    source = (ROOT / 'python' / template / 'app.py').read_text().replace('{{.PORT}}', str(port))
    main = next(node for node in ast.parse(source).body if isinstance(node, ast.If) and '__name__' in ast.unparse(node.test))
    # Run the shipped entrypoint without requiring camera hardware/GStreamer.
    program = 'from fastapi import FastAPI\napp = FastAPI()\n' + ast.unparse(main)
    script = tmp_path / 'server.py'
    script.write_text(program)
    child = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        for host in ['[::1]', '127.0.0.1']:
            deadline = time.monotonic() + 10
            while True:
                try:
                    opener.open(f'http://{host}:{port}/openapi.json', timeout=.5).close()
                    break
                except OSError:
                    if child.poll() is not None or time.monotonic() >= deadline:
                        raise AssertionError(f'{host} unavailable; child status {child.poll()}')
                    time.sleep(.05)
    finally:
        child.terminate()
        child.communicate(timeout=5)
