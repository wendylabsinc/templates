"""Keep the Dex3 knuckles out of their thermal cutout.

Measured 2026-09-15, left hand, one 49 s session:

    joint      temp 1st   2nd    3rd   mode  motorstate   follow
    middle_0      39 C   70 C   90 C      0         512   100 -> 63 -> 40 %
    index_0       39 C   69 C   90 C      0         512    98 -> 68 -> 29 %
    index_1       35 C   48 C   61 C      1           0    99 % throughout

The knuckles reach 90 C in under a minute and the ones that get there end
`mode 0, motorstate 512` -- disabled by the hand's own protection. After that
they are dead for the session: the onboard writes `mode = 1` a hundred times a
second and the motor keeps answering 0, which is what a thermal cutout SHOULD
do. There is no reset in this protocol (`_RIS_Mode` carries only a 3-bit
status, and 0x01 is already "enable"), so a trip cannot be undone in software.

The only move left is not to trip. The hand reports its own temperature at
10 Hz and nothing read it for control.

So: scale the joint's stiffness down as it approaches the cutout. Full gain
below WARM_C, none at HOT_C, linear between. A hand that grips at 60 % force
all afternoon is worth more than one that grips perfectly for forty seconds
and then not at all.

Position commands are NOT touched. Lowering kp lowers the holding torque --
the finger still closes, just with less force -- which is the variable that
makes the heat. Moving the finger somewhere else would be a grip the operator
did not ask for.
"""
import os

ORDER = ["thumb_0", "thumb_1", "thumb_2", "middle_0", "middle_1",
         "index_0", "index_1"]

# Measured on the left hand: 39 -> 70 -> 90 C by thirds of a 49 s session, and
# the joints that reached 90 C were the ones disabled at the end of it. The
# joints that never tripped ran at 61-78 C.
#
# So backing off must START at 55, not 70: at 70 the joint is already two
# thirds of the way through its climb with 20 C of headroom left, and a
# session that reached 90 C in under a minute does not leave time to be
# gentle later. 55 is above anything an idle hand shows (34-40 C measured)
# and below the 61 C that a healthy working joint sustained.
WARM_C = 55.0
HOT_C = 78.0
# Never below this fraction of kp: a finger with no stiffness at all drops
# what it is holding, and dropping a can on the floor to save a motor is not
# the trade anyone wants mid-grasp.
MIN_SCALE = 0.25


class ThermalGuard:
    """Per-joint gain scale from the hand's own reported temperature."""

    def __init__(self, warm_c=WARM_C, hot_c=HOT_C, min_scale=MIN_SCALE, enabled=True):
        self.warm_c = float(warm_c)
        self.hot_c = max(float(hot_c), float(warm_c) + 1.0)
        self.min_scale = min(max(float(min_scale), 0.0), 1.0)
        self.enabled = bool(enabled)
        self._last = {}          # (side, name) -> scale, for reporting

    @classmethod
    def from_env(cls, env=None):
        e = env if env is not None else os.environ

        def num(name, default, lo, hi):
            try:
                v = float(e.get(name, default))
            except (TypeError, ValueError):
                return default
            return v if lo <= v <= hi else default

        return cls(warm_c=num("G1_DEX3_WARM_C", WARM_C, 30.0, 100.0),
                   hot_c=num("G1_DEX3_HOT_C", HOT_C, 35.0, 110.0),
                   min_scale=num("G1_DEX3_MIN_SCALE", MIN_SCALE, 0.0, 1.0),
                   enabled=str(e.get("G1_DEX3_THERMAL", "1")).strip() not in ("0", "off"))

    def scale(self, side, temps7):
        """-> [7] gain multipliers in the bridge's joint order.

        A missing or non-numeric temperature reads as 1.0 -- full gain. A hand
        that will not report is not a reason to weaken every grip on the robot.
        """
        if not self.enabled or not temps7 or len(temps7) != len(ORDER):
            return [1.0] * len(ORDER)
        out = []
        for i, name in enumerate(ORDER):
            t = temps7[i]
            if not isinstance(t, (int, float)):
                out.append(1.0)
                continue
            if t <= self.warm_c:
                s = 1.0
            elif t >= self.hot_c:
                s = self.min_scale
            else:
                span = (t - self.warm_c) / (self.hot_c - self.warm_c)
                s = 1.0 - span * (1.0 - self.min_scale)
            out.append(s)
            self._last[(side, name)] = round(s, 3)
        return out

    def status(self):
        """-> {side: {joint: scale}} for the joints currently held back."""
        out = {}
        for (side, name), s in self._last.items():
            if s < 0.999:
                out.setdefault(side, {})[name] = s
        return out
