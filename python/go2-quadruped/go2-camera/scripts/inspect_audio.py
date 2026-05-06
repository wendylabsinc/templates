"""Inspect what unitree_webrtc_connect exposes — focused on audio.

Run inside the go2-camera container:
    python /tmp/inspect_audio.py

Looks at:
  1. Top-level UnitreeWebRTCConnection public attributes.
  2. The `audio` attribute of an instance (if it exists), and its methods.
  3. Anything in the package whose name mentions audio / volume / peak / play.

The third pass is the most useful one — it surfaces helper classes
(AudioClient, AudioPublisher, etc) that aren't reachable from the
top-level connection but exist inside the package.
"""

import importlib
import inspect
import pkgutil


def main() -> None:
    from unitree_webrtc_connect import UnitreeWebRTCConnection
    import unitree_webrtc_connect as u

    print("=" * 70)
    print("UnitreeWebRTCConnection public attributes")
    print("=" * 70)
    for n in dir(UnitreeWebRTCConnection):
        if n.startswith("_"):
            continue
        attr = getattr(UnitreeWebRTCConnection, n, None)
        kind = type(attr).__name__
        sig = ""
        if inspect.isfunction(attr) or inspect.ismethod(attr):
            try:
                sig = str(inspect.signature(attr))
            except (TypeError, ValueError):
                sig = "(?)"
        print(f"  {n:<32s} {kind:<14s} {sig}")
    print()

    print("=" * 70)
    print("Symbols anywhere in the package matching audio/volume/peak/play")
    print("=" * 70)
    keywords = ("udio", "olum", "peak", "play", "speak")
    for info in pkgutil.walk_packages(u.__path__, u.__name__ + "."):
        try:
            mod = importlib.import_module(info.name)
        except Exception as e:
            print(f"  (skip {info.name}: {e})")
            continue
        hits = [n for n in dir(mod) if any(k in n.lower() for k in keywords)]
        if hits:
            print(f"  {info.name}: {hits}")
    print()

    print("=" * 70)
    print("UnitreeWebRTCConnection instance attrs (uninstantiated; class-level)")
    print("=" * 70)
    # We can't instantiate without a real connection; inspect class body.
    for n, v in inspect.getmembers(UnitreeWebRTCConnection):
        if n.startswith("_"):
            continue
        print(f"  {n}: {type(v).__name__}")


if __name__ == "__main__":
    main()
