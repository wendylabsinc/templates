"""Dex3 hand: the closed pose, the range table, and the stall back-off.

Pure Python, no DDS, so it imports on a laptop and in a test; g1_dex3.py
(which needs unitree_sdk2py at import) re-exports the constants from here.

Why this module exists, in the operator's words: "when they feel resistance
they dont recover". In session 20260915-185518-handfix the KNUCKLES
(middle_0, index_0) fell from 98 % follow to 12-17 % and stayed there while
the OUTER joints of the same fingers held 99 % throughout. Same hand, same
commands, same wire. Whatever it is distinguishes the knuckle motors from the
outer ones -- and the knuckles are the load-bearing joints: closing on a can
they stop ON the can while the command stays at the range end, so the motor
holds kp x the full gap (up to 1.5 rad at kp 3) for the whole grip. Stall
current, heat. Thermal or current derating fits the 98 -> 17 -> 12 sequence;
a latched motor fault fits it too. Neither is confirmed -- the telemetry that
would confirm it (g1_dex3.Dex3Sender.measured_state) lands in the same
change -- but both mechanisms share the cause this addresses: a motor held
at full error against something it cannot move.

StallBackoff shrinks the HOLDING error, never the intent: a joint that has
not moved for `stall_s` while `gap_rad` short of its command is being held
by something, so the command it is sent becomes measured + margin in the
command's direction. The grip force becomes kp x margin instead of kp x gap.
The instant the raw command comes back toward the finger (the operator lets
go) it passes straight through -- recovery is not gated on anything.

If the cause was heat, the new per-motor temperature stops climbing during a
grip and the knuckles keep following late in a session. If it was a latch,
`motorstate` goes non-zero at the moment they stop and stays there, and this
back-off changes nothing -- which is also an answer.
"""
import math
import os
import time

# The bridge's joint order for one hand (retarget_g1.DEX3_LEFT with the side
# stripped). Both hands speak this order on the wire.
ORDER = ("thumb_0", "thumb_1", "thumb_2", "middle_0", "middle_1", "index_0", "index_1")

# retarget_g1.DEX3_RANGE, per hand, in ORDER. Copied because the bridge does
# not run on the robot; tests/test_dex3_stall.py pins both tables to the
# bridge's, so a drift fails a test rather than clamping a real finger wrong.
RANGE_LEFT = ((-1.04719755, 1.04719755), (-0.72431163, 0.920), (0.0, 1.74532925),
              (-1.57079632, 0.0), (-1.74532925, 0.0), (-1.57079632, 0.0), (-1.74532925, 0.0))
RANGE_RIGHT = ((-1.04719755, 1.04719755), (-0.920, 0.72431163), (-1.74532925, 0.0),
               (0.0, 1.57079632), (0.0, 1.74532925), (0.0, 1.57079632), (0.0, 1.74532925))

# grip_q(side, 1.0) with GRIP_THUMB0 = 0: the onboard's FALLBACK closed pose,
# used only when the bridge sent none (an older bridge). The bridge now sends
# the pose it would command at a full squeeze with every `home`, thumb_0 knob
# included, so the two cannot disagree about what "closed" means.
CLOSED_LEFT = [0.0, 0.92, 1.74532925, -1.57079632, -1.74532925, -1.57079632, -1.74532925]
CLOSED_RIGHT = [0.0, -0.92, -1.74532925, 1.57079632, 1.74532925, 1.57079632, 1.74532925]


def sanitize_hand_pose(hands):
    """{"left": [7], "right": [7]} -> (left7, right7) clamped into range, or None.

    None, never a partial pose: a home that closes one hand to what it was
    sent and the other to a default is a home nobody asked for. Non-finite
    anywhere refuses the whole payload, as sanitize_arm does for the arms.
    """
    if not isinstance(hands, dict):
        return None
    out = []
    for side, table in (("left", RANGE_LEFT), ("right", RANGE_RIGHT)):
        v = hands.get(side)
        if not isinstance(v, (list, tuple)) or len(v) != 7:
            return None
        row = []
        for x, (lo, hi) in zip(v, table):
            if isinstance(x, bool) or not isinstance(x, (int, float)):
                return None
            x = float(x)
            if not math.isfinite(x):
                return None
            row.append(min(hi, max(lo, x)))
        out.append(row)
    return out[0], out[1]


class StallBackoff:
    """Per hand joint: back a stalled command off to measured + margin."""

    # A joint that has moved less than this since the stall began has stopped.
    # 0.02 rad is about a degree: below the Dex3's own reporting noise floor
    # by a comfortable margin, above nothing a real finger does while gripping.
    MOVE_RAD = 0.02

    # margin_rad is THE GRIP FORCE on anything the hand cannot close around.
    #
    # A stalled finger holds `margin_rad` of position error, and the motor's
    # torque is that error times kp: it is the number that decides how hard a
    # hand squeezes a can it has already stopped closing on, and therefore how
    # much current it draws for as long as it holds. It was 0.35 rad, which at
    # kp 3.0 cooked a thumb to 86 C twice in one evening and tripped it both
    # times.
    #
    # 0.15 halves that and leaves the fingers at FULL strength everywhere
    # else: this applies only after 0.5 s of not moving against a gap, so
    # closing, opening and following the operator are untouched. Lowering kp
    # instead -- the only lever the deploy script exposed until now -- weakens
    # the same fingers whose knuckles needed kp 3.0 to move at all.
    #
    # Too low and the can slips; the operator tunes it against a real can via
    # G1_DEX3_HOLD_MARGIN_RAD, which deploy-onboard.sh now passes.
    #
    # The GAP that counts as a stall is the margin, not twice it. At 0.3 a
    # finger stopped between 0.15 and 0.3 short was never backed off and
    # pushed with its whole gap for as long as the trigger was held -- MORE
    # than the hold margin allows a finger that did stall. That is the empty
    # hand at full trigger: thumb_1 and thumb_2 stop ~0.21 rad short against
    # the closed fingers, and on 2026-09-16 ran 49-61 C with the other
    # fingers at 30-45 C. At 0.15 they are held at measured + 0.15 like any
    # other stalled finger. Closing, opening and following are untouched: a
    # finger has to sit still for stall_s first.
    def __init__(self, stall_s=0.5, gap_rad=0.15, margin_rad=0.15, enabled=True,
                 clock=time.monotonic):
        self.stall_s = float(stall_s)
        self.gap_rad = float(gap_rad)
        self.margin_rad = float(margin_rad)
        self.enabled = bool(enabled)
        self.clock = clock
        # (side, joint) -> {"q": measured when the still period began, "t": then,
        #                   "dir": sign(cmd - meas) then}
        self._still = {}

    @classmethod
    def from_env(cls, env=None, clock=time.monotonic):
        """Thresholds from G1_DEX3_STALL_*; refuses nonsense rather than guessing."""
        env = os.environ if env is None else env

        def num(name, default, lo, hi):
            raw = env.get(name, "")
            if raw == "":
                return default
            v = float(raw)
            if not math.isfinite(v) or not lo <= v <= hi:
                raise ValueError(f"{name}={raw!r}: expected a finite number in [{lo}, {hi}]")
            return v

        enabled = env.get("G1_DEX3_STALL_BACKOFF", "1").strip().lower() not in ("0", "off", "no", "false")
        return cls(stall_s=num("G1_DEX3_STALL_S", 0.5, 0.05, 10.0),
                   gap_rad=num("G1_DEX3_STALL_GAP_RAD", 0.15, 0.02, 2.0),
                   margin_rad=num("G1_DEX3_HOLD_MARGIN_RAD", 0.15, 0.0, 2.0),
                   enabled=enabled, clock=clock)

    def apply(self, side, cmd7, meas7, mode7=None):
        """-> (command to send, [joint names backed off]). Never raises.

        `mode7` is the per-motor enable the hand reports back. A motor at
        mode 0 is DISABLED and will never move, which looks exactly like a
        stalled one -- measured 2629 of 2675 frames on a right thumb_1 that
        turned out to be faulted in hardware (motorstate 512, mode 0, on
        every frame, cold). Backing it off does nothing to a motor that is
        not listening, and it reports "backed off" where the truth is
        "faulted", which is the more useful word and the one that sends
        somebody to the hand rather than to the code.
        """
        cmd = list(cmd7)
        if not self.enabled or meas7 is None or len(meas7) != len(cmd):
            return cmd, []
        disabled = set()
        if mode7 is not None and len(mode7) == len(cmd):
            disabled = {i for i, m in enumerate(mode7) if m == 0}
        now = self.clock()
        names = []
        for i, name in enumerate(ORDER):
            if i in disabled:
                continue                     # faulted, not stalled
            c, m = cmd[i], meas7[i]
            if not isinstance(m, (int, float)) or not isinstance(c, (int, float)):
                continue
            gap = c - m
            d = 1.0 if gap > 0 else -1.0 if gap < 0 else 0.0
            key = (side, name)
            st = self._still.get(key)
            # The still period ends when the finger moves, when the command
            # comes back within the gap, or when it reverses across the
            # finger. Any of those means nothing is holding it against this
            # command any more, and the timer starts over.
            if st is None or abs(m - st["q"]) > self.MOVE_RAD or abs(gap) <= self.gap_rad or d != st["dir"]:
                if abs(gap) > self.gap_rad and d != 0.0:
                    self._still[key] = {"q": m, "t": now, "dir": d}
                else:
                    self._still.pop(key, None)
                continue
            if now - st["t"] < self.stall_s:
                continue
            # Stalled: hold at measured + margin, never past the command.
            back = m + d * min(self.margin_rad, abs(gap))
            cmd[i] = back
            names.append(name)
        return cmd, names
