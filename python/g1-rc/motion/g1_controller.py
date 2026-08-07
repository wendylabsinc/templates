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
  - Battery state is not embedded in G1 LowState. It is published as
    `unitree_hg.msg.BmsState_` on `rt/lf/bmsstate`.

Public API mirrors `go2_controller.Go2Controller` so `main.py` shape
is preserved.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import ipaddress
import logging
import os
import socket
import struct
import threading
import time
from typing import Any, Optional

logger = logging.getLogger("g1-motion")


ROBOT_NETWORK = ipaddress.ip_network("192.168.123.0/24")
SIOCGIFADDR = 0x8915


def _interface_ipv4_addresses() -> dict[str, str]:
    """Return the host-network interfaces that currently have IPv4 addresses."""
    addresses: dict[str, str] = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        for _, name in socket.if_nameindex():
            try:
                request = struct.pack("256s", name.encode("utf-8")[:15])
                response = fcntl.ioctl(sock.fileno(), SIOCGIFADDR, request)
            except OSError:
                continue
            addresses[name] = socket.inet_ntoa(response[20:24])
    return addresses


def _choose_network_interface(
    configured: Optional[str], addresses: dict[str, str]
) -> str:
    """Resolve ``auto`` to the NIC attached to the G1's robot bus."""
    configured = (configured or "auto").strip()
    if configured.lower() != "auto":
        return configured

    for name, raw_address in addresses.items():
        try:
            if ipaddress.ip_address(raw_address) in ROBOT_NETWORK:
                return name
        except ValueError:
            continue

    visible = ", ".join(f"{name}={addr}" for name, addr in addresses.items())
    raise RuntimeError(
        "NETWORK_INTERFACE=auto could not find an interface on "
        f"{ROBOT_NETWORK}. Visible IPv4 interfaces: {visible or 'none'}. "
        "Set NETWORK_INTERFACE to the G1 robot-bus interface explicitly."
    )


MAX_VX = 0.6      # m/s
MAX_VY = 0.4      # m/s
MAX_VYAW = 1.0    # rad/s
DEFAULT_MOVE_SECONDS = 2.0
MOVE_WATCHDOG_SLOP_S = 0.5
VELOCITY_WATCHDOG_S = 1.0
# Same rationale as go2-motion: every LocoClient call goes through a
# thread + wait_for so a stuck DDS write can't deadlock the asyncio
# loop. Healthy writes return in a few ms.
SDK_CALL_TIMEOUT_S = float(os.environ.get("MOTION_SDK_CALL_TIMEOUT_S", "0.75"))
# Posture skills (StandUp / Squat / SitDown / Lie2StandUp / WaveHand /
# ShakeHand) trigger an internal FSM transition and routinely take
# 1-5 s. Use a longer timeout.
SDK_SKILL_TIMEOUT_S = float(os.environ.get("MOTION_SDK_SKILL_TIMEOUT_S", "8.0"))

# Native stand-up FSM ids — the sequence this G1 firmware actually
# requires (verified on hardware by wendy-studio-hermes-voice and
# g1-upper-control; `Start()`/`BalanceStand()` do NOT work there):
#
#     StopMove → select "ai" motion mode → DAMP (1) → LOCK STAND (4)
#                                                   → RUNNING (801)
#
# LOCK STAND is the standing pose; RUNNING is the walk-ready policy.
DAMP_FSM = int(os.environ.get("G1_LOCO_DAMP_FSM_ID", "1"))
LOCK_STAND_FSM = int(os.environ.get("G1_LOCO_LOCK_STAND_FSM_ID", "4"))
RUNNING_FSM = int(os.environ.get("G1_LOCO_RUNNING_FSM_ID", "801"))
FSM_WAIT_S = float(os.environ.get("G1_LOCO_FSM_WAIT_S", "12.0"))
FSM_RPC_TIMEOUT_S = float(os.environ.get("G1_LOCO_FSM_RPC_TIMEOUT_S", "0.5"))
FSM_POLL_INTERVAL_S = float(os.environ.get("G1_LOCO_FSM_POLL_INTERVAL_S", "0.25"))
FSM_MAX_AGE_S = float(os.environ.get("G1_LOCO_FSM_MAX_AGE_S", "2.0"))
BMS_STATE_TOPIC = os.environ.get("G1_BMS_STATE_TOPIC", "rt/lf/bmsstate")


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
        self._network_interface_setting = network_interface or os.environ.get(
            "G1_NETWORK_INTERFACE", "auto"
        )
        self._network_interface: Optional[str] = None
        self._loco_client = None
        self._switcher = None
        self._estop = threading.Event()
        self._lowstate_sub = None
        self._bmsstate_sub = None
        self._move_lock = asyncio.Lock()
        self._rpc_lock = asyncio.Lock()
        self._watchdog: Optional[asyncio.Task] = None
        self._fsm_monitor_task: Optional[asyncio.Task] = None
        self._latest_fsm_id: Optional[int] = None
        self._latest_fsm_at = 0.0
        self._latest_fsm_error: Optional[str] = "FSM readback has not started"
        self._last_fsm_error_logged: Optional[str] = None
        self._last_fsm_error_log_at = 0.0
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
        from unitree_sdk2py.idl.unitree_hg.msg.dds_ import BmsState_, LowState_

        self._network_interface = _choose_network_interface(
            self._network_interface_setting,
            _interface_ipv4_addresses(),
        )
        logger.info("Initializing DDS on interface %s", self._network_interface)
        ChannelFactoryInitialize(0, self._network_interface)

        client = LocoClient()
        # Failed Unitree RPCs return code 3104 only after this timeout.
        # Keep FSM monitoring responsive; long-running posture transitions
        # are implemented as short SetFsmId calls plus explicit polling.
        client.SetTimeout(FSM_RPC_TIMEOUT_S)
        client.Init()
        self._loco_client = client
        logger.info("LocoClient ready")

        # MotionSwitcher selects the "ai" motion mode — a prerequisite
        # for the DAMP → LOCK STAND → RUNNING stand-up sequence on the
        # firmware this was verified against. Degrade gracefully: the
        # FSM sequence still runs without it.
        try:
            from unitree_sdk2py.comm.motion_switcher.motion_switcher_client import (
                MotionSwitcherClient,
            )
            sw = MotionSwitcherClient()
            sw.SetTimeout(5.0)
            sw.Init()
            self._switcher = sw
        except Exception as exc:
            logger.warning("MotionSwitcher unavailable (ai-mode select off): %s", exc)


        sub = ChannelSubscriber("rt/lowstate", LowState_)
        sub.Init(self._on_lowstate, 10)
        self._lowstate_sub = sub
        logger.info("Subscribed to rt/lowstate (unitree_hg.LowState_)")

        bms_sub = ChannelSubscriber(BMS_STATE_TOPIC, BmsState_)
        bms_sub.Init(self._on_bmsstate, 10)
        self._bmsstate_sub = bms_sub
        logger.info(
            "Subscribed to %s (unitree_hg.BmsState_)",
            BMS_STATE_TOPIC,
        )

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
            # G1's HighFreqState equivalent. Battery state is a separate
            # DDS topic and is merged by _on_bmsstate.
            foot_force = list(getattr(msg, "foot_force", []) or [])
            tick = int(getattr(msg, "tick", 0))
            state = {
                "imu_rpy": rpy,
                "foot_force": foot_force,
                "tick": tick,
            }
        except Exception as exc:
            logger.warning("Failed to parse LowState: %s", exc)
            return
        with self._state_lock:
            self._latest_state.update(state)

    def _on_bmsstate(self, msg: Any) -> None:
        """Merge a real G1 battery sample into the public robot state."""
        try:
            soc = int(msg.soc)
            if not 0 <= soc <= 100:
                raise ValueError(f"battery SOC out of range: {soc}")
            state = {"battery_soc": soc}
        except Exception as exc:
            logger.warning("Failed to parse BmsState: %s", exc)
            return
        with self._state_lock:
            self._latest_state.update(state)

    def latest_state(self) -> dict[str, Any]:
        with self._state_lock:
            return dict(self._latest_state)

    def _fresh_fsm(self) -> tuple[Optional[int], float, Optional[str]]:
        age = time.monotonic() - self._latest_fsm_at
        if self._latest_fsm_id is None or age > FSM_MAX_AGE_S:
            return None, age, self._latest_fsm_error or "FSM readback is stale"
        return self._latest_fsm_id, age, self._latest_fsm_error

    async def _fsm_monitor(self) -> None:
        while True:
            if self._move_lock.locked():
                await asyncio.sleep(FSM_POLL_INTERVAL_S)
                continue
            try:
                async with self._rpc_lock:
                    fsm = await asyncio.to_thread(self._get_fsm_sync)
                self._latest_fsm_id = fsm
                self._latest_fsm_at = time.monotonic()
                self._latest_fsm_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._latest_fsm_error = str(exc)
                now = time.monotonic()
                if (
                    self._latest_fsm_error != self._last_fsm_error_logged
                    or now - self._last_fsm_error_log_at >= 15.0
                ):
                    logger.warning(
                        "fsm_readback_failed error=%s hint=%s",
                        self._latest_fsm_error,
                        "Check the G1 locomotion service and normal control mode.",
                    )
                    self._last_fsm_error_logged = self._latest_fsm_error
                    self._last_fsm_error_log_at = now
            await asyncio.sleep(FSM_POLL_INTERVAL_S)

    async def start_fsm_monitor(self) -> None:
        if self._fsm_monitor_task is None or self._fsm_monitor_task.done():
            self._fsm_monitor_task = asyncio.create_task(self._fsm_monitor())

    async def stop_fsm_monitor(self) -> None:
        if self._fsm_monitor_task is None:
            return
        self._fsm_monitor_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._fsm_monitor_task
        self._fsm_monitor_task = None

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
            async with self._rpc_lock:
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
        # Walking only actuates while the background monitor has a recent
        # RUNNING readback. Never put an FSM RPC in this 10 Hz path: a
        # Unitree 3104 timeout can take seconds and otherwise stacks calls.
        fsm, age, fsm_error = self._fresh_fsm()
        if fsm is None:
            return (
                f"ignored: FSM unavailable or stale (age={age:.1f}s; "
                f"{fsm_error or 'no readback'})"
            )
        if fsm != RUNNING_FSM:
            return (f"ignored: walking needs RUNNING({RUNNING_FSM}), "
                    f"current FSM is {fsm} — press ready-to-walk first")
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

    # The stand-up path uses the native FSM sequence (DAMP → LOCK STAND
    # → RUNNING) rather than `Start()`/`BalanceStand()`: the latter were
    # verified NOT to work on the firmware this template was developed
    # against (g1-upper-control/locomotion.py, hermes deployment).

    def _get_fsm_sync(self) -> int:
        client = self._require_client()
        res = client.GetFsmId()
        if isinstance(res, (tuple, list)):
            code, val = res[0], res[1]
            if code != 0 or val is None:
                raise RuntimeError(f"GetFsmId failed (code={code})")
            return int(val)
        return int(res)

    def _wait_fsm_sync(self, target: int, timeout: float = FSM_WAIT_S) -> int:
        deadline = time.monotonic() + timeout
        last = -1
        while time.monotonic() < deadline:
            last = self._get_fsm_sync()
            if last == target:
                return last
            time.sleep(0.3)
        return last

    def _select_ai_sync(self) -> None:
        if self._switcher is None:
            return
        self._switcher.SelectMode("ai")
        time.sleep(0.5)

    def _stand_fsm_sync(self, force: bool) -> dict[str, Any]:
        """StopMove → ai mode → DAMP(1) → LOCK STAND(4). LOCK STAND is
        standing. From an UNEXPECTED FSM this must pass through DAMP,
        which collapses an upright robot — refuse unless force=True
        (operator has confirmed crane support)."""
        client = self._require_client()
        try:
            client.StopMove()
            self._select_ai_sync()
            fsm = self._get_fsm_sync()
            if fsm == RUNNING_FSM:
                return {"ok": True, "fsm_id": fsm, "standing": True,
                        "note": "already running"}
            if fsm not in (DAMP_FSM, LOCK_STAND_FSM):
                if not force:
                    logger.warning(
                        "stand_refused fsm_id=%s reason=force_required",
                        fsm,
                    )
                    return {"ok": False, "fsm_id": fsm,
                            "error": f"unexpected FSM {fsm}; DAMP from here can "
                                     "collapse the robot if it is not "
                                     "crane-supported. Retry with force=true."}
                client.SetFsmId(DAMP_FSM)
                fsm = self._wait_fsm_sync(DAMP_FSM)
            if fsm == DAMP_FSM:
                client.SetFsmId(LOCK_STAND_FSM)
                fsm = self._wait_fsm_sync(LOCK_STAND_FSM)
            ok = fsm == LOCK_STAND_FSM
            if ok:
                self._latest_fsm_id = fsm
                self._latest_fsm_at = time.monotonic()
                self._latest_fsm_error = None
            return {"ok": ok, "fsm_id": fsm, "standing": ok}
        except Exception as exc:
            logger.exception("stand_failed error=%s", exc)
            try:
                client.StopMove()
            except Exception:
                pass
            return {"ok": False, "error": str(exc)}

    def _enter_running_sync(self) -> dict[str, Any]:
        """LOCK STAND(4) → RUNNING(801), with one retry (per hermes)."""
        client = self._require_client()
        try:
            fsm = self._get_fsm_sync()
            if fsm == RUNNING_FSM:
                return {"ok": True, "fsm_id": fsm, "ready_to_walk": True,
                        "note": "already running"}
            if fsm != LOCK_STAND_FSM:
                logger.warning(
                    "running_refused fsm_id=%s reason=not_lock_stand",
                    fsm,
                )
                return {"ok": False, "fsm_id": fsm,
                        "error": "not in LOCK STAND; POST /stand first"}
            client.SetFsmId(RUNNING_FSM)
            fsm = self._wait_fsm_sync(RUNNING_FSM)
            if fsm != RUNNING_FSM and self._get_fsm_sync() == LOCK_STAND_FSM:
                client.SetFsmId(RUNNING_FSM)  # one retry from stable LOCK STAND
                fsm = self._wait_fsm_sync(RUNNING_FSM)
            ok = fsm == RUNNING_FSM
            if ok:
                self._latest_fsm_id = fsm
                self._latest_fsm_at = time.monotonic()
                self._latest_fsm_error = None
            return {"ok": ok, "fsm_id": fsm, "ready_to_walk": ok}
        except Exception as exc:
            logger.exception("running_failed error=%s", exc)
            return {"ok": False, "error": str(exc)}

    async def stand_up(self, force: bool = False) -> dict[str, Any]:
        """Stand via the native FSM: DAMP → LOCK STAND."""
        self._cancel_watchdog()
        async with self._move_lock:
            async with self._rpc_lock:
                return await asyncio.to_thread(self._stand_fsm_sync, force)

    async def enter_running(self) -> dict[str, Any]:
        """LOCK STAND → RUNNING: the walk-ready policy. Velocity
        commands only actuate in this state."""
        self._cancel_watchdog()
        async with self._move_lock:
            async with self._rpc_lock:
                return await asyncio.to_thread(self._enter_running_sync)

    async def fsm_status(self) -> dict[str, Any]:
        if self._loco_client is None:
            return {"connected": False, "estop_latched": self._estop.is_set()}
        fsm, age, error = self._fresh_fsm()
        status = {
            "connected": True,
            "fsm_id": fsm,
            "fsm_age_seconds": round(age, 2),
            "fsm_fresh": fsm is not None,
            "standing": fsm in (LOCK_STAND_FSM, RUNNING_FSM),
            "ready_to_walk": fsm == RUNNING_FSM,
            "estop_latched": self._estop.is_set(),
        }
        if fsm is None and error:
            status["error"] = error
        return status

    # -- e-stop (latching) -------------------------------------------------------

    def estop_latched(self) -> bool:
        return self._estop.is_set()

    async def estop(self) -> dict[str, Any]:
        """Latching soft e-stop: halt walking, fade the arms back to the
        balance controller, and refuse all motion until cleared. Soft on
        purpose — a hard torque cut would drop the robot; the wireless
        remote's e-stop remains the primary hardware stop."""
        self._estop.set()
        self._cancel_watchdog()
        try:
            await self._call_sdk("StopMove")
        except Exception:
            pass
        if self._arm is not None:
            try:
                await asyncio.to_thread(self._arm._release, 0.5)
            except Exception:
                logger.exception("estop: arm release failed")
        logger.warning("E-STOP latched: walking stopped, arms released")
        return {"ok": True, "estop_latched": True}

    async def clear_estop(self) -> dict[str, Any]:
        self._estop.clear()
        logger.info("E-stop cleared")
        return {"ok": True, "estop_latched": False}

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

    def _damp_sync(self) -> dict[str, Any]:
        client = self._require_client()
        client.StopMove()
        try:
            client.SetFsmId(DAMP_FSM)
            fsm = self._wait_fsm_sync(DAMP_FSM)
            return {"ok": fsm == DAMP_FSM, "fsm_id": fsm}
        except Exception:
            # Older firmwares expose Damp() instead of the FSM id.
            client.Damp()
            return {"ok": True, "note": "via Damp()"}

    async def damp(self) -> dict[str, Any]:
        """Soft-stop into damping mode (joints go compliant). The robot
        will collapse if it is standing unsupported — the caller is
        responsible for confirming crane support."""
        self._cancel_watchdog()
        async with self._move_lock:
            async with self._rpc_lock:
                return await asyncio.to_thread(self._damp_sync)

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
