"""Wraps unitree_sdk2_python's G1 LocoClient.

The G1 humanoid uses the same SDK family as the Go2 (DDS over
CycloneDDS, unitree_sdk2_python bindings), but with a different
high-level client — `LocoClient` instead of `SportClient`. Most method
names overlap with go2-motion's controller; the differences worth
flagging:

  - `Move()` and `StopMove()` are the same shape.
  - Posture: G1 has `StandUp()`, `Squat()`, `SitDown()`. There's no
    `Sit` in the dog sense; `SitDown` puts the humanoid on the floor.
  - Hand gestures live on LocoClient directly: `WaveHand(turn_flag)`,
    `ShakeHand(stage)`. These move the arms, not Dex3 fingers — finger
    control is a separate SDK (deferred to v2).
  - LowState type is `unitree_hg.msg.LowState_`, not `unitree_go.…`.

Public API mirrors `go2_controller.Go2Controller` so `main.py` shape
is preserved.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import Any, Optional

logger = logging.getLogger("g1-motion")


MAX_VX = 0.6      # m/s
MAX_VY = 0.4      # m/s
MAX_VYAW = 1.0    # rad/s
DEFAULT_MOVE_SECONDS = 2.0
MOVE_WATCHDOG_SLOP_S = 0.5
VELOCITY_WATCHDOG_S = 1.0
# Same rationale as go2-motion: every LocoClient call goes through a
# thread + wait_for so a stuck DDS write can't deadlock the asyncio
# loop. Healthy writes return in a few ms.
SDK_CALL_TIMEOUT_S = float(os.environ.get("MOTION_SDK_CALL_TIMEOUT_S", "0.5"))
# Posture skills (StandUp / Squat / SitDown / Lie2StandUp / WaveHand /
# ShakeHand) trigger an internal FSM transition and routinely take
# 1-5 s. Use a longer timeout.
SDK_SKILL_TIMEOUT_S = float(os.environ.get("MOTION_SDK_SKILL_TIMEOUT_S", "8.0"))


def _clamp(value: float, limit: float) -> float:
    return max(-limit, min(limit, value))


def _import_loco_client():
    """Find LocoClient in whichever submodule path the installed SDK uses.

    Different `unitree_sdk2_python` SHAs have moved the G1 LocoClient
    around — known locations across recent versions:

      - `unitree_sdk2py.g1.loco.g1_loco_client.LocoClient`  (current master)
      - `unitree_sdk2py.g1.loco_client.LocoClient`           (older)
      - `unitree_sdk2py.g1.LocoClient`                       (newer)

    Try each in order, raising a clear error listing what we tried so
    a future SDK bump is easy to diagnose.
    """
    candidates = [
        ("unitree_sdk2py.g1.loco.g1_loco_client", "LocoClient"),
        ("unitree_sdk2py.g1.loco_client", "LocoClient"),
        ("unitree_sdk2py.g1.loco.loco_client", "LocoClient"),
        ("unitree_sdk2py.g1", "LocoClient"),
    ]
    last_exc: Optional[Exception] = None
    for module_path, attr in candidates:
        try:
            mod = __import__(module_path, fromlist=[attr])
            return getattr(mod, attr)
        except (ImportError, AttributeError) as exc:
            last_exc = exc
    raise ImportError(
        "Couldn't locate LocoClient in unitree_sdk2py. Tried: "
        + ", ".join(f"{m}.{a}" for m, a in candidates)
        + f". Last error: {last_exc}"
    )


class G1Controller:
    def __init__(self, network_interface: Optional[str] = None) -> None:
        self._network_interface = network_interface or os.environ.get(
            "G1_NETWORK_INTERFACE", "eth0"
        )
        self._loco_client = None
        self._lowstate_sub = None
        self._move_lock = asyncio.Lock()
        self._watchdog: Optional[asyncio.Task] = None
        self._state_lock = threading.Lock()
        self._latest_state: dict[str, Any] = {}
        # Arm controller is lazily imported in connect() because it
        # touches the DDS channel factory which is initialised there.
        self._arm = None

    def connect(self) -> None:
        from unitree_sdk2py.core.channel import (
            ChannelFactoryInitialize,
            ChannelSubscriber,
        )
        LocoClient = _import_loco_client()
        # G1 publishes LowState on a different IDL than Go2 — the
        # `unitree_hg` package, not `unitree_go`. Get the import wrong
        # and Cyclone silently drops every sample.
        from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowState_

        logger.info("Initializing DDS on interface %s", self._network_interface)
        ChannelFactoryInitialize(0, self._network_interface)

        client = LocoClient()
        client.SetTimeout(3.0)
        client.Init()
        self._loco_client = client
        logger.info("LocoClient ready")


        sub = ChannelSubscriber("rt/lowstate", LowState_)
        sub.Init(self._on_lowstate, 10)
        self._lowstate_sub = sub
        logger.info("Subscribed to rt/lowstate (unitree_hg.LowState_)")

        # Bring up the arm_sdk publisher + TX thread. Optional — failures
        # here are logged but don't abort startup so locomotion still
        # works if the arm_sdk topic is unavailable on this firmware.
        try:
            from g1_arm import G1Arm
            arm = G1Arm()
            arm.connect()
            self._arm = arm
        except Exception:
            logger.exception("Arm controller init failed — arm presets disabled")

    def _on_lowstate(self, msg: Any) -> None:
        try:
            rpy = list(msg.imu_state.rpy) if msg.imu_state.rpy else [0.0, 0.0, 0.0]
            # G1's HighFreqState equivalent. The fields are roughly
            # analogous to Go2 but not byte-for-byte identical; we
            # pluck the ones the brain cares about and fall back to
            # None / 0 for anything missing on a given firmware.
            bms = getattr(msg, "power_supply", None) or getattr(msg, "bms_state", None)
            soc = int(getattr(bms, "soc", 0)) if bms is not None else 0
            power_v = float(getattr(msg, "power_v", 0.0))
            foot_force = list(getattr(msg, "foot_force", []) or [])
            tick = int(getattr(msg, "tick", 0))
            state = {
                "battery_soc": soc,
                "power_v": power_v,
                "imu_rpy": rpy,
                "foot_force": foot_force,
                "tick": tick,
            }
        except Exception as exc:
            logger.warning("Failed to parse LowState: %s", exc)
            return
        with self._state_lock:
            self._latest_state = state

    def latest_state(self) -> dict[str, Any]:
        with self._state_lock:
            return dict(self._latest_state)

    def _require_client(self):
        if self._loco_client is None:
            raise RuntimeError("G1Controller.connect() was not called")
        return self._loco_client

    async def _call_sdk(self, method_name: str, *args, timeout: float = SDK_CALL_TIMEOUT_S) -> None:
        """Run a LocoClient method off the event loop with a timeout.

        Same pattern as go2-motion's `_call_sdk`. See
        `go2_controller.py` for the long-form rationale.
        """
        client = self._require_client()
        method = getattr(client, method_name)
        try:
            await asyncio.wait_for(
                asyncio.to_thread(method, *args),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "LocoClient.%s%s timed out after %.2fs (SDK stuck — "
                "next request will retry)",
                method_name, args, timeout,
            )
            raise

    async def _arm_watchdog(self, seconds: float) -> None:
        self._cancel_watchdog()

        async def _stop_after():
            try:
                await asyncio.sleep(seconds)
                logger.info("Watchdog firing after %.2f s; stopping G1", seconds)
                try:
                    await self._call_sdk("StopMove")
                except Exception as exc:
                    logger.warning("watchdog StopMove failed: %s", exc)
            except asyncio.CancelledError:
                pass

        self._watchdog = asyncio.create_task(_stop_after())

    def _cancel_watchdog(self) -> None:
        if self._watchdog and not self._watchdog.done():
            self._watchdog.cancel()

    # -- non-blocking motion (brain-facing) -------------------------------------

    async def set_velocity(
        self,
        vx: float = 0.0,
        vy: float = 0.0,
        vyaw: float = 0.0,
    ) -> str:
        """Set the G1's commanded velocity. Returns immediately.

        Same contract as go2-motion's set_velocity: brain pings at
        ~10 Hz, watchdog stops the robot if pings cease. On the G1
        in particular, `LocoClient.Move()` is a synchronous service
        call that waits for the robot's velocity controller to ack.
        If the robot is harnessed off the floor (no foot contact)
        or not in a velocity-accepting FSM state, Move() never gets
        an ack and we time out. We swallow that timeout so the
        brain's 10 Hz tick loop and the UI joystick don't see a
        torrent of 500s — the watchdog still arms, so a stalled
        velocity loop still triggers StopMove behaviour on schedule.
        """
        vx = _clamp(vx, MAX_VX)
        vy = _clamp(vy, MAX_VY)
        vyaw = _clamp(vyaw, MAX_VYAW)
        result = "ok"
        try:
            await self._call_sdk("Move", vx, vy, vyaw)
        except asyncio.TimeoutError:
            result = "timeout"  # already logged by _call_sdk
        await self._arm_watchdog(VELOCITY_WATCHDOG_S)
        return f"velocity vx={vx:.2f} vy={vy:.2f} vyaw={vyaw:.2f} ({result})"

    # -- blocking motion (manual / scripted) ------------------------------------

    async def move(
        self,
        vx: float = 0.0,
        vy: float = 0.0,
        vyaw: float = 0.0,
        duration: float = DEFAULT_MOVE_SECONDS,
    ) -> str:
        vx = _clamp(vx, MAX_VX)
        vy = _clamp(vy, MAX_VY)
        vyaw = _clamp(vyaw, MAX_VYAW)
        duration = max(0.1, min(duration, 10.0))

        async with self._move_lock:
            logger.info(
                "Move vx=%.2f vy=%.2f vyaw=%.2f for %.1fs", vx, vy, vyaw, duration
            )
            await self._call_sdk("Move", vx, vy, vyaw)
            await self._arm_watchdog(duration + MOVE_WATCHDOG_SLOP_S)
            await asyncio.sleep(duration)
            await self._call_sdk("StopMove")
        return f"moved vx={vx:.2f} vy={vy:.2f} vyaw={vyaw:.2f} for {duration:.1f}s"

    async def stop(self) -> str:
        async with self._move_lock:
            await self._call_sdk("StopMove")
        self._cancel_watchdog()
        return "stopped"

    # -- postures ---------------------------------------------------------------
    # G1's posture vocabulary: StandUp (from sit/lie), Squat (low
    # crouch), SitDown (full sit), Lie2StandUp (recovery from a fall).
    # Each cancels any pending watchdog so a recently-armed StopMove
    # can't interrupt mid-skill.

    async def stand_up(self) -> str:
        """Wake from Damping / ZeroTorque into BalanceStand.

        On this LocoClient firmware there's no `StandUp` method — the
        wake transition is `Start()`. (Verified by AttributeError when
        we tried `StandUp` on 2026-06-12.) If your firmware uses a
        different name, set MOTION_STAND_METHOD env to override.
        """
        method = os.environ.get("MOTION_STAND_METHOD", "Start")
        self._cancel_watchdog()
        async with self._move_lock:
            await self._call_sdk(method, timeout=SDK_SKILL_TIMEOUT_S)
        return f"standing (via {method})"

    async def squat(self) -> str:
        self._cancel_watchdog()
        async with self._move_lock:
            await self._call_sdk("Squat", timeout=SDK_SKILL_TIMEOUT_S)
        return "squatting"

    async def sit_down(self) -> str:
        self._cancel_watchdog()
        async with self._move_lock:
            await self._call_sdk("SitDown", timeout=SDK_SKILL_TIMEOUT_S)
        return "sitting"

    async def lie_to_stand(self) -> str:
        """Recovery from a fall — same as `/lie` button in go2-RC but
        G1's semantics are reversed (going *from* lying *to* standing)."""
        self._cancel_watchdog()
        async with self._move_lock:
            await self._call_sdk("Lie2StandUp", timeout=SDK_SKILL_TIMEOUT_S)
        return "lie-to-stand"

    async def balance_stand(self, balance_mode: int = 0) -> str:
        """Active balance-stand — the default 'ready to walk' pose.

        `balance_mode` is firmware-specific. 0 = static (safe default);
        1 = dynamic. Required positional arg on G1 LocoClient — we
        learned this from `TypeError: BalanceStand() missing 1
        required positional argument: 'balance_mode'` on 2026-06-12.
        """
        self._cancel_watchdog()
        async with self._move_lock:
            await self._call_sdk(
                "BalanceStand", int(balance_mode),
                timeout=SDK_SKILL_TIMEOUT_S,
            )
        return f"balance-stand mode={int(balance_mode)}"

    async def damp(self) -> str:
        """Soft-stop into damping mode (joints go compliant). The
        safest 'something is wrong' state — used by the watchdog on
        repeated SDK failures."""
        self._cancel_watchdog()
        async with self._move_lock:
            await self._call_sdk("Damp", timeout=SDK_SKILL_TIMEOUT_S)
        return "damping"

    # -- hand gestures (LocoClient arms; NOT Dex3 fingers) ----------------------

    async def wave_hand(self, with_turn: bool = False) -> str:
        """Wave with one arm. `with_turn=True` adds a body turn so the
        wave faces a person off-axis. Built-in to LocoClient; doesn't
        need the Dex3 hand SDK."""
        self._cancel_watchdog()
        async with self._move_lock:
            method = "WaveHandWithTurn" if with_turn else "WaveHand"
            await self._call_sdk(method, timeout=SDK_SKILL_TIMEOUT_S)
        return "waving"

    async def shake_hand(self, stage: int = 0) -> str:
        """Two-stage handshake. `stage=0` extends the arm, `stage=1`
        completes the shake. Caller is responsible for sequencing."""
        self._cancel_watchdog()
        async with self._move_lock:
            await self._call_sdk("ShakeHand", int(stage), timeout=SDK_SKILL_TIMEOUT_S)
        return f"shake stage={stage}"

    # -- arm presets (low-level via arm_sdk) ----------------------------------

    async def arm_preset(
        self,
        preset: str,
        side: str = "both",
        duration: float = 2.0,
        release: bool = False,
    ) -> str:
        """Move one or both arms to a named preset pose.

        Runs the move on a worker thread because g1_arm.G1Arm uses
        blocking sleeps for the interpolation ramp. The watchdog is
        cancelled first to avoid the velocity-zero StopMove firing
        in the middle of an arm motion.
        """
        if self._arm is None:
            raise RuntimeError("Arm controller is not available on this firmware")
        self._cancel_watchdog()
        return await asyncio.to_thread(
            self._arm.move_preset,
            preset=preset, side=side, duration=duration, release=release,
        )

    # -- Dex3 finger control (stubbed in v1) ------------------------------------
    # Per-finger pose control needs Dex3Client + LowCmd_/LowState_ on
    # `rt/dex3/{left,right}/cmd` and `rt/dex3/{left,right}/state`. That's
    # a separate SDK from LocoClient and has its own safety surface
    # (no built-in watchdog for finger motors). Deferred until v2 so
    # the v1 release can stay focused on locomotion + arm gestures.

    async def grip(self, side: str = "right", strength: float = 0.5) -> str:
        raise NotImplementedError(
            "Dex3 finger control not wired in v1 — see g1_controller.py"
        )

    async def release(self, side: str = "right") -> str:
        raise NotImplementedError(
            "Dex3 finger control not wired in v1 — see g1_controller.py"
        )
