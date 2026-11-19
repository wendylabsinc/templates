"""Suite-wide fixtures."""
import pathlib
import sys

import pytest

# The autouse fixture below imports retarget_g1, so make it importable HERE.
# It used to rely on whichever test module sorted first having done it, which
# broke twice in one evening -- once when a file named test_dex3_order.py was
# added, and again for test_tracking_leash. A conftest that needs an import
# should arrange for it.
for _d in ("teleop/bridge", "onboard"):
    _p = str(pathlib.Path(__file__).parent.parent / _d)
    if _p not in sys.path:
        sys.path.insert(0, _p)


@pytest.fixture(autouse=True)
def _sign_file_in_tmp(tmp_path, monkeypatch):
    """Never let a test write the repo's real calibration.

    flip_wrist persists to bridge/calibration/wrist_sign.json. This guard used
    to live in test_forearm.py only, so the flip tests in test_shoulder.py
    wrote the real file on every run -- and that is how left shoulder_pitch=-1
    got into the shipped calibration and stayed there after the shoulder solve
    it was correcting had been replaced. The suite was green, and the image it
    shipped would have inverted the left shoulder on hardware.

    autouse in conftest so no test module can forget it.
    """
    import retarget_g1
    monkeypatch.setattr(retarget_g1.G1Retargeter, "_SIGN_FILE",
                        str(tmp_path / "wrist_sign.json"))
