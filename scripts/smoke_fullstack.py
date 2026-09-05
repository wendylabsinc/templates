"""Build a generated Web app and verify its page, API, and built assets."""

import argparse
import json
import http.client
import pathlib
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]


def command(*args):
    return subprocess.check_output(args, text=True).strip()


def render(language, destination):
    prefix = f"{language}/fullstack/"
    names = command("git", "-C", str(ROOT), "ls-files", "--cached", "--others", "--exclude-standard").splitlines()
    count = 0
    for name in sorted(set(names)):
        source = ROOT / name
        if not name.startswith(prefix) or not source.is_file() or source.name == "template.json":
            continue
        relative = name[len(prefix):].replace("Sources/fullstack/", "Sources/starter-smoke/")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        data = source.read_bytes().replace(b"{{.APP_ID}}", b"starter-smoke").replace(b"{{.PORT}}", b"3001")
        if re.search(rb"{{\.[A-Z_]+}}", data):
            raise ValueError(f"Unresolved template variable in {name}")
        target.write_bytes(data)
        count += 1
    return count


def smoke(language, skip_build=False):
    image = f"wendy-starter-smoke-{language}"
    with tempfile.TemporaryDirectory(prefix="wendy-starter-") as temp:
        start = time.monotonic()
        count = render(language, pathlib.Path(temp))
        rendered = time.monotonic() - start
        start = time.monotonic()
        if not skip_build:
            subprocess.run(["docker", "build", "-t", image, temp], check=True)
        built = time.monotonic() - start
        start = time.monotonic()
        container = command("docker", "run", "--rm", "-d", "-p", "127.0.0.1::3001", image)
        try:
            port = command("docker", "port", container, "3001/tcp").rsplit(":", 1)[1]
            base = f"http://127.0.0.1:{port}"
            for attempt in range(60):
                try:
                    with urllib.request.urlopen(base + "/health", timeout=2) as response:
                        assert json.load(response) == {"status": "ok"}
                    break
                except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException):
                    if attempt == 59:
                        raise
                    time.sleep(0.5)
            ready = time.monotonic() - start
            with urllib.request.urlopen(base + "/api/hello", timeout=5) as response:
                assert json.load(response) == {"message": "Hello from Wendy!"}
            with urllib.request.urlopen(base, timeout=5) as response:
                html = response.read().decode()
                assert '<div id="root"></div>' in html
            assets = re.findall(r'(?:src|href)="(/assets/[^\"]+)"', html)
            assert any(asset.endswith(".js") for asset in assets), html
            assert any(asset.endswith(".css") for asset in assets), html
            for asset in assets:
                with urllib.request.urlopen(base + asset, timeout=5) as response:
                    assert len(response.read()) > 0
                    assert "text/html" not in response.headers["Content-Type"]
            result = {"language": language, "files": count, "render_seconds": round(rendered, 3), "build_seconds": round(built, 3), "ready_seconds": round(ready, 3), "image_bytes": int(command("docker", "image", "inspect", image, "--format", "{{.Size}}"))}
            print(json.dumps(result))
            return result
        except Exception:
            subprocess.run(["docker", "logs", container], check=False)
            raise
        finally:
            subprocess.run(["docker", "stop", container], check=False, stdout=subprocess.DEVNULL)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--language", choices=["python", "node", "swift", "rust", "cpp", "mojo"], required=True)
    parser.add_argument("--skip-build", action="store_true", help="Test a previously built smoke image")
    args = parser.parse_args()
    smoke(args.language, args.skip_build)
