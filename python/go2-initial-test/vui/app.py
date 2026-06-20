"""VUI test — probe the Go2's head light brightness + speaker volume.

The standard Go2 has no addressable RGB *body* LEDs; what's controllable is the
VUI (head light brightness + volume) via SDK2's VuiClient. This fills the
body_leds tile with a real probe instead of `na`.

MANUAL: the dashboard button runs it. The VuiClient API names (Get/SetBrightness,
Get/SetVolume) are best-guess from the SDK and may differ by firmware — the
detail surfaces whatever actually worked. Adapted pattern from the SDK clients.
"""
import asyncio
import os
import socket

import uvicorn
from fastapi import FastAPI

PORT = int(os.environ.get("PORT", "3620"))
IFACE = os.environ.get("GO2_NETWORK_INTERFACE", "eth0")
GO2_IP = os.environ.get("GO2_IP", "192.168.123.161")


def _resolve_dds_address(robot_ip):
    """Local IP this host uses to reach the Go2 — the address CycloneDDS must bind
    to (the Orin is multi-homed). GO2_DDS_ADDRESS overrides; otherwise ask the
    kernel which source IP routes to the robot (no packets sent, never blocks).
    Returns "" off-robot (no route)."""
    override = os.environ.get("GO2_DDS_ADDRESS", "").strip()
    if override:
        return override
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((robot_ip, 1))  # no traffic; the kernel just picks the route
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return ""


DDS_ADDR = _resolve_dds_address(GO2_IP)
# Read-only by default; set VUI_WRITE=1 to also bump+restore brightness.
VUI_WRITE = os.environ.get("VUI_WRITE", "").lower() in ("1", "true", "yes")

app = FastAPI(title="go2-test-vui")
_client = None
_factory_initialized = False
_result = {"interface": "body_leds", "status": "manual",
           "detail": "press “Run test” to probe the Go2 VUI (head light brightness + volume)",
           "data": {}}


def _probe():
    global _client, _factory_initialized
    try:
        from unitree_sdk2py.core.channel import ChannelFactoryInitialize
        from unitree_sdk2py.go2.vui.vui_client import VuiClient
        if not _factory_initialized:
            if DDS_ADDR:
                os.environ["CYCLONEDDS_URI"] = (
                    "<CycloneDDS><Domain><General><Interfaces>"
                    f'<NetworkInterface address="{DDS_ADDR}"/>'
                    "</Interfaces></General></Domain></CycloneDDS>"
                )
                ChannelFactoryInitialize(0)
            else:
                ChannelFactoryInitialize(0, IFACE)
            _factory_initialized = True
        if _client is None:
            c = VuiClient()
            c.SetTimeout(3.0)
            c.Init()
            _client = c
        info = {}
        try:
            _, b = _client.GetBrightness()
            info["brightness"] = b
            if VUI_WRITE:
                # Opt-in: bump then restore, to prove it's writable.
                _client.SetBrightness(min(10, (b or 0) + 1))
                _client.SetBrightness(b or 0)
                info["write"] = "ok"
        except Exception as e:  # noqa: BLE001
            info["brightness_err"] = str(e)
        try:
            _, v = _client.GetVolume()
            info["volume"] = v
        except Exception as e:  # noqa: BLE001
            info["volume_err"] = str(e)
        ok = "brightness" in info or "volume" in info
        if ok:
            shown = ", ".join(f"{k}={v}" for k, v in info.items() if not k.endswith("_err"))
            return {"interface": "body_leds", "status": "pass",
                    "detail": f"VUI reachable · {shown} (head light/volume; no body-RGB on standard Go2)",
                    "data": info}
        return {"interface": "body_leds", "status": "fail",
                "detail": "VuiClient init OK but brightness/volume reads failed — verify SDK API names",
                "data": info}
    except Exception as e:  # noqa: BLE001
        return {"interface": "body_leds", "status": "fail",
                "detail": f"VuiClient unavailable: {e} (no known body-RGB API on standard Go2)", "data": {}}


def _run():
    global _result
    _result = _probe()


@app.get("/status")
def status():
    return {"results": [_result]}


@app.post("/run")
async def rerun():
    await asyncio.to_thread(_run)
    return {"ok": _result["status"] == "pass", "result": _result}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
