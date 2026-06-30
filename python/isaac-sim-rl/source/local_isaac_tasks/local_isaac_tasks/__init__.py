"""Local IsaacLab task registration hook.

Keep this package tiny at first. When you have a robot-specific task, register
its Gymnasium id here so `scripts/run_isaaclab_train.py` imports it before
IsaacLab parses `--task`.
"""

# Example shape:
#
# import gymnasium as gym
#
# gym.register(
#     id="My-Robot-Task-v0",
#     entry_point="isaaclab.envs:ManagerBasedRLEnv",
#     disable_env_checker=True,
#     kwargs={
#         "env_cfg_entry_point": "local_isaac_tasks.my_robot_env_cfg:MyRobotEnvCfg",
#         "rsl_rl_cfg_entry_point": "local_isaac_tasks.agents.rsl_rl_ppo_cfg:MyRobotPPORunnerCfg",
#     },
# )
