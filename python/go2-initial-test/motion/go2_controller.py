"""Wraps unitree_sdk2_python's SportClient + LowState subscriber.

Vendored from /demos/go2-motion/go2_controller.py. Clamps to safety velocity
caps; every SDK call runs off the event loop with a timeout, and a watchdog
fires StopMove() if a velocity command isn't renewed.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import threading
import time
from typing import Any, Optional

logger = logging.getLogger("go2-motion")


MAX_VX = 0.6      # m/s
MAX_VY = 0.4      # m/s
MAX_VYAW = 1.0    # rad/s
DEFAULT_MOVE_SECONDS = 2.0
MOVE_WATCHDOG_SLOP_S = 0.5
VELOCITY_WATCHDOG_S = 1.0
SDK_CALL_TIMEOUT_S = float(os.environ.get("MOTION_SDK_CALL_TIMEOUT_S", "0.5"))
SDK_SKILL_TIMEOUT_S = float(os.environ.get("MOTION_SDK_SKILL_TIMEOUT_S", "5.0"))

# ChannelFactoryInitialize is process-global and one-shot; track it separately
# from "client ready" so a failed SportClient.Init() doesn't make a retry call
# ChannelFactoryInitialize twice ("already initialized" wedge).
_factory_initialized = False


def _clamp(value: float, limit: float) -> float:
    return max(-limit, min(limit, value))


def _resolve_dds_address(robot_ip: str) -> str:
    """Local IP this host uses to reach the Go2 — the address CycloneDDS must bind
    to (the Orin is multi-homed). GO2_DDS_ADDRESS overrides; otherwise ask the
    kernel which source IP routes to the robot (no packets sent, never blocks).
    Returns "" off-robot (no route)."""
    override = os.environ.get("GO2_DDS_ADDRESS", "").strip()
    if override:
        return override
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((robot_ip, 1))  # no traffic; the kernel just picks the route
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return ""


class Go2Controller:
    def __init__(self, network_interface: Optional[str] = None) -> None:
        self._network_interface = network_interface or os.environ.get(
            "GO2_NETWORK_INTERFACE", "eth0"
        )
        self._sport_client = None
        self._lowstate_sub = None
        self._sport_sub = None
        self._move_lock = asyncio.Lock()
        self._watchdog: Optional[threading.Timer] = None
        self._watchdog_gen = 0
        self._state_lock = threading.Lock()
        self._latest_state: dict[str, Any] = {}
        self._speed: Optional[float] = None   # planar body speed (m/s) from sportmodestate
        self._speed_ts = 0.0

    def connect(self) -> None:
        from unitree_sdk2py.core.channel import (
            ChannelFactoryInitialize,
            ChannelSubscriber,
        )
        from unitree_sdk2py.go2.sport.sport_client import SportClient
        from unitree_sdk2py.idl.unitree_go.msg.dds_ import LowState_, SportModeState_

        # Prefer binding DDS to a specific local IP (the NIC that reaches the
        # robot controller). On a multi-homed Jetson — e.g. eth1 holds both
        # 192.168.100.x and 192.168.123.x — selecting by interface NAME is
        # ambiguous and CycloneDDS may advertise the wrong address, so the robot
        # never hears us. Binding by ADDRESS is unambiguous.
        global _factory_initialized
        if not _factory_initialized:
            dds_addr = _resolve_dds_address(os.environ.get("GO2_IP", "192.168.123.161"))
            if dds_addr:
                os.environ["CYCLONEDDS_URI"] = (
                    "<CycloneDDS><Domain><General><Interfaces>"
                    f'<NetworkInterface address="{dds_addr}"/>'
                    "</Interfaces></General></Domain></CycloneDDS>"
                )
                logger.info("Initializing DDS bound to address %s", dds_addr)
                ChannelFactoryInitialize(0)
            else:
                logger.info("Initializing DDS on interface %s", self._network_interface)
                ChannelFactoryInitialize(0, self._network_interface)
            _factory_initialized = True

        client = SportClient()
        client.SetTimeout(3.0)
        client.Init()
        self._sport_client = client
        logger.info("SportClient ready")

        sub = ChannelSubscriber("rt/lowstate", LowState_)
        sub.Init(self._on_lowstate, 10)
        self._lowstate_sub = sub

        # Body velocity (for verified-stop) comes from sportmodestate.
        sport = ChannelSubscriber("rt/sportmodestate", SportModeState_)
        sport.Init(self._on_sportstate, 10)
        self._sport_sub = sport
        logger.info("Subscribed to rt/lowstate + rt/sportmodestate")

    def _on_sportstate(self, msg: Any) -> None:
        try:
            vx, vy = float(msg.velocity[0]), float(msg.velocity[1])
            speed = (vx * vx + vy * vy) ** 0.5
        except Exception:  # noqa: BLE001
            return
        with self._state_lock:
            self._speed = speed
            self._speed_ts = time.time()

    def _on_lowstate(self, msg: Any) -> None:
        try:
            rpy = list(msg.imu_state.rpy) if msg.imu_state.rpy else [0.0, 0.0, 0.0]
            state = {
                "battery_soc": int(msg.bms_state.soc),
                "power_v": float(msg.power_v),
                "imu_rpy": rpy,
                "foot_force": list(msg.foot_force),
                "tick": int(msg.tick),
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to parse LowState: %s", exc)
            return
        with self._state_lock:
            self._latest_state = state

    def latest_state(self) -> dict[str, Any]:
        with self._state_lock:
            return dict(self._latest_state)

    def _require_client(self):
        if self._sport_client is None:
            raise RuntimeError("Go2Controller.connect() was not called")
        return self._sport_client

    async def _call_sdk(self, method_name: str, *args, timeout: float = SDK_CALL_TIMEOUT_S) -> None:
        client = self._require_client()
        method = getattr(client, method_name)
        try:
            await asyncio.wait_for(asyncio.to_thread(method, *args), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("SportClient.%s%s timed out after %.2fs", method_name, args, timeout)
            raise

    def _arm_watchdog(self, seconds: float) -> None:
        # OS-level timer thread that fires a SYNCHRONOUS StopMove — independent of
        # the asyncio loop + thread pool it would otherwise share with the move it
        # guards (a watchdog that fails in the same conditions as the thing it
        # protects isn't a safety stop). The firmware's own Move timeout is the
        # primary guard; this is the backup. Renewed on each velocity command.
        self._cancel_watchdog()
        gen = self._watchdog_gen
        t = threading.Timer(seconds, self._sync_stop, args=(gen,))
        t.daemon = True
        self._watchdog = t
        t.start()

    def _sync_stop(self, gen: int) -> None:
        # threading.Timer.cancel() can't stop a timer that's already firing, so a
        # generation check makes a superseded timer no-op (prevents a stutter-stop
        # when a renewal lands at the same instant the old timer fires).
        if gen != self._watchdog_gen:
            return
        try:
            if self._sport_client is not None:
                logger.info("Watchdog firing; StopMove (sync, off-loop)")
                self._sport_client.StopMove()
        except Exception as exc:  # noqa: BLE001
            logger.warning("watchdog StopMove failed: %s", exc)

    def _cancel_watchdog(self) -> None:
        self._watchdog_gen += 1  # invalidate any timer counting down OR already firing
        if self._watchdog is not None:
            self._watchdog.cancel()
            self._watchdog = None

    async def set_velocity(self, vx: float = 0.0, vy: float = 0.0, vyaw: float = 0.0) -> str:
        # NOTE: mutually exclusive with move() — set_velocity does NOT take
        # _move_lock (it's the 10 Hz dead-man path). Don't call both concurrently;
        # the go2-initial-test walk test uses move()/stop() only, not set_velocity.
        vx, vy, vyaw = _clamp(vx, MAX_VX), _clamp(vy, MAX_VY), _clamp(vyaw, MAX_VYAW)
        await self._call_sdk("Move", vx, vy, vyaw)
        self._arm_watchdog(VELOCITY_WATCHDOG_S)
        return f"velocity vx={vx:.2f} vy={vy:.2f} vyaw={vyaw:.2f}"

    async def move(self, vx: float = 0.0, vy: float = 0.0, vyaw: float = 0.0,
                   duration: float = DEFAULT_MOVE_SECONDS) -> str:
        vx, vy, vyaw = _clamp(vx, MAX_VX), _clamp(vy, MAX_VY), _clamp(vyaw, MAX_VYAW)
        duration = max(0.1, min(duration, 10.0))
        async with self._move_lock:
            logger.info("Move vx=%.2f vy=%.2f vyaw=%.2f for %.1fs", vx, vy, vyaw, duration)
            await self._call_sdk("Move", vx, vy, vyaw)
            self._arm_watchdog(duration + MOVE_WATCHDOG_SLOP_S)
            await asyncio.sleep(duration)
            await self._call_sdk("StopMove")
        return f"moved vx={vx:.2f} vy={vy:.2f} vyaw={vyaw:.2f} for {duration:.1f}s"

    async def stop(self) -> str:
        # Safety stop must NOT queue behind the move it's stopping, so it does
        # NOT take _move_lock (which move() holds for its whole duration). Retry
        # with the longer skill timeout; raise if every attempt fails (a timed-out
        # stop reported "stopped" is dangerous).
        self._cancel_watchdog()
        last = None
        for attempt in range(3):
            try:
                await self._call_sdk("StopMove", timeout=SDK_SKILL_TIMEOUT_S)
            except Exception as e:  # noqa: BLE001
                last = e
                logger.warning("StopMove attempt %d failed: %s", attempt + 1, e)
                continue
            # Verify the robot actually reached ~0 velocity — an SDK call that
            # returns success but doesn't halt must not be reported "stopped".
            v = await self._verify_stopped()
            if v == "confirmed":
                return "stopped (velocity ≈0 confirmed)"
            if v == "unknown":
                return "stop sent (no velocity feedback to verify)"
            last = RuntimeError("velocity did not reach ~0 after StopMove")
            logger.warning("velocity not ~0 after StopMove (attempt %d)", attempt + 1)
        raise last or RuntimeError("StopMove failed")

    async def _verify_stopped(self, timeout: float = 2.0, thresh: float = 0.05) -> str:
        """Poll body speed (from sportmodestate) after StopMove. Returns
        'confirmed' (≈0 seen), 'high' (fresh data but still moving), or 'unknown'
        (no fresh velocity feedback to judge by)."""
        saw = False
        end = time.time() + timeout
        while time.time() < end:
            with self._state_lock:
                sp, ts = self._speed, self._speed_ts
            if sp is not None and (time.time() - ts) < 1.0:
                saw = True
                if sp <= thresh:
                    return "confirmed"
            await asyncio.sleep(0.1)
        return "high" if saw else "unknown"

    async def stand_up(self) -> str:
        self._cancel_watchdog()
        async with self._move_lock:
            await self._call_sdk("StandUp", timeout=SDK_SKILL_TIMEOUT_S)
        return "standing"

    async def lie_down(self) -> str:
        self._cancel_watchdog()
        async with self._move_lock:
            await self._call_sdk("StandDown", timeout=SDK_SKILL_TIMEOUT_S)
        return "lying down"

    async def skill(self, name: str, *args) -> str:
        """Run any named SportClient skill (BalanceStand, RecoveryStand, Euler,
        BodyHeight, FootRaiseHeight, FrontFlip, FrontJump, …) with the skill timeout."""
        self._cancel_watchdog()
        async with self._move_lock:
            await self._call_sdk(name, *args, timeout=SDK_SKILL_TIMEOUT_S)
        return name
