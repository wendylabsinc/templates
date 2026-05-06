"""Dump the WebRTCAudioHub / WebRTCAudioChannel APIs.

Run inside the go2-camera container:
    python /tmp/inspect_audiohub.py
"""
import inspect

from unitree_webrtc_connect import constants as C
from unitree_webrtc_connect.webrtc_audio import (
    AudioStreamTrack,
    WebRTCAudioChannel,
)
from unitree_webrtc_connect.webrtc_audiohub import (
    AUDIO_API,
    WebRTCAudioHub,
)


def dump(name, cls):
    print("=" * 70)
    print(name)
    print("=" * 70)
    for n in dir(cls):
        if n.startswith("_") and n != "__init__":
            continue
        attr = getattr(cls, n, None)
        sig = ""
        if callable(attr):
            try:
                sig = str(inspect.signature(attr))
            except (TypeError, ValueError):
                sig = "(?)"
        kind = type(attr).__name__
        print(f"  {n:<32s} {kind:<14s} {sig}")
        # Print docstring first line if present.
        doc = inspect.getdoc(attr)
        if doc:
            first = doc.splitlines()[0]
            print(f"    └─ {first[:80]}")
    print()


def dump_constants():
    print("=" * 70)
    print("AUDIO_API constants")
    print("=" * 70)
    for n in dir(AUDIO_API):
        if n.startswith("_"):
            continue
        v = getattr(AUDIO_API, n)
        print(f"  {n} = {v!r}")
    print()


if __name__ == "__main__":
    dump("WebRTCAudioHub", WebRTCAudioHub)
    dump("WebRTCAudioChannel", WebRTCAudioChannel)
    dump("AudioStreamTrack", AudioStreamTrack)
    dump_constants()
