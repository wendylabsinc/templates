"""Held-X directional fore/aft gain for controller wrists before arm IK.

The raw WebXR frame is never mutated.  Callers feed the returned frame to the
retargeter and keep the original for recording; the observation returned beside
it makes the derived transform reproducible offline.
"""
from __future__ import annotations

import math
import os
import time


SCHEMA = "wendy.g1.controller-wrist-gain.v1"


class ControllerWristGain:
    """Incremental Z-only gain while the left controller's X button is held.

    Controller wrists are in the head-heading WebXR frame: -Z is forward, so a
    positive Z delta is backward. X/Y and every non-position field pass through
    unchanged. A persistent per-side Z offset rebases normal 1:1 control when X
    is released, avoiding a discontinuity at the mode transition.
    """

    def __init__(self, backward_gain=1.5, forward_gain=0.35,
                 deadband_m=0.003, max_velocity_m_s=0.6, max_dt_s=0.05, enabled=True):
        vals = (backward_gain, forward_gain, deadband_m,
                max_velocity_m_s, max_dt_s)
        if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in vals):
            raise ValueError("controller wrist gain values must be finite numbers")
        if (backward_gain <= 0 or forward_gain <= 0 or deadband_m < 0
                or max_velocity_m_s <= 0 or max_dt_s <= 0):
            raise ValueError("gains/speed/dt must be positive and deadband non-negative")
        self.backward_gain = float(backward_gain)
        self.forward_gain = float(forward_gain)
        self.deadband_m = float(deadband_m)
        self.max_velocity_m_s = float(max_velocity_m_s)
        self.max_dt_s = float(max_dt_s)
        # Off: X is ignored and every controller passes through 1:1, exactly
        # as if X were never held.
        self.enabled = bool(enabled)
        self._active = False
        self._sides = {}
        self._offset_z = {}
        self._last_observation = self._observation(False, False, {})

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        # DISABLED, and deliberately not configurable: the operator removed
        # held X as a control. The transform is kept for offline replay of
        # older sessions and for a later decision to bring it back, which is
        # a code change rather than an env var someone can set by accident.
        try:
            return cls(
                enabled=False,
                backward_gain=float(env.get("TELEOP_ARM_BACKWARD_GAIN", "1.5")),
                forward_gain=float(env.get("TELEOP_ARM_FORWARD_GAIN", "0.35")),
                deadband_m=float(env.get("TELEOP_ARM_DIRECTION_DEADBAND_M", "0.003")),
                max_velocity_m_s=float(env.get("TELEOP_ARM_DIRECTION_MAX_M_S", "0.6")),
                max_dt_s=float(env.get("TELEOP_ARM_DIRECTION_MAX_DT_S", "0.05")),
            )
        except (TypeError, ValueError) as e:
            raise ValueError(f"controller wrist directional gain config: {e}") from e

    @staticmethod
    def _controller_hands(frame):
        return [h for h in (frame.get("hands") or ())
                if isinstance(h, dict) and h.get("source") == "controller"]

    @staticmethod
    def _wrist(hand):
        w = hand.get("wrist")
        if (not isinstance(w, (list, tuple)) or len(w) != 3
                or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in w)):
            return None
        return [float(v) for v in w]

    @classmethod
    def _x_down(cls, frame):
        return any(h.get("hand") == "left"
                   and bool((h.get("buttons") or {}).get("primary"))
                   for h in cls._controller_hands(frame))

    def _capture(self, side, wrist, now):
        effective_z = wrist[2] + self._offset_z.get(side, 0.0)
        self._sides[side] = {
            "captured_raw_wrist": list(wrist),
            "raw_wrist": list(wrist),
            "raw_z": wrist[2],
            "effective_z": effective_z,
            "residual_z": 0.0,
            "t": now,
        }
        return self._sides[side]

    def reset(self):
        self._active = False
        self._sides = {}
        self._offset_z = {}
        self._last_observation = self._observation(False, False, {})

    def transform(self, frame, armed, now=None):
        """Return `(control_frame, derived_observation)` without mutating frame."""
        now = time.monotonic() if now is None else float(now)
        if not armed or not self.enabled:
            self.reset()
            return frame, self._last_observation

        controllers = self._controller_hands(frame)
        down = self._x_down(frame)
        pressed = down and not self._active
        released = self._active and not down
        if pressed:
            self._active = True

        by_id = {id(h): h for h in controllers}
        transformed = []
        side_obs = {}
        for original in frame.get("hands") or ():
            if id(original) not in by_id:
                transformed.append(original)
                continue
            side = original.get("hand")
            wrist = self._wrist(original)
            if side not in ("left", "right") or wrist is None:
                transformed.append(original)
                continue

            row = self._sides.get(side)
            velocity_limited = False
            raw_delta_z = 0.0
            scaled_delta_z = 0.0
            if pressed or (down and row is None):
                row = self._capture(side, wrist, now)
            elif down and row is not None:
                raw_delta_z = wrist[2] - row["raw_z"]
                dt = min(self.max_dt_s, max(0.0, now - row["t"]))
                row["raw_z"], row["t"] = wrist[2], now
                row["raw_wrist"] = list(wrist)
                row["residual_z"] += raw_delta_z
                residual = row["residual_z"]
                if abs(residual) > self.deadband_m:
                    usable = residual - math.copysign(self.deadband_m, residual)
                    row["residual_z"] = math.copysign(self.deadband_m, residual)
                    gain = self.backward_gain if usable > 0 else self.forward_gain
                    scaled_delta_z = usable * gain
                    bound = self.max_velocity_m_s * dt
                    limited = max(-bound, min(bound, scaled_delta_z))
                    velocity_limited = limited != scaled_delta_z
                    scaled_delta_z = limited
                    row["effective_z"] += scaled_delta_z
            elif released and row is not None:
                # Keep the last effective point on the release frame, then let
                # subsequent raw motion continue 1:1 around this new origin.
                self._offset_z[side] = row["effective_z"] - wrist[2]
                row["raw_wrist"] = list(wrist)

            out_wrist = list(wrist)
            if down and row is not None:
                out_wrist[2] = row["effective_z"]
            else:
                out_wrist[2] += self._offset_z.get(side, 0.0)
            changed = dict(original)
            changed["wrist"] = out_wrist
            transformed.append(changed)
            side_obs[side] = {
                "captured_raw_wrist": (list(row["captured_raw_wrist"]) if row else None),
                "raw_wrist": list(wrist),
                "effective_wrist": list(out_wrist),
                "raw_delta_z": round(raw_delta_z, 6),
                "scaled_delta_z": round(scaled_delta_z, 6),
                "velocity_limited": velocity_limited,
                "rebase_offset_z": round(self._offset_z.get(side, 0.0), 6),
                "tracked": True,
            }

        if released:
            # A side can disappear on the exact release frame. Rebase it from
            # its last observed raw pose too; otherwise it snaps back to the
            # pre-X mapping when tracking returns.
            for side, row in self._sides.items():
                if side in side_obs:
                    continue
                self._offset_z[side] = row["effective_z"] - row["raw_z"]
                effective = list(row["raw_wrist"])
                effective[2] = row["effective_z"]
                side_obs[side] = {
                    "captured_raw_wrist": list(row["captured_raw_wrist"]),
                    "raw_wrist": list(row["raw_wrist"]),
                    "effective_wrist": effective,
                    "raw_delta_z": 0.0,
                    "scaled_delta_z": 0.0,
                    "velocity_limited": False,
                    "rebase_offset_z": round(self._offset_z[side], 6),
                    "tracked": False,
                }
            self._active = False
            self._sides = {}

        control = dict(frame)
        control["hands"] = transformed
        obs = self._observation(pressed, released, side_obs)
        self._last_observation = obs
        return control, obs

    def _observation(self, pressed, released, sides):
        return {
            "schema": SCHEMA,
            "enabled": self.enabled,
            "active": self._active,
            "pressed": bool(pressed),
            "released": bool(released),
            "backward_gain": self.backward_gain,
            "forward_gain": self.forward_gain,
            "deadband_m": self.deadband_m,
            "max_velocity_m_s": self.max_velocity_m_s,
            "max_dt_s": self.max_dt_s,
            "axis": "head_heading_z",
            "backward_sign": 1,
            "sides": sides,
        }

    def status(self):
        return dict(self._last_observation)
