"""Waist yaw from the RIGHT thumbstick: the reach the arm cannot give.

Every reach problem so far was fought inside the arm. The shoulder-yaw search
exists because the hand could not get across the body, and the receiver in
the coke task sits 42 cm to the operator's side. Turning the waist moves the
whole workspace instead of contorting the arm -- and the training side already
expects it: the 5090 policy corrects waist yaw as one of its 15 slots.

Yaw only. This robot's waist roll and pitch report dead (27 of 29 motors live,
see TELEOP-RUNBOOK.md "waist reads 1-DoF"), and both would shift the centre of
mass on a standing robot. Motor 12, `waist_yaw_joint`, model range +-2.618 rad.

Driven by the stick, deliberately, rather than derived from the operator's
body. The operator's complaint about the shoulder yaw was "goes somewhere I
didn't point" -- motion nobody asked for. A torso inferred from the hands or
the head would be exactly that: hands doing a task move relative to each
other, and a head turns to look. The stick is explicit, and its value is on
the wire and in every row, so an offline replay can say later whether a
follow mode would have been right.

Rate control, not position: the stick's deflection is a turning SPEED, and
releasing it holds. Position control would put the waist at whatever angle
the stick happened to rest at, and a thumb slipping off would be a snap.

Sign. Robot yaw is about +z (up) with x forward and y left, so the robot's
RIGHT is NEGATIVE yaw. Pushing the stick right (x > 0) must turn the robot to
its right, hence target += -x * rate * dt.
"""
from __future__ import annotations

import math
import os

DEAD_ZONE = 0.15          # a resting thumb sits at a few hundredths; never move on it
DEFAULT_RATE_RAD_S = 0.5  # ~30 deg/s at full deflection: a deliberate turn, not a flick
DEFAULT_CAP_DEG = 143.0   # the whole turn the joint has: the model's 2.618 rad
                          # (150 deg) less a few degrees so the motor is never
                          # driven into its mechanical stop and left stalling
                          # there. The onboard's own limit is the safety bound;
                          # this only decides how far the stick can ASK.
DEFAULT_ACK_LEAD_DEG = 3.0      # at most ~90 ms ahead of onboard's 0.6 rad/s slew
DEFAULT_ACK_MAX_AGE_S = 0.25    # actual applies can arrive at ~8 Hz with Dex3 traffic
# A long gap between frames must not license a correspondingly long step --
# the same rule the onboard's slew applies. 0.1 s at full rate is 3 deg.
MAX_DT_S = 0.1


class WaistDial:
    """Per-bridge state: where the waist target is, and where it started.

    The target is ABSOLUTE (radians on motor 12). On the first ARMED frame it
    is seeded from the measured waist, so arming never commands a move; the
    stick then integrates from there. Leaving ARMED forgets everything: each
    arming re-captures, exactly as the arm's arming offset does.
    """

    def __init__(self, rate_rad_s=DEFAULT_RATE_RAD_S, cap_deg=DEFAULT_CAP_DEG,
                 dead_zone=DEAD_ZONE, ack_lead_deg=DEFAULT_ACK_LEAD_DEG,
                 ack_max_age_s=DEFAULT_ACK_MAX_AGE_S):
        for v in (rate_rad_s, cap_deg, dead_zone, ack_lead_deg, ack_max_age_s):
            if not (isinstance(v, (int, float)) and math.isfinite(v)):
                raise ValueError("waist rate, cap, ack lead/age and dead zone must be finite")
        if (rate_rad_s <= 0 or cap_deg <= 0 or ack_lead_deg <= 0
                or ack_max_age_s <= 0 or not 0 <= dead_zone < 1):
            raise ValueError("waist rate, cap, ack lead/age must be positive; dead zone in [0, 1)")
        self.rate = float(rate_rad_s)
        self.cap = math.radians(float(cap_deg))
        self.dead_zone = float(dead_zone)
        self.ack_lead = math.radians(float(ack_lead_deg))
        self.ack_max_age_s = float(ack_max_age_s)
        self._target = None       # None: not armed / not seeded
        self._t_prev = None
        self._moving = False
        self._ack_fresh = False
        self._ack_cmd = None
        self._ack_age_s = None
        self._warned_no_measure = False

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        try:
            rate = float(env.get("TELEOP_WAIST_RATE_RAD_S", DEFAULT_RATE_RAD_S))
            cap = float(env.get("TELEOP_WAIST_MAX_DEG", DEFAULT_CAP_DEG))
            ack_lead = float(env.get("TELEOP_WAIST_ACK_LEAD_DEG", DEFAULT_ACK_LEAD_DEG))
            ack_age = float(env.get("TELEOP_WAIST_ACK_MAX_AGE_S", DEFAULT_ACK_MAX_AGE_S))
        except (TypeError, ValueError) as e:
            raise ValueError("TELEOP_WAIST_RATE_RAD_S / TELEOP_WAIST_MAX_DEG / "
                             "TELEOP_WAIST_ACK_LEAD_DEG / TELEOP_WAIST_ACK_MAX_AGE_S: "
                             f"{e}") from e
        return cls(rate_rad_s=rate, cap_deg=cap, ack_lead_deg=ack_lead,
                   ack_max_age_s=ack_age)

    @staticmethod
    def stick_x(frame):
        """The RIGHT controller's thumbstick x, or None for a tracked hand.

        Only a controller has a stick. A tracked hand carries none, and a
        frame that carries no right controller must not move the waist -- so
        None here, never 0.0, which would read as "stick centred".
        """
        for h in (frame.get("hands") or ()):
            if h.get("hand") == "right" and h.get("source") == "controller":
                ts = h.get("thumbstick")
                if isinstance(ts, (list, tuple)) and len(ts) >= 1:
                    x = ts[0]
                    if isinstance(x, (int, float)) and math.isfinite(x):
                        return float(x)
                return None
        return None

    def step(self, frame, armed, measured, now, acknowledged=None, ack_age_s=None):
        """-> the absolute waist target (rad) for this frame, or None.

        None when not armed or when the frame has no right controller: nothing
        is added to the targets, so nothing is sent and the onboard leaves
        motor 12 alone. `measured` is motor 12 from the onboard's body state
        (None when the onboard predates it).
        """
        if not armed:
            self._target = None
            self._t_prev = None
            self._moving = False
            self._note_ack(None, None)
            return None
        x = self.stick_x(frame)
        if x is None:
            return None
        if self._target is None:
            # First ARMED frame: start where the waist IS, so arming is not a
            # move. Without a measurement, zero -- and say so, because a waist
            # that is not at zero will then step there at the onboard's slew.
            if measured is None:
                if not self._warned_no_measure:
                    self._warned_no_measure = True
                    print("[waist] armed with no measured waist -- starting the dial at 0; "
                          "the onboard's body state is what seeds it")
                self._target = 0.0
            else:
                self._target = float(measured)
            self._t_prev = now
            self._moving = False
            self._note_ack(acknowledged, ack_age_s)
            return self._clamp(self._target)
        dt = min(MAX_DT_S, max(0.0, now - self._t_prev))
        self._t_prev = now
        ack = self._note_ack(acknowledged, ack_age_s)
        if abs(x) >= self.dead_zone:
            self._target += -x * self.rate * dt
            self._moving = True
            if ack is not None:
                self._target = max(ack - self.ack_lead,
                                   min(ack + self.ack_lead, self._target))
        elif self._moving:
            # A fresh applied-command acknowledgement says how far the
            # onboard has actually advanced after its slew/leash. Holding it
            # drops only unacknowledged backlog. Stale/missing ack keeps the
            # exact old integrator behavior instead of pinning the waist.
            if ack is not None:
                self._target = ack
            self._moving = False
        self._target = self._clamp(self._target)
        return self._target

    def velocity(self, frame, armed):
        """Return the bounded motor velocity requested by the right stick.

        Position integration remains in :meth:`step` only for the orange
        operator-intent trace. The physical controller receives this velocity
        and integrates it exactly once per onboard motor tick, so websocket
        resend cadence can no longer change how far the waist moves.
        """
        if not armed:
            return None
        x = self.stick_x(frame)
        if x is None:
            return None
        if abs(x) < self.dead_zone:
            return 0.0
        x = max(-1.0, min(1.0, x))
        return -x * self.rate

    def _note_ack(self, acknowledged, age_s):
        valid = (isinstance(acknowledged, (int, float)) and math.isfinite(acknowledged)
                 and isinstance(age_s, (int, float)) and math.isfinite(age_s)
                 and 0.0 <= age_s <= self.ack_max_age_s)
        self._ack_fresh = bool(valid)
        self._ack_cmd = float(acknowledged) if valid else None
        self._ack_age_s = float(age_s) if isinstance(age_s, (int, float)) and math.isfinite(age_s) else None
        return self._ack_cmd

    def _clamp(self, v):
        return max(-self.cap, min(self.cap, v))

    def status(self):
        return {"target": (round(self._target, 4) if self._target is not None else None),
                "rate_rad_s": self.rate, "cap_deg": round(math.degrees(self.cap), 1),
                "ack_lead_deg": round(math.degrees(self.ack_lead), 1),
                "ack_max_age_s": self.ack_max_age_s, "ack_fresh": self._ack_fresh,
                "ack_cmd": self._ack_cmd, "ack_age_s": self._ack_age_s,
                "moving": self._moving}
