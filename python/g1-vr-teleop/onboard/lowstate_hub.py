"""One reader of rt/lowstate for the whole onboard process, newest sample only.

The robot publishes its state at up to ~1 kHz, and every sample a Python
reader takes is decoded in Python while holding the GIL. The onboard used to
run TWO such readers of the same topic:

  - the Commander's listener (`ChannelSubscriber.Init(on_lowstate, 10)`),
    which decodes EVERY published sample in the DDS listener, queues it, and
    then threw 9 in 10 away (LOWSTATE_DECIMATE);
  - the arm controller's own subscribe thread in g1_arm.py, polling every
    2 ms.

On 2026-09-19 that cost showed up as the operator's "motors fighting": the
50 Hz arm publisher could not get the interpreter (loop 43.5 Hz, CRC 21.7 ms
where 0.7 ms is normal), measured state reached the Commander in bursts with
110-145 ms gaps, the arm held its last command for up to ~0.6 s and then
lurched, and the tracking leash fired on that stale measurement.

This takes the newest sample at a fixed rate instead -- a plain DataReader
keeps only the latest sample (KEEP_LAST 1), so a poll decodes exactly one
message however fast the robot publishes -- and hands the same object to
every consumer: the Commander (liveness, remote, measured joints, leash) and
the arm controller (its velocity clip and startup pose). Decoding cost is
now `hz` samples a second, once, instead of every sample twice.
"""
from __future__ import annotations

import os
import threading
import time

from hotpath import DeadlineLoop, pin_current_thread

POLL_HZ_DEFAULT = 200.0
POLL_HZ_MIN, POLL_HZ_MAX = 20.0, 1000.0


def poll_hz_from_env(env=None):
    """G1_LOWSTATE_HZ, default 200. A bad value refuses to start."""
    env = os.environ if env is None else env
    raw = str(env.get("G1_LOWSTATE_HZ", "")).strip()
    if not raw:
        return POLL_HZ_DEFAULT
    try:
        hz = float(raw)
    except ValueError:
        raise ValueError(f"G1_LOWSTATE_HZ={raw!r}: expected a number") from None
    if not POLL_HZ_MIN <= hz <= POLL_HZ_MAX:
        raise ValueError(f"G1_LOWSTATE_HZ={raw!r}: expected {POLL_HZ_MIN:g}..{POLL_HZ_MAX:g}")
    return hz


class LowstateHub:
    """Poll `read()` at `hz`, keep the newest sample, call every listener.

    `read` is `ChannelSubscriber.Read` on a subscriber initialised WITHOUT a
    handler: it blocks until a sample exists and returns the newest one.
    Listeners run in the hub's thread, in the order they were added; one that
    raises is reported and does not stop the others or the hub.
    """

    def __init__(self, read, hz=POLL_HZ_DEFAULT, clock=time.monotonic):
        self._read = read
        self.hz = float(hz)
        self.period = 1.0 / self.hz
        self._clock = clock
        self._lock = threading.Lock()
        self._msg = None
        self._seq = 0
        self._t = None
        self._listeners = []
        self._stop = False
        self._thread = None
        self.affinity = None
        self.n_samples = 0
        self.n_listener_errors = 0
        self.rate_hz = 0.0
        self._rate_n = 0
        self._rate_t0 = None

    def add_listener(self, fn):
        self._listeners.append(fn)

    def latest(self):
        """-> (seq, msg, t_monotonic). seq is 0 until the first sample."""
        with self._lock:
            return self._seq, self._msg, self._t

    def poll_once(self):
        """Take one sample and publish it. -> True if there was one."""
        msg = self._read()
        if msg is None:
            return False
        now = self._clock()
        with self._lock:
            self._msg, self._t = msg, now
            self._seq += 1
        self.n_samples += 1
        self._rate_n += 1
        if self._rate_t0 is None:
            self._rate_t0 = now
        elif now - self._rate_t0 >= 1.0:
            self.rate_hz = round(self._rate_n / (now - self._rate_t0), 1)
            self._rate_n, self._rate_t0 = 0, now
        for fn in self._listeners:
            try:
                fn(msg)
            except Exception as e:  # noqa: BLE001 -- one bad consumer must not stop the others
                self.n_listener_errors += 1
                if self.n_listener_errors <= 5 or self.n_listener_errors % 1000 == 0:
                    print(f"[lowstate] listener error ({self.n_listener_errors}): {e}")
        return True

    def _run(self):
        # Keep DDS decoding off the motor core. `off` restores the scheduler's
        # normal placement; with one available core both roles necessarily
        # resolve to that core.
        self.affinity = pin_current_thread(
            "G1_LOWSTATE_CPU", default="auto", auto_offset=1)
        cadence = DeadlineLoop(self.period)
        while not self._stop:
            try:
                self.poll_once()
            except Exception as e:  # noqa: BLE001 -- a read error must not end liveness
                print(f"[lowstate] read error: {e}")
            cadence.wait()

    def start(self):
        self._thread = threading.Thread(target=self._run, name="lowstate-hub", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop = True

    def status(self):
        with self._lock:
            age = (self._clock() - self._t) if self._t is not None else None
        return {"poll_hz": self.hz, "rate_hz": self.rate_hz, "samples": self.n_samples,
                "age_s": round(age, 4) if age is not None else None,
                "listener_errors": self.n_listener_errors,
                "affinity": self.affinity}
