import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
MOJO = ROOT / "mojo" / "go2-rc"
PYTHON = ROOT / "python" / "go2-rc"


def test_mojo_controller_uses_max_and_same_origin_motion_proxy():
    source = (MOJO / "rc" / "main.mojo").read_text()
    assert "from max.gpu.host import DeviceContext" in source
    assert 'elif request.path == "/api/runtime"' in source
    assert "proxy_http(connection, 8000" in source
    assert re.search(r"proxy_http\(\s*connection,\s*3201\b", source)


def test_mojo_variant_does_not_open_robot_apis_with_wildcard_cors():
    camera = (MOJO / "camera" / "main.py").read_text()
    motion = (MOJO / "motion" / "main.mojo").read_text()
    assert "CORSMiddleware" not in camera
    assert 'allow_origins=["*"]' not in camera
    assert "Access-Control-Allow-Origin" not in motion


def test_mojo_camera_is_a_media_only_python_sidecar():
    camera = MOJO / "camera"
    assert not (camera / "audio.py").exists()
    assert not (camera / "perception.py").exists()
    assert not (camera / "cyclonedds.xml").exists()
    requirements = (camera / "requirements.txt").read_text()
    assert "cyclonedds" not in requirements
    assert "numpy" not in requirements
    source = (camera / "main.py").read_text()
    assert "LidarSubscriber" not in source
    assert 'websocket("/ws/perception")' not in source


def test_mojo_motion_service_uses_native_unitree_bindings():
    source = (MOJO / "motion" / "main.mojo").read_text()
    dockerfile = (MOJO / "motion" / "Dockerfile").read_text()
    assert "from unitree_mojo import Go2Client" in source
    assert "https://github.com/wendylabsinc/unitree-mojo.git" in dockerfile
    assert "ARG UNITREE_MOJO_REF=v0.2.0" in dockerfile
    assert 'request_path == "/perception"' in source
    assert "-I /opt/unitree-mojo" in dockerfile
    assert "modular==26.5.0" in dockerfile
    assert "unitree_sdk2_python" not in dockerfile
    assert not (MOJO / "motion" / "main.py").exists()
    assert not (MOJO / "motion" / "requirements.txt").exists()
    assert not (MOJO / "motion" / "bridge").exists()
    assert not (MOJO / "motion" / "unitree_mojo").exists()


def test_mojo_ui_uses_wendy_brand_and_reports_max_runtime():
    html = (MOJO / "rc" / "index.html").read_text()
    assert "--background: #171c23" in html
    assert "--seafoam: #9fe2bf" in html
    assert '/assets/wendy-logo.svg' in html
    assert 'fetch("/api/runtime")' in html
    assert "MAX 26.5.0" not in html  # populated from the live runtime response
