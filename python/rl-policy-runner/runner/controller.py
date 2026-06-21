"""On-robot RL policy control loop with an independent safety watchdog.

Generic structure (robot-agnostic):
    load policy (ONNX) -> build observation from robot state -> policy(obs)
    -> clamp/scale action -> apply to robot -> renew watchdog -> repeat @ CONTROL_HZ

Robot-specific bits live in two clearly-marked ADAPTER functions you edit to match
*your* trained policy and robot: ``build_observation`` and ``apply_action``. The
defaults target a Unitree Go2 with an Isaac-Lab-style observation/action layout —
they are almost certainly NOT byte-for-byte correct for your policy; edit them.

SAFETY (this drives a real robot):
- The policy never runs until ``/start`` is called AND ``ENABLE_POLICY=1``.
- An independent ``threading.Timer`` watchdog (its own OS thread) fires a
  *synchronous* stop if the control loop stalls or isn't renewed within
  ``WATCHDOG_S`` — it does not depend on the asyncio loop or the loop thread.
- Actions are clamped to configured limits every tick.
- ``velocity`` mode (SportClient, high-level) is the safe default. ``lowcmd`` mode
  (direct joint control) is gated behind ``ENABLE_LOWCMD=1``, requires sport_mode
  OFF on the robot, and ships as an adapter stub you must complete + verify on a
  stand. Test everything with the robot suspended / e-stop ready.
"""

import os
import threading
import time
import urllib.request

import numpy as np
import onnxruntime as ort

ROBOT = os.environ.get("ROBOT", "go2").lower()
CONTROL_MODE = os.environ.get("CONTROL_MODE", "velocity").lower()  # velocity | lowcmd
CONTROL_HZ = float(os.environ.get("CONTROL_HZ", "50"))
OBS_DIM = int(os.environ.get("OBS_DIM", "48"))
ACT_DIM = int(os.environ.get("ACT_DIM", "12"))
WATCHDOG_S = float(os.environ.get("WATCHDOG_S", "0.5"))
MAX_VX = float(os.environ.get("MAX_VX", "0.5"))
MAX_VY = float(os.environ.get("MAX_VY", "0.3"))
MAX_VYAW = float(os.environ.get("MAX_VYAW", "0.5"))
ACTION_SCALE = float(os.environ.get("ACTION_SCALE", "0.25"))  # lowcmd joint-delta scale
POLICY_PATH = os.environ.get("POLICY_PATH", "/policy/policy.onnx")
POLICY_URL = os.environ.get("POLICY_URL", "").strip()
DDS_ADDR = os.environ.get("GO2_DDS_ADDRESS", "").strip()
IFACE = os.environ.get("GO2_NETWORK_INTERFACE", "eth0")
ENABLE_POLICY = os.environ.get("ENABLE_POLICY", "0") == "1"
ENABLE_LOWCMD = os.environ.get("ENABLE_LOWCMD", "0") == "1"


class PolicyRunner:
    def __init__(self) -> None:
        self._sess: ort.InferenceSession | None = None
        self._in_name = ""
        self._sport = None            # SportClient (velocity mode)
        self._lowcmd_pub = None       # LowCmd publisher (lowcmd mode)
        self._state_lock = threading.Lock()
        self._latest_state = {}       # filled by the DDS lowstate callback
        self._subs = []               # keep subscriber refs alive
        self._loop_thread: threading.Thread | None = None
        self._watchdog: threading.Timer | None = None
        self._wd_gen = 0
        self._running = False
        self._connected = False
        self._last_action = np.zeros(ACT_DIM, dtype=np.float32)
        self._command = np.zeros(3, dtype=np.float32)  # [vx, vy, vyaw] target
        self._status = {"state": "idle", "rate_hz": 0.0, "detail": "", "obs": [], "action": []}

    # ----- policy + connection -------------------------------------------------

    def load_policy(self) -> None:
        if self._sess is not None:
            return
        if POLICY_URL and not os.path.exists(POLICY_PATH):
            os.makedirs(os.path.dirname(POLICY_PATH) or ".", exist_ok=True)
            urllib.request.urlretrieve(POLICY_URL, POLICY_PATH)  # noqa: S310
        if not os.path.exists(POLICY_PATH):
            raise FileNotFoundError(f"no policy at {POLICY_PATH} (set POLICY_URL or mount one)")
        self._sess = ort.InferenceSession(POLICY_PATH, providers=["CPUExecutionProvider"])
        self._in_name = self._sess.get_inputs()[0].name

    def connect(self) -> None:
        if self._connected:
            return
        # Lazy import so the service still starts (and /status works) without the SDK.
        from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelSubscriber
        from unitree_sdk2py.idl.unitree_go.msg.dds_ import LowState_

        _init_dds()
        self._lowstate_sub = ChannelSubscriber("rt/lowstate", LowState_)
        self._lowstate_sub.Init(self._on_lowstate, 10)
        self._subs.append(self._lowstate_sub)

        if CONTROL_MODE == "velocity":
            from unitree_sdk2py.go2.sport.sport_client import SportClient
            self._sport = SportClient()
            self._sport.SetTimeout(3.0)
            self._sport.Init()
        elif CONTROL_MODE == "lowcmd":
            if not ENABLE_LOWCMD:
                raise RuntimeError("CONTROL_MODE=lowcmd requires ENABLE_LOWCMD=1 (and sport_mode OFF on the robot)")
            self._init_lowcmd()
        else:
            raise RuntimeError(f"unknown CONTROL_MODE {CONTROL_MODE!r}")
        self._connected = True

    def _on_lowstate(self, msg) -> None:
        try:
            with self._state_lock:
                self._latest_state = {
                    "rpy": list(msg.imu_state.rpy),
                    "gyro": list(msg.imu_state.gyroscope),
                    "quat": list(msg.imu_state.quaternion),
                    "q": [m.q for m in msg.motor_state[:12]],
                    "dq": [m.dq for m in msg.motor_state[:12]],
                }
        except Exception:  # noqa: BLE001 - never let a parse error kill the subscriber
            pass

    # ----- ADAPTERS (edit these to match your policy) --------------------------

    def build_observation(self) -> np.ndarray:
        """ROBOT/POLICY-SPECIFIC. Build the obs vector your policy was trained on.

        Default = an Isaac-Lab-style Go2 layout: [ang_vel(3), projected_gravity(3),
        command(3), joint_pos(12), joint_vel(12), last_action(12)] = 45, zero-padded
        or truncated to OBS_DIM. EDIT to match your training observation exactly —
        order and scaling must agree or the robot will behave unpredictably.
        """
        with self._state_lock:
            s = dict(self._latest_state)
        if not s:
            return np.zeros(OBS_DIM, dtype=np.float32)
        gyro = np.asarray(s.get("gyro", [0, 0, 0]), dtype=np.float32)
        proj_g = _projected_gravity(s.get("quat", [1, 0, 0, 0]))
        q = np.asarray(s.get("q", [0.0] * 12), dtype=np.float32)
        dq = np.asarray(s.get("dq", [0.0] * 12), dtype=np.float32)
        obs = np.concatenate([gyro, proj_g, self._command, q, dq, self._last_action])
        if obs.shape[0] < OBS_DIM:
            obs = np.concatenate([obs, np.zeros(OBS_DIM - obs.shape[0], dtype=np.float32)])
        return obs[:OBS_DIM].astype(np.float32)

    def apply_action(self, action: np.ndarray) -> None:
        """ROBOT/POLICY-SPECIFIC. Apply the policy output to the robot, clamped."""
        if CONTROL_MODE == "velocity":
            # High-level: interpret the first 3 outputs as a velocity command.
            vx = float(np.clip(action[0], -MAX_VX, MAX_VX))
            vy = float(np.clip(action[1] if ACT_DIM > 1 else 0.0, -MAX_VY, MAX_VY))
            vyaw = float(np.clip(action[2] if ACT_DIM > 2 else 0.0, -MAX_VYAW, MAX_VYAW))
            self._sport.Move(vx, vy, vyaw)  # must be re-sent each tick (firmware times out)
        else:
            # lowcmd: ADAPTER STUB — joint-target policies (e.g. Isaac Lab locomotion)
            # need per-joint kp/kd and a default pose here. Complete + verify on a stand.
            self._apply_lowcmd(action)

    # ----- control loop + safety ----------------------------------------------

    def start(self) -> str:
        if not ENABLE_POLICY:
            return "blocked: set ENABLE_POLICY=1 to allow the policy to drive the robot"
        if self._running:
            return "already running"
        self.load_policy()
        self.connect()
        self._running = True
        self._loop_thread = threading.Thread(target=self._loop, daemon=True)
        self._loop_thread.start()
        return "started"

    def stop(self) -> str:
        self._running = False
        if self._loop_thread:
            self._loop_thread.join(timeout=2.0)
        self._cancel_watchdog()
        self._sync_stop()
        self._status["state"] = "stopped"
        return "stopped"

    def estop(self) -> str:
        self._running = False
        self._cancel_watchdog()
        self._sync_stop()
        self._status.update(state="estop", detail="emergency stop")
        return "estopped"

    def _loop(self) -> None:
        period = 1.0 / CONTROL_HZ
        self._status.update(state="running", detail="")
        try:
            while self._running:
                t0 = time.monotonic()
                obs = self.build_observation()
                action = self._infer(obs)
                self._last_action = action
                self.apply_action(action)
                self._arm_watchdog()  # renew the dead-man timer every tick
                self._status.update(
                    rate_hz=round(1.0 / max(time.monotonic() - t0, 1e-6), 1),
                    obs=[round(float(x), 3) for x in obs[:8]],
                    action=[round(float(x), 3) for x in action[:8]],
                )
                dt = period - (time.monotonic() - t0)
                if dt > 0:
                    time.sleep(dt)
        except Exception as exc:  # noqa: BLE001
            self._status.update(state="error", detail=str(exc))
        finally:
            self._running = False
            self._cancel_watchdog()
            self._sync_stop()  # always stop the robot when the loop exits, for any reason

    def _infer(self, obs: np.ndarray) -> np.ndarray:
        out = self._sess.run(None, {self._in_name: obs.reshape(1, -1)})[0]
        return np.asarray(out, dtype=np.float32).reshape(-1)[:ACT_DIM]

    def _arm_watchdog(self) -> None:
        self._cancel_watchdog()
        self._wd_gen += 1
        gen = self._wd_gen
        t = threading.Timer(WATCHDOG_S, self._sync_stop, kwargs={"gen": gen})
        t.daemon = True
        self._watchdog = t
        t.start()

    def _cancel_watchdog(self) -> None:
        if self._watchdog is not None:
            self._watchdog.cancel()
            self._watchdog = None

    def _sync_stop(self, gen: int | None = None) -> None:
        # Independent of the asyncio loop / control-loop thread. Generation guard so a
        # cancelled-but-already-firing timer no-ops if a newer tick re-armed.
        if gen is not None and gen != self._wd_gen:
            return
        self._running = False
        try:
            if CONTROL_MODE == "velocity" and self._sport is not None:
                self._sport.StopMove()
            elif CONTROL_MODE == "lowcmd":
                self._lowcmd_damping()
        except Exception:  # noqa: BLE001
            pass

    # ----- lowcmd stubs (complete + verify on a stand) -------------------------

    def _init_lowcmd(self) -> None:
        raise NotImplementedError(
            "lowcmd mode: implement the LowCmd publisher + your policy's joint kp/kd and "
            "default pose, then verify on a stand. Joint-target policies (e.g. Isaac Lab) "
            "use this path; it requires sport_mode OFF on the robot."
        )

    def _apply_lowcmd(self, action: np.ndarray) -> None:  # pragma: no cover - stub
        raise NotImplementedError("complete _apply_lowcmd for your robot/policy")

    def _lowcmd_damping(self) -> None:  # pragma: no cover - stub
        # On stop, command a safe damping pose (kp=0, kd>0) rather than zero torque.
        pass

    # ----- status --------------------------------------------------------------

    def status(self) -> dict:
        return {
            "interface": "policy",
            "robot": ROBOT,
            "mode": CONTROL_MODE,
            "enabled": ENABLE_POLICY,
            "policy_loaded": self._sess is not None,
            **self._status,
        }


def _init_dds() -> None:
    """Bind CycloneDDS by IP address (multi-homed Go2 Orin); idempotent."""
    from unitree_sdk2py.core.channel import ChannelFactoryInitialize

    if getattr(_init_dds, "_done", False):
        return
    if DDS_ADDR:
        os.environ["CYCLONEDDS_URI"] = (
            "<CycloneDDS><Domain><General><Interfaces>"
            f'<NetworkInterface address="{DDS_ADDR}"/>'
            "</Interfaces></General></Domain></CycloneDDS>"
        )
        ChannelFactoryInitialize(0)
    else:
        ChannelFactoryInitialize(0, IFACE)
    _init_dds._done = True  # type: ignore[attr-defined]


def _projected_gravity(quat) -> np.ndarray:
    # gravity (0,0,-1) rotated into the base frame from the IMU quaternion (w,x,y,z).
    w, x, y, z = (list(quat) + [1, 0, 0, 0])[:4]
    return np.array(
        [-2 * (x * z - w * y), -2 * (y * z + w * x), -(1 - 2 * (x * x + y * y))],
        dtype=np.float32,
    )
