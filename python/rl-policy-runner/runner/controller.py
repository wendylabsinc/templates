"""On-robot control loop for the Walk-These-Ways (WTW) Unitree Go2 policy.

Faithfully replicates the deploy path of github.com/Teddy-Liao/walk-these-ways-go2
(an MIT-licensed port of Improbable-AI/walk-these-ways): a gait-conditioned RMA
policy with two TorchScript nets and a stacked observation history.

    every 50 Hz tick:
      update gait clock -> build single obs(70) -> push into history(15*70=1050)
      latent = adaptation_module(history)
      action = body(cat(history, latent))           # 12 joint deltas
      target_q = default_pose + action*0.25 (hip*0.5), reorder to SDK, rt/lowcmd PD

Constants below are the WTW Go2 config; items marked [VERIFY] are the standard
values that should be confirmed (a startup self-check validates the obs size and
latent dim against the actual .jit shapes). SAFETY: ENABLE gates, sport_mode OFF,
independent watchdog -> damping stop, startup ramp to the default pose. Test on a
stand, e-stop ready.
"""

import os
import threading
import time

import numpy as np

# ---- WTW Go2 spec (from walk-these-ways-go2 deploy code) -------------------------

NUM_OBS = int(os.environ.get("NUM_OBS", "70"))          # single-step obs [VERIFY vs parameters.pkl]
HISTORY = int(os.environ.get("HISTORY_LEN", "15"))      # observation history length
NUM_COMMANDS = 15
ACTION_SCALE = 0.25
HIP_SCALE_REDUCTION = 0.5                                # applied to policy hip idxs 0,3,6,9
KP = float(os.environ.get("KP", "25.0"))
KD = float(os.environ.get("KD", "0.6"))
STOP_KD = float(os.environ.get("STOP_KD", "3.0"))
DOF_VEL_SCALE = 0.05
CLIP_ACTIONS = 100.0
CONTROL_HZ = float(os.environ.get("CONTROL_HZ", "50"))
DT = 1.0 / CONTROL_HZ

# Policy joint order is FL,FR,RL,RR (each hip,thigh,calf); SDK motor order is
# FR,FL,RR,RL. joint_idxs maps policy->SDK (and is its own inverse for SDK->policy).
JOINT_IDXS = np.array([3, 4, 5, 0, 1, 2, 9, 10, 11, 6, 7, 8])  # [VERIFY] vs cheetah_state_estimator
DEFAULT_POSE_POLICY = np.array(
    [0.1, 0.8, -1.5, -0.1, 0.8, -1.5, 0.1, 1.0, -1.5, -0.1, 1.0, -1.5], dtype=np.float32
)
# command_scale, per WTW lcm_agent (obs_scales applied to the raw command vector)
COMMAND_SCALE = np.array(
    [2.0, 2.0, 0.25, 2.0, 1, 1, 1, 1, 1, 0.15, 0.3, 0.3, 1.0, 1.0, 1.0], dtype=np.float32
)

# Desired motion command (defaults = trot in place). Velocities are clamped.
CMD_VX = float(np.clip(float(os.environ.get("CMD_VX", "0.0")), -0.6, 0.6))
CMD_VY = float(np.clip(float(os.environ.get("CMD_VY", "0.0")), -0.6, 0.6))
CMD_VYAW = float(np.clip(float(os.environ.get("CMD_VYAW", "0.0")), -1.0, 1.0))
STEP_FREQ = float(os.environ.get("STEP_FREQ", "3.0"))
FOOTSWING = float(os.environ.get("FOOTSWING", "0.08"))
# raw 15-dim command: [vx,vy,yaw, body_height, freq, phase,offset,bound,duration,
#                      footswing, pitch, roll, stance_width, stance_length, aux]
RAW_COMMAND = np.array(
    [CMD_VX, CMD_VY, CMD_VYAW, 0.0, STEP_FREQ, 0.5, 0.0, 0.0, 0.5, FOOTSWING,
     0.0, 0.0, 0.33, 0.40, 0.0], dtype=np.float32)

POLICY_DIR = os.environ.get("POLICY_DIR", "/policy")
DDS_ADDR = os.environ.get("GO2_DDS_ADDRESS", "").strip()
IFACE = os.environ.get("GO2_NETWORK_INTERFACE", "eth0")
ENABLE_POLICY = os.environ.get("ENABLE_POLICY", "0") == "1"
ENABLE_LOWCMD = os.environ.get("ENABLE_LOWCMD", "0") == "1"


class PolicyRunner:
    def __init__(self) -> None:
        self._adapt = None
        self._body = None
        self._latent_dim = 0
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
        self._obs_history = np.zeros(NUM_OBS * HISTORY, dtype=np.float32)
        self._actions = np.zeros(12, dtype=np.float32)        # a(t-1), policy order
        self._last_actions = np.zeros(12, dtype=np.float32)   # a(t-2), policy order
        self._gait_index = 0.0
        self._status = {"state": "idle", "rate_hz": 0.0, "detail": "", "obs": [], "action": []}

    # ----- load + connect ------------------------------------------------------

    def load_policy(self) -> None:
        if self._body is not None:
            return
        import torch

        self._torch = torch
        self._adapt = torch.jit.load(f"{POLICY_DIR}/adaptation_module_latest.jit").to("cpu").eval()
        self._body = torch.jit.load(f"{POLICY_DIR}/body_latest.jit").to("cpu").eval()
        # Self-check: infer latent dim and validate the obs-history size against the nets.
        with torch.no_grad():
            hist = torch.zeros(1, NUM_OBS * HISTORY)
            latent = self._adapt(hist)
            self._latent_dim = int(latent.shape[-1])
            self._body(torch.cat((hist, latent), dim=-1))  # raises if sizes are wrong
        self._status["detail"] = f"nets ok: obs={NUM_OBS}x{HISTORY}={NUM_OBS*HISTORY}, latent={self._latent_dim}"

    def connect(self) -> None:
        if self._connected:
            return
        from unitree_sdk2py.core.channel import ChannelSubscriber
        from unitree_sdk2py.idl.unitree_go.msg.dds_ import LowState_

        _init_dds()
        sub = ChannelSubscriber("rt/lowstate", LowState_)
        sub.Init(self._on_lowstate, 10)
        self._subs.append(sub)
        if not ENABLE_LOWCMD:
            raise RuntimeError("WTW needs ENABLE_LOWCMD=1 AND the robot's sport_mode OFF (low-level mode)")
        self._init_lowcmd()
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

    # ----- observation (WTW) ---------------------------------------------------

    def _single_obs(self) -> np.ndarray:
        with self._state_lock:
            s = dict(self._latest)
        if not s:
            return np.zeros(NUM_OBS, dtype=np.float32)
        gravity = _projected_gravity(s["quat"])                       # 3  [VERIFY sign]
        commands = RAW_COMMAND * COMMAND_SCALE                        # 15
        q_pol = np.asarray(s["q"], dtype=np.float32)[JOINT_IDXS]      # SDK->policy
        dq_pol = np.asarray(s["dq"], dtype=np.float32)[JOINT_IDXS]
        dof_pos = (q_pol - DEFAULT_POSE_POLICY)                       # 12 (dof_pos scale = 1.0)
        dof_vel = dq_pol * DOF_VEL_SCALE                             # 12
        actions = np.clip(self._actions, -CLIP_ACTIONS, CLIP_ACTIONS)  # 12
        clock = self._clock_inputs()                                  # 4
        ob = np.concatenate([gravity, commands, dof_pos, dof_vel, actions,
                             self._last_actions, clock]).astype(np.float32)
        if ob.shape[0] != NUM_OBS:  # zero-pad/trim defensively (should match exactly)
            ob = np.concatenate([ob, np.zeros(max(0, NUM_OBS - ob.shape[0]), dtype=np.float32)])[:NUM_OBS]
        return ob

    def _clock_inputs(self) -> np.ndarray:
        freq, phase, offset, bound = RAW_COMMAND[4], RAW_COMMAND[5], RAW_COMMAND[6], RAW_COMMAND[7]
        self._gait_index = (self._gait_index + DT * freq) % 1.0
        gi = self._gait_index
        feet = np.array([gi + phase + offset + bound, gi + offset, gi + bound, gi + phase])
        return np.sin(2 * np.pi * feet).astype(np.float32)  # FL, FR, RL, RR (sin only)

    # ----- action (WTW -> rt/lowcmd) -------------------------------------------

    def _apply_action(self, action: np.ndarray) -> None:
        jt = action * ACTION_SCALE                       # policy order
        jt[[0, 3, 6, 9]] *= HIP_SCALE_REDUCTION          # hips
        jt = jt + DEFAULT_POSE_POLICY
        q_sdk = jt[JOINT_IDXS]                            # policy -> SDK
        self._publish_lowcmd(q_sdk, kp=KP, kd=KD)

    # ----- lowcmd publish ------------------------------------------------------

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
        self._lowcmd.level_flag = 0xFF
        self._lowcmd.gpio = 0
        self._crc = CRC()

    def _publish_lowcmd(self, q_sdk: np.ndarray, kp: float, kd: float) -> None:
        if self._lowcmd_pub is None:
            return
        for j in range(12):
            mc = self._lowcmd.motor_cmd[j]
            mc.mode = 0x01
            mc.q = float(q_sdk[j])
            mc.dq = 0.0
            mc.kp = kp
            mc.kd = kd
            mc.tau = 0.0
        self._lowcmd.crc = self._crc.Crc(self._lowcmd)
        self._lowcmd_pub.Write(self._lowcmd)

    def _lowcmd_damping(self) -> None:
        with self._state_lock:
            q = self._latest.get("q", [0.0] * 12)
        self._publish_lowcmd(np.asarray(q, dtype=np.float32), kp=0.0, kd=STOP_KD)

    def _calibrate(self) -> None:
        # Smoothly ramp from the current pose to the default stance before the policy
        # takes over, so there's no jerk (mirrors WTW's calibration phase).
        with self._state_lock:
            q = self._latest.get("q")
        if not q:
            return
        cur = np.asarray(q, dtype=np.float32)[JOINT_IDXS]  # policy order
        goal = DEFAULT_POSE_POLICY
        for _ in range(50):  # ~1 s of 0.05-rad steps
            cur = cur + np.clip(goal - cur, -0.05, 0.05)
            self._publish_lowcmd(cur[JOINT_IDXS], kp=KP, kd=KD)
            time.sleep(0.02)
            if np.max(np.abs(goal - cur)) < 0.01:
                break

    # ----- control loop + safety ----------------------------------------------

    def start(self) -> str:
        if not (ENABLE_POLICY and ENABLE_LOWCMD):
            return "blocked: needs ENABLE_POLICY=1 and ENABLE_LOWCMD=1 (and sport_mode OFF)"
        if self._running:
            return "already running"
        self.load_policy()
        self.connect()
        self._obs_history[:] = 0.0
        self._gait_index = 0.0
        self._calibrate()
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
        torch = self._torch
        self._status.update(state="running", detail="")
        try:
            while self._running:
                t0 = time.monotonic()
                ob = self._single_obs()
                self._obs_history = np.concatenate([self._obs_history[NUM_OBS:], ob])
                with torch.no_grad():
                    hist = torch.from_numpy(self._obs_history).float().unsqueeze(0)
                    latent = self._adapt(hist)
                    action = self._body(torch.cat((hist, latent), dim=-1)).squeeze(0).numpy()
                self._last_actions = self._actions
                self._actions = action.astype(np.float32)
                self._apply_action(self._actions.copy())
                self._arm_watchdog()
                self._status.update(
                    rate_hz=round(1.0 / max(time.monotonic() - t0, 1e-6), 1),
                    obs=[round(float(x), 3) for x in ob[:8]],
                    action=[round(float(x), 3) for x in self._actions[:8]],
                )
                dt = DT - (time.monotonic() - t0)
                if dt > 0:
                    time.sleep(dt)
        except Exception as exc:  # noqa: BLE001
            self._status.update(state="error", detail=str(exc))
        finally:
            self._running = False
            self._cancel_watchdog()
            self._sync_stop()

    def _arm_watchdog(self) -> None:
        self._cancel_watchdog()
        self._wd_gen += 1
        gen = self._wd_gen
        t = threading.Timer(max(0.1, 3 * DT), self._sync_stop, kwargs={"gen": gen})
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
            self._lowcmd_damping()
        except Exception:  # noqa: BLE001
            pass

    def status(self) -> dict:
        return {
            "interface": "policy", "robot": "go2", "policy": "walk-these-ways",
            "enabled": ENABLE_POLICY and ENABLE_LOWCMD, "policy_loaded": self._body is not None,
            "latent_dim": self._latent_dim, "cmd": [CMD_VX, CMD_VY, CMD_VYAW],
            **self._status,
        }


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
    # gravity (0,0,-1) in the base frame from IMU quaternion (w,x,y,z); upright -> [0,0,-1].
    w, x, y, z = (list(quat) + [1, 0, 0, 0])[:4]
    return np.array(
        [-2 * (x * z - w * y), -2 * (y * z + w * x), -(1 - 2 * (x * x + y * y))],
        dtype=np.float32,
    )
