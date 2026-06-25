"""Bluetooth presence test — is a BT adapter / USB dongle on the Jetson?

Green when a Bluetooth controller is present (a USB dongle plugged into the dog's
Jetson, or an onboard radio); red when none is found. Detection is OS-level via
sysfs (`/sys/class/bluetooth/hci*` and a USB wireless-controller scan), so it
needs no `bluetooth` entitlement and the container always starts.

This validates "the BT hardware is there." Actually pairing/scanning over BlueZ
is a separate, heavier check that needs the bluetooth entitlement's D-Bus proxy.
"""
import glob
import os

import uvicorn
from fastapi import FastAPI

PORT = int(os.environ.get("PORT", "3621"))
app = FastAPI(title="go2-test-bt")


def _read(p):
    try:
        with open(p) as f:
            return f.read().strip()
    except Exception:  # noqa: BLE001
        return ""


def _adapters():
    out = []
    for d in sorted(glob.glob("/sys/class/bluetooth/*")):
        out.append({"name": os.path.basename(d), "address": _read(os.path.join(d, "address"))})
    return out


def _usb_dongles():
    # USB class 0xe0 = wireless controller (Bluetooth radios report this). Many BT
    # dongles are composite: device class 00/ef with e0 only on an INTERFACE
    # descriptor, so also scan */*:*/bInterfaceClass.
    hits = []
    for d in glob.glob("/sys/bus/usb/devices/*"):
        cls = _read(os.path.join(d, "bDeviceClass"))
        prod = _read(os.path.join(d, "product"))
        iface_e0 = any(_read(i) == "e0" for i in glob.glob(os.path.join(d, "*:*/bInterfaceClass")))
        if cls == "e0" or iface_e0 or "bluetooth" in prod.lower():
            vid, pid = _read(os.path.join(d, "idVendor")), _read(os.path.join(d, "idProduct"))
            hits.append({"product": prod or "?", "id": f"{vid}:{pid}"})
    return hits


def _result():
    adapters = _adapters()
    usb = _usb_dongles()
    if adapters:
        a = adapters[0]
        extra = f" · USB {usb[0]['product']} ({usb[0]['id']})" if usb else ""
        return {"interface": "bluetooth", "status": "pass",
                "detail": f"adapter {a['name']} {a['address']}".rstrip() + extra,
                "data": {"adapters": adapters, "usb": usb}}
    if usb:
        return {"interface": "bluetooth", "status": "pass",
                "detail": f"USB BT dongle present: {usb[0]['product']} ({usb[0]['id']}) — "
                          "no hci adapter bound yet (driver/firmware?)",
                "data": {"usb": usb}}
    return {"interface": "bluetooth", "status": "fail",
            "detail": "no Bluetooth adapter found — plug a USB BT dongle into the dog's Jetson",
            "data": {}}


@app.get("/status")
def status():
    return {"results": [_result()]}


@app.post("/run")
def rerun():
    r = _result()
    return {"ok": r["status"] == "pass", "results": [r]}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
