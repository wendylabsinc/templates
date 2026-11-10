"""teleop.js must survive being IMPORTED, not merely parsed.

The other JS tests import one function from one module and exercise it. That
cannot see a module whose top level throws -- and on 2026-09-16 one did:
`const GHOST = ... HUD_SCALE)` at line 53 read a `const HUD_SCALE` declared
300 lines below. `const` is not hoisted, so the module raised

    ReferenceError: Cannot access 'HUD_SCALE' before initialization

at load and teleop.js never ran a line. The page kept its static HTML: the
Enter button on its initial "Checking WebXR..." label and the status on
"idle". That reads exactly like a headset or certificate fault, and it was
chased as one -- through a browser restart, a port, and a protocol -- before
anyone loaded the module and read the error.

Syntax checking would not have caught it either: the file parses. Only
evaluating the module does.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent.parent / "teleop" / "web"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node not installed")

# Enough of a browser for module-level code. Deliberately minimal: anything a
# module needs BEYOND this at load time is itself worth knowing about.
STUB = """
const el = () => ({ style:{}, classList:{add(){},remove(){}}, addEventListener(){},
                    appendChild(){}, getContext:()=>null, textContent:"", innerHTML:"",
                    width:0, height:0, disabled:true, hidden:false });
globalThis.document = { getElementById: el, createElement: el, body: el(),
                        addEventListener(){}, querySelectorAll:()=>[] };
globalThis.window = globalThis;
globalThis.location = { search: %s, href: "http://localhost:8200/", protocol: "http:" };
Object.defineProperty(globalThis, "navigator", {
  value: { xr: { isSessionSupported: async () => false }, userAgent: "node" },
  configurable: true });
globalThis.performance = { now: () => 0 };
globalThis.WebSocket = class { constructor(){} send(){} close(){} };
globalThis.requestAnimationFrame = () => 0;
globalThis.fetch = async () => ({ ok: false, status: 404 });
try {
  await import(%s);
  console.log("OK");
} catch (e) {
  console.log("THREW " + e.constructor.name + ": " + e.message);
}
"""


def _load(module, search='"?"'):
    url = json.dumps((WEB / module).as_uri())
    script = textwrap.dedent(STUB % (search, url))
    r = subprocess.run([NODE, "--input-type=module", "-e", script],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"node failed: {r.stderr}"
    return r.stdout.strip()


@needs_node
@pytest.mark.parametrize("module", ["teleop.js", "robot_ghost.js", "feedback.js"])
def test_the_module_evaluates_without_throwing(module):
    out = _load(module)
    assert out.endswith("OK"), f"{module} threw at import: {out}"


@needs_node
@pytest.mark.parametrize("search", [
    '"?"',                      # the plain page
    '"?input=controllers"',     # what the operator actually opens
    '"?hudscale=0.8&ghostviews=1"',
    '"?ghost=0&haptics=0"',
    '"?hudy=-2&huddist=9&hudscale=99"',   # every knob past its clamp
])
def test_teleop_evaluates_for_the_urls_the_operator_uses(search):
    """A knob is only useful if the page still loads with it set."""
    out = _load("teleop.js", search)
    assert out.endswith("OK"), f"teleop.js threw with {search}: {out}"
