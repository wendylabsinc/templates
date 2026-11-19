"""Robot-relative walking intent from the left XR controller.

XR +y is down/back and +x is right. G1 +vx is forward and +yaw is left.
Stick clicks belong to arm/home and the ghost toggle, never walking.
"""
import math

MAX_FORWARD_M_S = 0.25
MAX_YAW_RAD_S = 0.4
DEAD_ZONE = 0.15


class WalkingPermission:
    """Operator permission, reset to blocked whenever the bridge starts."""
    def __init__(self):
        self.allowed = False
        self._needs_center = True

    def set_allowed(self, allowed):
        if self.allowed != allowed or not allowed:
            self.reset()
        self.allowed = allowed

    def reset(self):
        self._needs_center = True

    def status(self):
        return {"allowed": self.allowed,
                "awaiting_center": self.allowed and self._needs_center}

    def filter(self, intent):
        stop = {"active": False, "vx": 0., "yaw": 0.}
        if not self.allowed or not intent["active"]:
            self.reset()
            return stop
        if self._needs_center:
            if intent["vx"] or intent["yaw"]:
                return stop
            self._needs_center = False
        return intent


def left_stick_velocity(frame):
    stopped = {"active": False, "vx": 0., "yaw": 0.}
    hands = frame.get("hands") or []
    if any((h.get("buttons") or {}).get("stick") for h in hands):
        return stopped
    left = next((h for h in hands if h.get("hand") == "left"
                 and h.get("source") == "controller"), None)
    if left is None:
        return stopped
    stick = left.get("thumbstick")
    if not isinstance(stick, (tuple, list)) or len(stick) != 2:
        return stopped
    if not all(type(v) in (float, int) and math.isfinite(v) and abs(v) <= 1
               for v in stick):
        return stopped

    def axis(v):
        # Rescale outside the dead zone so motion starts from zero smoothly.
        return math.copysign(max(0., (abs(v) - DEAD_ZONE) / (1 - DEAD_ZONE)), v)

    return {"active": True, "vx": -axis(stick[1]) * MAX_FORWARD_M_S,
            "yaw": -axis(stick[0]) * MAX_YAW_RAD_S}
