# Third-party attribution

This project vendors code and assets from **Unitree Robotics `xr_teleoperate`**
(https://github.com/unitreerobotics/xr_teleoperate), licensed under **Apache
License 2.0**. The full license text is in `UNITREE_LICENSE`.

Vendored / adapted from that repo:

| Here | Origin | Change |
|---|---|---|
| `assets/g1/` | `assets/g1/` | verbatim — G1 29-DoF MuJoCo `.xml`, URDF, meshes |
| `assets/unitree_hand/` | `assets/unitree_hand/` | verbatim — Dex3-1 URDFs, meshes, `unitree_dex3.yml` |
| `bridge/_vendor_robot_arm_ik.py` | `teleop/robot_control/robot_arm_ik.py` | `logging_mp`→`logging`; asset paths made package-relative |
| `bridge/weighted_moving_filter.py` | `teleop/utils/weighted_moving_filter.py` | matplotlib import made lazy |
| `bridge/_vendor_hand_retargeting.py` | `teleop/robot_control/hand_retargeting.py` | reference only (not imported; our `retarget_g1.py` reimplements the init with absolute paths) |

Finger retargeting logic (`retarget_g1.py`) mirrors their
`robot_control/robot_hand_unitree.py`; the arm-IK call mirrors
`teleop/teleop_hand_and_arm.py`.

`dex_retargeting` (installed via pip) is a separate Apache-2.0 project by the
AnySkill / dexsuite authors.

**Not** vendored: their Isaac Lab sim path, ROS2/DDS transport, Vuer frontend,
and `televuer`/`teleimager` streaming — replaced here by wendy-sim + the WebXR
client in `web/`.
