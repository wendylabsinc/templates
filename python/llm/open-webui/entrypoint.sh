#!/usr/bin/env bash
set -euo pipefail

mkdir -p "$DATA_DIR"

export WEBUI_AUTH="${WEBUI_AUTH:-False}"
export ENABLE_PERSISTENT_CONFIG="${ENABLE_PERSISTENT_CONFIG:-False}"
export WEBUI_NAME="${WEBUI_NAME:-Wendy}"

# On a Wendy device each compose service runs in its own network namespace,
# so the Docker service-name URL from docker-compose.yml does not resolve.
# The ollama service publishes 11434 on the device host, so point at it via
# the agent-injected device hostname instead (see the README).
if [[ -n "${WENDY_DEVICE_HOSTNAME:-}" && "${OLLAMA_BASE_URL:-}" == "http://ollama:11434" ]]; then
  export OLLAMA_BASE_URL="http://${WENDY_DEVICE_HOSTNAME}:11434"
  echo "Wendy device detected; OLLAMA_BASE_URL=${OLLAMA_BASE_URL}"
fi

BRAND_PYTHON="/root/.local/share/uv/tools/open-webui/bin/python"
if [[ ! -x "$BRAND_PYTHON" ]]; then
  BRAND_PYTHON="python3"
fi

"$BRAND_PYTHON" <<'PY'
from pathlib import Path
import importlib.util
import re
import shutil
import sys

brand = Path("/opt/wendy-brand")
spec = importlib.util.find_spec("open_webui")
custom_css = """
/* wendy-llm-branding-v1 */
img[src$="/static/favicon.svg"],
img[src$="/static/favicon.png"],
img[src$="/static/favicon-96x96.png"],
img[src*="favicon.svg"],
img[src*="favicon.png"],
img[src*="logo.svg"],
img[src*="logo.png"],
img[src*="/api/v1/models/model/profile/image"] {
  content: url("/static/favicon.svg") !important;
  filter: none !important;
}

html.dark img[src$="/static/favicon.png"],
html.dark img[src$="/static/favicon-96x96.png"],
html.dark img[src*="favicon.svg"],
html.dark img[src*="favicon.png"],
html.dark img[src*="logo.svg"],
html.dark img[src*="logo.png"],
html.dark img[src*="/api/v1/models/model/profile/image"],
html.oled-dark img[src$="/static/favicon.png"],
html.oled-dark img[src$="/static/favicon-96x96.png"],
html.oled-dark img[src*="favicon.svg"],
html.oled-dark img[src*="favicon.png"],
html.oled-dark img[src*="logo.svg"],
html.oled-dark img[src*="logo.png"],
html.oled-dark img[src*="/api/v1/models/model/profile/image"] {
  filter: invert(1) !important;
}
"""

if brand.exists():
    search_roots = {
        Path(sys.prefix),
        Path(sys.prefix) / "lib",
        Path("/root/.local/share/uv/tools/open-webui"),
    }
    if spec and spec.origin:
        package_root = Path(spec.origin).parent
        search_roots.update({package_root, package_root.parent})

    replacements = {
        "favicon.ico": "favicon.ico",
        "favicon.png": "favicon.png",
        "favicon-96x96.png": "favicon-96x96.png",
        "favicon.svg": "wendy-emblem.svg",
        "logo.svg": "wendy-emblem.svg",
        "favicon-16x16.png": "favicon-16x16.png",
        "favicon-32x32.png": "favicon-32x32.png",
        "apple-touch-icon.png": "apple-touch-icon.png",
        "android-chrome-192x192.png": "android-chrome-192x192.png",
        "android-chrome-512x512.png": "android-chrome-512x512.png",
    }

    for root in search_roots:
        if not root.exists():
            continue
        for target in root.rglob("*"):
            source_name = replacements.get(target.name.lower())
            if not source_name and target.name.lower() in {"logo.png", "favicon.png"}:
                source_name = "android-chrome-192x192.png"
            if source_name:
                source = brand / source_name
                if source.exists():
                    shutil.copyfile(source, target)

        for target in root.rglob("custom.css"):
            existing = target.read_text(errors="ignore") if target.exists() else ""
            existing = re.sub(
                r"\n*/\* wendy-llm-branding-v\d+ \*/\n.*?filter: invert\(1\) !important;\n}\n?",
                "\n",
                existing,
                flags=re.S,
            ).rstrip()
            target.write_text(f"{existing}\n\n{custom_css.strip()}\n")
PY

exec /root/.local/bin/open-webui serve
