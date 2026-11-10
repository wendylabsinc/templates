"""Optional leased G1 walking; one worker owns all locomotion RPCs.

This module never changes the native FSM or claims arm/leg motor control.
The commander supplies its existing ownership, ARMED and feedback gates.
Fresh neutral input is required after every loss of authority or input.
"""
import math
import threading
import time

MAX_FORWARD_M_S = .25
MAX_YAW_RAD_S = .4
INPUT_TTL_S = .15
LEASE_S = .2


def validate_intent(value):
    if not isinstance(value, dict) or type(value.get("active")) is not bool:
        raise ValueError("locomotion requires active, vx and yaw")
    for key, limit in (("vx", MAX_FORWARD_M_S), ("yaw", MAX_YAW_RAD_S)):
        v = value.get(key)
        if type(v) not in (int, float) or not math.isfinite(v) or abs(v) > limit:
            raise ValueError(f"invalid locomotion {key}")
    return dict(active=value["active"], vx=float(value["vx"]), yaw=float(value["yaw"]))


class Locomotion:
    def __init__(self, client, gate, clock=time.monotonic):
        self.client, self.gate, self.clock = client, gate, clock
        self._lock = threading.Lock()
        self._latest = None
        self._reset_version = 0
        self._seen_reset = 0
        self._epoch = None
        self._neutral = False
        self._moving = False  # possibly moving, including an unacknowledged RPC
        self._closed = threading.Event()
        self._thread = None
        self._status = {"enabled": True, "ready": False, "reason": "center left stick",
                        "requested": None, "rpc_code": None, "fsm": None}

    def submit(self, value):
        try:
            intent = validate_intent(value)
        except ValueError:
            self.reset()
            return False
        with self._lock:
            self._latest = (intent, self.clock())
        return True

    def reset(self):
        # Nonblocking ingress: a slow SDK RPC must not block the arm event loop.
        with self._lock:
            self._latest = None
            self._reset_version += 1

    def status(self):
        return dict(self._status)

    def _stop(self):
        self._neutral = False
        self._status = dict(self._status, ready=False)
        if self._moving:
            # Keep retrying a failed stop. The native lease is also finite.
            code = self.client.SetVelocity(0., 0., 0., LEASE_S)
            self._status = dict(self._status, rpc_code=code, requested=[0., 0., 0.])
            if code == 0:
                self._moving = False

    def tick(self):
        with self._lock:
            latest, reset = self._latest, self._reset_version
        allowed, epoch = self.gate()
        changed = epoch != self._epoch or reset != self._seen_reset
        self._epoch, self._seen_reset = epoch, reset
        if changed:
            self._stop()
        fresh = latest is not None and 0 <= self.clock() - latest[1] <= INPUT_TTL_S
        active = fresh and latest[0]["active"]
        if not allowed or not active:
            self._status = dict(self._status, ready=False,
                                reason="not armed/ready" if not allowed else "input lost")
            self._stop()
            return
        intent = latest[0]
        if self._moving and not self._neutral:
            self._stop()  # retry a failed stop even if the stick remains deflected
        moving = bool(intent["vx"] or intent["yaw"])
        if not moving:
            self._stop()
            self._neutral = not self._moving
            self._status = dict(self._status, ready=self._neutral,
                                reason="centered" if self._neutral else "stop RPC failed")
            return
        if changed or not self._neutral:
            self._status = dict(self._status, ready=False, reason="center left stick")
            return
        code, fsm = self.client.GetFsmId()
        self._status = dict(self._status, fsm=fsm)
        if code != 0 or fsm != 801:
            self._stop()
            self._status = dict(self._status, ready=False, reason="native FSM is not running")
            return
        # The FSM query can block: recheck liveness/authority before a move.
        with self._lock:
            latest = self._latest
            reset_unchanged = self._reset_version == reset
        if (not reset_unchanged or self.gate() != (True, epoch)
                or latest is None or self.clock() - latest[1] > INPUT_TTL_S
                or not latest[0]["active"]):
            self._stop()
            return
        intent = latest[0]  # controller may have updated during the RPC query
        if not (intent["vx"] or intent["yaw"]):
            self._stop()
            return
        velocity = [intent["vx"], 0., intent["yaw"]]
        self._moving = True
        code = self.client.SetVelocity(*velocity, LEASE_S)
        self._status = dict(self._status, ready=code == 0, rpc_code=code,
                            requested=velocity, reason="command accepted" if code == 0 else "RPC failed")
        if code != 0:
            self._stop()

    def start(self):
        def run():
            while not self._closed.wait(.05):
                try:
                    self.tick()
                except Exception as exc:
                    self._neutral = False
                    self.reset()
                    self._status = dict(self._status, ready=False, reason=f"RPC error: {exc}")
                    # _moving stays true, so the next tick retries the stop.
            try:
                self._stop()
            except Exception:
                pass  # native lease bounds a failed shutdown stop
        self._thread = threading.Thread(target=run, daemon=True, name="g1-locomotion")
        self._thread.start()

    def close(self):
        self.reset()
        self._closed.set()
        if self._thread is not None:
            self._thread.join(timeout=1.)
