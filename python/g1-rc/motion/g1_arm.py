"""Low-level arm pose controller for the G1 humanoid.

LocoClient only exposes named gestures (WaveHand, ShakeHand). For
arbitrary arm poses we publish `unitree_hg.msg.LowCmd_` directly to
`rt/arm_sdk` — the G1 firmware's DDS channel that accepts arm-only
joint targets while LocoClient continues to handle the legs.

How activation works (G1 v1.x firmware):
  - `motor_cmd[29].q` is the *arm_sdk control weight* — a float in
    [0.0, 1.0]. While the weight is 0, the legs/locomotion controller
    owns the arms. While 1, our LowCmd targets drive them.
  - We ramp the weight from 0 → 1 to take control, hold during the
    pose, then ramp back to 0 if `release_on_finish` is set.
  - Some firmwares index the weight at motor 13 or 14 instead of 29.
    If presets don't appear to take effect, see the WEIGHT_INDEX
    comment below and try alternatives.

Joint layout assumed (G1 29-DOF, 7-DOF arms each side). Verify the
indices on your firmware via `/state` — wrong indices mean the wrong
joint moves but won't damage the robot (limits are clamped).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Optional

logger = logging.getLogger("g1-motion.arm")


# G1 29-DOF joint map (firmware-dependent). 0-11 legs, 12-14 waist,
# 15-21 left arm, 22-28 right arm.
L_SHOULDER_PITCH = 15
L_SHOULDER_ROLL  = 16
L_SHOULDER_YAW   = 17
L_ELBOW          = 18
L_WRIST_ROLL     = 19
L_WRIST_PITCH    = 20
L_WRIST_YAW      = 21
R_SHOULDER_PITCH = 22
R_SHOULDER_ROLL  = 23
R_SHOULDER_YAW   = 24
R_ELBOW          = 25
R_WRIST_ROLL     = 26
R_WRIST_PITCH    = 27
R_WRIST_YAW      = 28

ARM_JOINTS_L = [
    L_SHOULDER_PITCH, L_SHOULDER_ROLL, L_SHOULDER_YAW,
    L_ELBOW, L_WRIST_ROLL, L_WRIST_PITCH, L_WRIST_YAW,
]
ARM_JOINTS_R = [
    R_SHOULDER_PITCH, R_SHOULDER_ROLL, R_SHOULDER_YAW,
    R_ELBOW, R_WRIST_ROLL, R_WRIST_PITCH, R_WRIST_YAW,
]

# `motor_cmd[WEIGHT_INDEX].q` is the arm_sdk activation weight in
# Unitree's recent G1 firmwares. If your firmware differs, override
# with G1_ARM_WEIGHT_INDEX env (some firmware revisions use 13 or 14).
WEIGHT_INDEX = int(os.environ.get("G1_ARM_WEIGHT_INDEX", "29"))

# Conservative gains. The G1's arms are direct-drive — high Kp shakes
# them. Start low; tune up if presets feel sloppy.
KP_ARM = float(os.environ.get("G1_ARM_KP", "60.0"))
KD_ARM = float(os.environ.get("G1_ARM_KD", "2.0"))

# Soft joint limits. Tighter than the hardware mechanical limits so a
# bad sign on a preset can't slam the arm into the body or the floor.
JOINT_LIMITS = {
    "shoulder_pitch": (-2.5,  2.5),
    "shoulder_roll":  (-1.5,  1.5),
    "shoulder_yaw":   (-2.0,  2.0),
    "elbow":          (-1.0,  2.2),
    "wrist_roll":     (-1.5,  1.5),
    "wrist_pitch":    (-1.0,  1.0),
    "wrist_yaw":      (-1.5,  1.5),
}
# Per-arm clamps in the joint order [pitch, roll, yaw, elbow, wroll, wpitch, wyaw].
CLAMP_PER_JOINT = [
    JOINT_LIMITS["shoulder_pitch"],
    JOINT_LIMITS["shoulder_roll"],
    JOINT_LIMITS["shoulder_yaw"],
    JOINT_LIMITS["elbow"],
    JOINT_LIMITS["wrist_roll"],
    JOINT_LIMITS["wrist_pitch"],
    JOINT_LIMITS["wrist_yaw"],
]

# Preset poses, indexed [shoulder_pitch, shoulder_roll, shoulder_yaw,
# elbow, wrist_roll, wrist_pitch, wrist_yaw]. Negative shoulder_pitch
# raises the arm forward / up. Positive shoulder_roll abducts the LEFT
# arm out to its side; for the RIGHT arm the sign flips.
PRESETS_LEFT = {
    "home":          [ 0.0,  0.10, 0.0, 0.0,  0.0, 0.0, 0.0],  # arm hanging
    "raise":         [ 0.0,  1.30, 0.0, 0.5,  0.0, 0.0, 0.0],  # arm out to side
    "point_forward": [-1.50, 0.10, 0.0, 0.2,  0.0, 0.0, 0.0],  # arm forward
    "hands_up":      [-2.20, 0.10, 0.0, 0.3,  0.0, 0.0, 0.0],  # arm overhead
    "salute":        [-1.80, 0.40, -0.3, 1.5, 0.0, 0.0, 0.0],
}
PRESETS_RIGHT = {
    "home":          [ 0.0, -0.10, 0.0, 0.0,  0.0, 0.0, 0.0],
    "raise":         [ 0.0, -1.30, 0.0, 0.5,  0.0, 0.0, 0.0],
    "point_forward": [-1.50,-0.10, 0.0, 0.2,  0.0, 0.0, 0.0],
    "hands_up":      [-2.20,-0.10, 0.0, 0.3,  0.0, 0.0, 0.0],
    "salute":        [-1.80,-0.40, 0.3, 1.5,  0.0, 0.0, 0.0],
}
PRESET_NAMES = list(PRESETS_LEFT.keys())


def _clamp_pose(joints: list[float]) -> list[float]:
    return [max(lo, min(hi, j)) for j, (lo, hi) in zip(joints, CLAMP_PER_JOINT)]


class G1Arm:
    """Owns the arm_sdk LowCmd publisher + TX thread + pose state."""

    def __init__(self) -> None:
        self._cmd_pub = None
        self._state_sub = None
        self._lowcmd = None
        self._tx_thread: Optional[threading.Thread] = None
        self._tx_stop = threading.Event()
        self._lock = threading.Lock()
        # We always publish (so the firmware sees a heartbeat); when
        # `_weight` is 0 the arms remain under LocoClient's control.
        self._weight: float = 0.0
        # 29-element target vector. Only arm indices are ever set;
        # other slots stay at their current measured value so we don't
        # fight the legs.
        self._target_q: list[float] = [0.0] * 35  # over-allocated for newer 35-DOF
        self._current_q: list[float] = [0.0] * 35
        self._move_lock = threading.Lock()  # serialises pose moves

    def connect(self) -> None:
        from unitree_sdk2py.core.channel import (
            ChannelPublisher, ChannelSubscriber,
        )
        from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowCmd_, LowState_
        from unitree_sdk2py.idl.default import unitree_hg_msg_dds__LowCmd_

        # arm_sdk topic name varies across SDK versions. Try in order;
        # publishers don't fail at Init() for non-existent topics so we
        # just go with the first that the SDK accepts.
        topic = os.environ.get("G1_ARM_TOPIC", "rt/arm_sdk")
        logger.info("Initializing arm_sdk publisher on %s", topic)
        self._cmd_pub = ChannelPublisher(topic, LowCmd_)
        self._cmd_pub.Init()

        # Subscribe to lowstate so we can seed _current_q for smooth
        # interpolation. The shared global Cyclone factory was already
        # initialised by G1Controller.connect() — don't re-init.
        self._state_sub = ChannelSubscriber("rt/lowstate", LowState_)
        self._state_sub.Init(self._on_state, 10)

        self._lowcmd = unitree_hg_msg_dds__LowCmd_()
        # Enable PMSM mode on the arm joints; leave everything else
        # untouched (LocoClient owns it).
        for j in ARM_JOINTS_L + ARM_JOINTS_R:
            self._lowcmd.motor_cmd[j].mode = 1
            self._lowcmd.motor_cmd[j].q  = 0.0
            self._lowcmd.motor_cmd[j].dq = 0.0
            self._lowcmd.motor_cmd[j].tau = 0.0
            self._lowcmd.motor_cmd[j].kp = 0.0
            self._lowcmd.motor_cmd[j].kd = 0.0

        self._tx_stop.clear()
        self._tx_thread = threading.Thread(
            target=self._tx_loop, name="g1-arm-tx", daemon=True,
        )
        self._tx_thread.start()
        logger.info("Arm TX thread started (200 Hz, weight=%d)", WEIGHT_INDEX)

    def shutdown(self) -> None:
        self._tx_stop.set()
        if self._tx_thread is not None:
            self._tx_thread.join(timeout=1.0)
            self._tx_thread = None

    # -- DDS callbacks ---------------------------------------------------------

    def _on_state(self, msg: Any) -> None:
        try:
            n = min(len(msg.motor_state), len(self._current_q))
            with self._lock:
                first_state = all(q == 0.0 for q in self._current_q[:n])
                for i in range(n):
                    self._current_q[i] = float(msg.motor_state[i].q)
                if first_state:
                    # On first packet, seed target = current so the
                    # first activation doesn't snap.
                    for i in range(n):
                        self._target_q[i] = self._current_q[i]
        except Exception as exc:
            logger.debug("Failed to parse LowState in G1Arm: %s", exc)

    # -- TX thread ------------------------------------------------------------

    def _tx_loop(self) -> None:
        period = 1.0 / 200.0
        while not self._tx_stop.is_set():
            try:
                with self._lock:
                    w = self._weight
                    for j in ARM_JOINTS_L + ARM_JOINTS_R:
                        self._lowcmd.motor_cmd[j].q  = self._target_q[j]
                        self._lowcmd.motor_cmd[j].dq = 0.0
                        self._lowcmd.motor_cmd[j].tau = 0.0
                        self._lowcmd.motor_cmd[j].kp = KP_ARM * w
                        self._lowcmd.motor_cmd[j].kd = KD_ARM * w
                    # arm_sdk activation weight goes on motor_cmd[WEIGHT_INDEX].q
                    try:
                        self._lowcmd.motor_cmd[WEIGHT_INDEX].q = w
                    except IndexError:
                        # Firmware doesn't expose that motor slot;
                        # weight signalling is disabled — gains still
                        # ramp via `w`, so motion will work but the
                        # firmware may keep legs-controller authority
                        # over arms.
                        pass
                self._cmd_pub.Write(self._lowcmd)
            except Exception as exc:
                logger.warning("LowCmd publish failed: %s", exc)
            time.sleep(period)

    # -- pose moves -----------------------------------------------------------

    def _interp_to(
        self,
        joints: list[int],
        target: list[float],
        duration: float,
        weight_ramp: bool = True,
    ) -> None:
        """Smoothly interpolate `joints` from current to target over `duration`.

        If `weight_ramp` is True and we're currently inactive, ramp the
        arm_sdk weight 0→1 in parallel so motion starts from a soft
        engagement, not a snap.
        """
        target = _clamp_pose(target)
        steps = max(1, int(duration * 100))  # 100 Hz update rate
        with self._lock:
            start_q = [self._current_q[j] for j in joints]
            start_w = self._weight
        for i in range(steps + 1):
            alpha = i / steps
            with self._lock:
                if weight_ramp:
                    self._weight = start_w + (1.0 - start_w) * min(1.0, alpha * 2.0)
                for k, j in enumerate(joints):
                    self._target_q[j] = (
                        start_q[k] + (target[k] - start_q[k]) * alpha
                    )
            time.sleep(0.01)

    def _release(self, fade_duration: float = 0.5) -> None:
        """Ramp the arm_sdk weight back to 0 so LocoClient retakes
        control of the arms. Joint targets are held at the last
        position during the fade."""
        steps = max(1, int(fade_duration * 100))
        with self._lock:
            start_w = self._weight
        for i in range(steps + 1):
            alpha = i / steps
            with self._lock:
                self._weight = start_w * (1.0 - alpha)
            time.sleep(0.01)

    def move_preset(
        self,
        preset: str,
        side: str = "both",
        duration: float = 2.0,
        release: bool = False,
    ) -> str:
        """Move one or both arms to a named preset.

        Args:
            preset: one of PRESET_NAMES.
            side: "left" / "right" / "both".
            duration: seconds for the linear ramp from current → target.
            release: if True, after holding, fade arm_sdk weight back
                so LocoClient retakes the arms.
        """
        if preset not in PRESETS_LEFT:
            raise ValueError(f"unknown preset {preset!r}; available: {PRESET_NAMES}")
        side = side.lower()
        if side not in ("left", "right", "both"):
            raise ValueError(f"side must be left/right/both, got {side!r}")

        with self._move_lock:
            if side in ("left", "both"):
                self._interp_to(
                    ARM_JOINTS_L,
                    PRESETS_LEFT[preset],
                    duration,
                )
            if side in ("right", "both"):
                self._interp_to(
                    ARM_JOINTS_R,
                    PRESETS_RIGHT[preset],
                    duration,
                    # weight already 1 after the left move; skip ramp
                    weight_ramp=(side == "right"),
                )
            if release:
                self._release()
        return f"arm preset={preset} side={side} duration={duration:.1f}s"
