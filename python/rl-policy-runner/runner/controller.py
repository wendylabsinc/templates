"""On-robot RL policy control loop — wired for the NVIDIA Isaac Lab Unitree Go2
velocity locomotion policy (``Isaac-Velocity-Flat-Unitree-Go2-v0``), with an
independent safety watchdog.

    load ONNX policy -> build Isaac-Lab observation from rt/lowstate -> policy(obs)
    -> target_q = default_pose + action*0.25 -> publish rt/lowcmd (PD) @ 50 Hz
       (+ independent threading.Timer watchdog firing a synchronous damping stop)

The Isaac-Lab convention is baked in as named constants below (obs layout, action
scale, default pose, PD gains, and the Isaac<->Unitree-SDK joint-order permutation).
Items marked [VERIFY] should be checked against YOUR exported ``env.yaml`` and with a
one-joint twitch test before any free-standing run — a wrong gain or permutation is
dangerous on hardware.

SAFETY (drives a real robot):
- Never runs until ``/start`` AND ``ENABLE_POLICY=1``; ``lowcmd`` also needs
  ``ENABLE_LOWCMD=1`` and the robot's high-level ``sport_mode`` turned OFF.
- Independent ``threading.Timer`` watchdog (its own OS thread) fires a synchronous
  damping stop if the loop stalls / isn't renewed within ``WATCHDOG_S``.
- Joint targets are clamped to ``JOINT_DELTA_MAX`` from the default pose every tick.
- The loop always issues a damping stop on exit/error/disconnect.
- FIRST RUNS ON A STAND / SUSPENDED, e-stop ready.
"""

import os
import threading
import time
import urllib.request

import numpy as np
import onnxruntime as ort

# ---- Isaac Lab Unitree Go2 velocity convention (edit only to match YOUR policy) ----

# Joint-order permutation between the Unitree SDK (rt/lowstate, rt/lowcmd) and the
# Isaac Lab USD articulation order the policy was trained in.
#   SDK order:   FR[hip,thigh,calf]=0..2, FL=3..5, RR=6..8, RL=9..11
#   Isaac order: hips[FL,FR,RL,RR]=0..3, thighs=4..7, calves=8..11
ISAAC_FROM_SDK = [3, 0, 9, 6, 4, 1, 10, 7, 5, 2, 11, 8]   # isaac[i] = sdk[ISAAC_FROM_SDK[i]]
SDK_FROM_ISAAC = [1, 5, 9, 0, 4, 8, 3, 7, 11, 2, 6, 10]   # sdk[j]   = isaac[SDK_FROM_ISAAC[j]]

# Default joint pose in ISAAC order (radians): hips ±0.1, front thighs 0.8 / rear 1.0,
# all calves -1.5.  [FL_hip,FR_hip,RL_hip,RR_hip, FL_th,FR_th,RL_th,RR_th, FL_cf..RR_cf]
DEFAULT_POSE_ISAAC = np.array(
    [0.1, -0.1, 0.1, -0.1, 0.8, 0.8, 1.0, 1.0, -1.5, -1.5, -1.5, -1.5], dtype=np.float32
)

ACTION_SCALE = float(os.environ.get("ACTION_SCALE", "0.25"))   # Isaac Lab Go2 default
# PD gains: upstream IsaacLab UNITREE_GO2_CFG = 25/0.5; Unitree's unitree_rl_lab = 40/1.0.
# [VERIFY] use the gains from the SAME repo/env.yaml that produced your policy.
KP = float(os.environ.get("KP", "25.0"))
KD = float(os.environ.get("KD", "0.5"))
STOP_KD = float(os.environ.get("STOP_KD", "3.0"))             # damping-stop kd (kp=0)
JOINT_DELTA_MAX = float(os.environ.get("JOINT_DELTA_MAX", "1.2"))  # clamp |target-default|

CONTROL_MODE = os.environ.get("CONTROL_MODE", "lowcmd").lower()  # lowcmd | velocity
CONTROL_HZ = float(os.environ.get("CONTROL_HZ", "50"))           # Isaac Lab = 50 Hz
OBS_DIM = int(os.environ.get("OBS_DIM", "48"))
ACT_DIM = int(os.environ.get("ACT_DIM", "12"))
USE_BASE_LIN_VEL = os.environ.get("USE_BASE_LIN_VEL", "1") == "1"  # flat=48 uses it; blind=45 doesn't
WATCHDOG_S = float(os.environ.get("WATCHDOG_S", "0.5"))
CMD = np.array(
    [float(os.environ.get("CMD_VX", "0.0")), float(os.environ.get("CMD_VY", "0.0")),
     float(os.environ.get("CMD_VYAW", "0.0"))], dtype=np.float32)
MAX_VX = float(os.environ.get("MAX_VX", "0.5"))
MAX_VY = float(os.environ.get("MAX_VY", "0.3"))
MAX_VYAW = float(os.environ.get("MAX_VYAW", "0.5"))

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
        self._sport = None
        self._lowcmd_pub = None
        self._lowcmd = None
        self._crc = None
        self._state_lock = threading.Lock()
        self._latest = {}
        self._subs = []
        self._loop_thread: threading.Thread | None = None
        self._watchdog: threading.Timer | None = None
        self._wd_gen = 0
        self._running = False
        self._connected = False
        self._last_action = np.zeros(12, dtype=np.float32)   # Isaac order, raw net output
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
        from unitree_sdk2py.core.channel import ChannelSubscriber
        from unitree_sdk2py.idl.unitree_go.msg.dds_ import LowState_

        _init_dds()
        sub = ChannelSubscriber("rt/lowstate", LowState_)
        sub.Init(self._on_lowstate, 10)
        self._subs.append(sub)

        if CONTROL_MODE == "lowcmd":
            if not ENABLE_LOWCMD:
                raise RuntimeError("lowcmd mode needs ENABLE_LOWCMD=1 AND sport_mode OFF on the robot")
            self._init_lowcmd()
        elif CONTROL_MODE == "velocity":
            from unitree_sdk2py.go2.sport.sport_client import SportClient
            self._sport = SportClient()
            self._sport.SetTimeout(3.0)
            self._sport.Init()
        else:
            raise RuntimeError(f"unknown CONTROL_MODE {CONTROL_MODE!r}")
        self._connected = True

    def _on_lowstate(self, msg) -> None:
        try:
            with self._state_lock:
                self._latest = {
                    "gyro": list(msg.imu_state.gyroscope),
                    "quat": list(msg.imu_state.quaternion),
                    "q": [m.q for m in msg.motor_state[:12]],     # SDK order
                    "dq": [m.dq for m in msg.motor_state[:12]],   # SDK order
                }
        except Exception:  # noqa: BLE001
            pass

    # ----- observation (Isaac Lab layout) --------------------------------------

    def build_observation(self) -> np.ndarray:
        """Isaac Lab Go2 velocity obs, RAW SI units (no legged_gym-style scaling):
        [base_lin_vel(3), base_ang_vel(3), projected_gravity(3), velocity_command(3),
         joint_pos_rel(12), joint_vel(12), last_action(12)] in ISAAC joint order.
        base_lin_vel is privileged in sim — see the note: hard to get in low-level
        mode (sport_mode off), so it defaults to zeros. Prefer a 'blind' 45-dim policy
        for real deployment, or wire a state estimator here.
        """
        with self._state_lock:
            s = dict(self._latest)
        if not s:
            return np.zeros(OBS_DIM, dtype=np.float32)
        ang_vel = np.asarray(s["gyro"], dtype=np.float32)
        proj_g = _projected_gravity(s["quat"])
        q_isaac = _to_isaac(s["q"])
        dq_isaac = _to_isaac(s["dq"])
        joint_pos_rel = q_isaac - DEFAULT_POSE_ISAAC
        parts = []
        if USE_BASE_LIN_VEL:
            parts.append(self._base_lin_vel())   # [VERIFY] zeros unless you add an estimator
        parts += [ang_vel, proj_g, CMD, joint_pos_rel, dq_isaac, self._last_action]
        obs = np.concatenate(parts).astype(np.float32)
        if obs.shape[0] != OBS_DIM:  # pad/trim defensively, but the layout should already match
            obs = np.concatenate([obs, np.zeros(max(0, OBS_DIM - obs.shape[0]), dtype=np.float32)])[:OBS_DIM]
        return obs

    def _base_lin_vel(self) -> np.ndarray:
        # Not available from rt/lowstate, and rt/sportmodestate is silent in low-level
        # mode. Returns zeros by default — train/deploy a blind (no-lin-vel) policy, or
        # plug a state estimator here.
        return np.zeros(3, dtype=np.float32)

    # ----- action --------------------------------------------------------------

    def apply_action(self, action: np.ndarray) -> None:
        if CONTROL_MODE == "lowcmd":
            # Isaac Lab: target_q = default_pose + action*scale, clamped, then to SDK order.
            target_isaac = DEFAULT_POSE_ISAAC + action * ACTION_SCALE
            target_isaac = np.clip(
                target_isaac, DEFAULT_POSE_ISAAC - JOINT_DELTA_MAX, DEFAULT_POSE_ISAAC + JOINT_DELTA_MAX)
            self._publish_lowcmd(_to_sdk(target_isaac), kp=KP, kd=KD)
        else:
            vx = float(np.clip(action[0], -MAX_VX, MAX_VX))
            vy = float(np.clip(action[1] if ACT_DIM > 1 else 0.0, -MAX_VY, MAX_VY))
            vyaw = float(np.clip(action[2] if ACT_DIM > 2 else 0.0, -MAX_VYAW, MAX_VYAW))
            self._sport.Move(vx, vy, vyaw)

    # ----- lowcmd (rt/lowcmd PD publish) ---------------------------------------

    def _init_lowcmd(self) -> None:
        from unitree_sdk2py.core.channel import ChannelPublisher
        from unitree_sdk2py.idl.default import unitree_go_msg_dds__LowCmd_
        from unitree_sdk2py.idl.unitree_go.msg.dds_ import LowCmd_
        from unitree_sdk2py.utils.crc import CRC

        self._lowcmd_pub = ChannelPublisher("rt/lowcmd", LowCmd_)
        self._lowcmd_pub.Init()
        self._lowcmd = unitree_go_msg_dds__LowCmd_()
        self._lowcmd.head[0] = 0xFE
        self._lowcmd.head[1] = 0xEF
        self._lowcmd.level_flag = 0xFF   # low-level
        self._lowcmd.gpio = 0
        self._crc = CRC()

    def _publish_lowcmd(self, q_sdk: np.ndarray, kp: float, kd: float, dq: float = 0.0) -> None:
        if self._lowcmd_pub is None:
            return
        for j in range(12):
            mc = self._lowcmd.motor_cmd[j]
            mc.mode = 0x01  # servo (PMSM)
            mc.q = float(q_sdk[j])
            mc.dq = dq
            mc.kp = kp
            mc.kd = kd
            mc.tau = 0.0
        self._lowcmd.crc = self._crc.Crc(self._lowcmd)
        self._lowcmd_pub.Write(self._lowcmd)

    def _lowcmd_damping(self) -> None:
        # Safe stop: zero stiffness + joint damping → the dog folds/settles gently.
        # (On a stand it just relaxes. Keep it on a stand.)
        with self._state_lock:
            q = self._latest.get("q", [0.0] * 12)
        self._publish_lowcmd(np.asarray(q, dtype=np.float32), kp=0.0, kd=STOP_KD)

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
                self._arm_watchdog()
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
            self._sync_stop()

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
        if gen is not None and gen != self._wd_gen:
            return
        self._running = False
        try:
            if CONTROL_MODE == "lowcmd":
                self._lowcmd_damping()
            elif self._sport is not None:
                self._sport.StopMove()
        except Exception:  # noqa: BLE001
            pass

    def status(self) -> dict:
        return {
            "interface": "policy", "robot": os.environ.get("ROBOT", "go2"),
            "mode": CONTROL_MODE, "enabled": ENABLE_POLICY and (ENABLE_LOWCMD or CONTROL_MODE != "lowcmd"),
            "policy_loaded": self._sess is not None, "obs_dim": OBS_DIM, "act_dim": ACT_DIM,
            **self._status,
        }


def _to_isaac(v_sdk) -> np.ndarray:
    a = np.asarray(v_sdk, dtype=np.float32)
    return np.array([a[ISAAC_FROM_SDK[i]] for i in range(12)], dtype=np.float32)


def _to_sdk(v_isaac) -> np.ndarray:
    a = np.asarray(v_isaac, dtype=np.float32)
    return np.array([a[SDK_FROM_ISAAC[j]] for j in range(12)], dtype=np.float32)


def _init_dds() -> None:
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
    # gravity (0,0,-1) expressed in the base frame from the IMU quaternion (w,x,y,z).
    # Upright (w=1) -> [0,0,-1], matching Isaac Lab's projected_gravity.
    w, x, y, z = (list(quat) + [1, 0, 0, 0])[:4]
    return np.array(
        [-2 * (x * z - w * y), -2 * (y * z + w * x), -(1 - 2 * (x * x + y * y))],
        dtype=np.float32,
    )
