"""Wraps unitree_sdk2_python's SportClient + LowState subscriber.

Two motion APIs:

  - `move(vx, vy, vyaw, duration)` — blocking. Useful for one-shot CLI
    commands ("walk forward 2 m"). Sends Move(), sleeps duration, sends
    StopMove(). Watchdog at duration + 0.5 s.

  - `set_velocity(vx, vy, vyaw)` — non-blocking. Sends Move() and arms a
    1 s watchdog that fires StopMove() if no further set_velocity comes
    in. Designed for the brain's 10 Hz tick loop.

Both clamp to safety velocity caps. Both surface the dog's LowState
(battery, IMU, foot forces) via `latest_state()` for /state.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import Any, Optional

logger = logging.getLogger("go2-motion")


MAX_VX = 0.6      # m/s
MAX_VY = 0.4      # m/s
MAX_VYAW = 1.0    # rad/s
DEFAULT_MOVE_SECONDS = 2.0
MOVE_WATCHDOG_SLOP_S = 0.5
VELOCITY_WATCHDOG_S = 1.0
# SportClient calls are sync (DDS writes underneath). If one hangs (DDS
# deadlock, controller backlog), it blocks the entire async event loop
# — uvicorn stops accepting requests, brain piles up timeouts. We run
# every SDK call in a thread + wait_for so a stuck call returns control
# to the event loop within SDK_CALL_TIMEOUT_S and the next request can
# proceed. Healthy calls return in a few ms; the timeout is just a
# safety net.
SDK_CALL_TIMEOUT_S = float(os.environ.get("MOTION_SDK_CALL_TIMEOUT_S", "0.5"))
# Skills (Sit/StandUp/StandDown/Hello/Dance1) trigger an internal mode
# switch on the dog and routinely take 1–3 s to return. Use a longer
# timeout for those so a healthy posture command isn't aborted.
SDK_SKILL_TIMEOUT_S = float(os.environ.get("MOTION_SDK_SKILL_TIMEOUT_S", "5.0"))


def _clamp(value: float, limit: float) -> float:
    return max(-limit, min(limit, value))


class Go2Controller:
    def __init__(self, network_interface: Optional[str] = None) -> None:
        self._network_interface = network_interface or os.environ.get(
            "GO2_NETWORK_INTERFACE", "eth0"
        )
        self._sport_client = None
        self._lowstate_sub = None
        self._move_lock = asyncio.Lock()
        self._watchdog: Optional[asyncio.Task] = None
        self._state_lock = threading.Lock()
        self._latest_state: dict[str, Any] = {}

    def connect(self) -> None:
        from unitree_sdk2py.core.channel import (
            ChannelFactoryInitialize,
            ChannelSubscriber,
        )
        from unitree_sdk2py.go2.sport.sport_client import SportClient
        from unitree_sdk2py.idl.unitree_go.msg.dds_ import LowState_

        logger.info("Initializing DDS on interface %s", self._network_interface)
        ChannelFactoryInitialize(0, self._network_interface)

        client = SportClient()
        client.SetTimeout(3.0)
        client.Init()
        self._sport_client = client
        logger.info("SportClient ready")

        sub = ChannelSubscriber("rt/lowstate", LowState_)
        sub.Init(self._on_lowstate, 10)
        self._lowstate_sub = sub
        logger.info("Subscribed to rt/lowstate")

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
        except Exception as exc:
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
        """Run a SportClient method off the event loop with a timeout.

        Healthy DDS writes return in ~ms; a stuck one would block the
        whole event loop if called inline. asyncio.to_thread frees the
        loop; wait_for caps the wait so we fast-fail and let other
        requests through. The thread itself can't be cancelled (sync
        code), but its result is ignored on timeout — leaks a thread
        per stuck call, which is acceptable because SDK hangs are rare
        and self-resolve when DDS unsticks."""
        client = self._require_client()
        method = getattr(client, method_name)
        try:
            await asyncio.wait_for(
                asyncio.to_thread(method, *args),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "SportClient.%s%s timed out after %.2fs (SDK stuck — "
                "next request will retry)",
                method_name, args, timeout,
            )
            raise

    async def _arm_watchdog(self, seconds: float) -> None:
        self._cancel_watchdog()

        async def _stop_after():
            try:
                await asyncio.sleep(seconds)
                logger.info("Watchdog firing after %.2f s; stopping dog", seconds)
                # Use the wrapped helper so even the watchdog's stop
                # can't deadlock the event loop on a stuck DDS write.
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
        """Set the dog's commanded velocity. Returns immediately.

        Brain calls this at ~10 Hz. The watchdog stops the dog within
        `VELOCITY_WATCHDOG_S` if no renewal arrives — so a brain crash
        or a network drop can't leave the dog walking.
        """
        vx = _clamp(vx, MAX_VX)
        vy = _clamp(vy, MAX_VY)
        vyaw = _clamp(vyaw, MAX_VYAW)
        await self._call_sdk("Move", vx, vy, vyaw)
        await self._arm_watchdog(VELOCITY_WATCHDOG_S)
        return f"velocity vx={vx:.2f} vy={vy:.2f} vyaw={vyaw:.2f}"

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

    # -- skills -----------------------------------------------------------------
    # Each skill cancels any pending velocity watchdog first — otherwise a
    # recently-armed StopMove() will fire mid-skill and interrupt it.

    async def stand_up(self) -> str:
        self._cancel_watchdog()
        async with self._move_lock:
            await self._call_sdk("StandUp", timeout=SDK_SKILL_TIMEOUT_S)
        return "standing"

    async def sit(self) -> str:
        self._cancel_watchdog()
        async with self._move_lock:
            await self._call_sdk("Sit", timeout=SDK_SKILL_TIMEOUT_S)
        return "sitting"

    async def lie_down(self) -> str:
        self._cancel_watchdog()
        async with self._move_lock:
            await self._call_sdk("StandDown", timeout=SDK_SKILL_TIMEOUT_S)
        return "lying down"

    async def hello(self) -> str:
        self._cancel_watchdog()
        async with self._move_lock:
            await self._call_sdk("Hello", timeout=SDK_SKILL_TIMEOUT_S)
        return "waving"

    async def dance(self) -> str:
        self._cancel_watchdog()
        async with self._move_lock:
            await self._call_sdk("Dance1", timeout=SDK_SKILL_TIMEOUT_S)
        return "dancing"
