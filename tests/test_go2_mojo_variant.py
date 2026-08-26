from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
MOJO = ROOT / "mojo" / "go2-rc"
PYTHON = ROOT / "python" / "go2-rc"


def test_mojo_controller_uses_max_and_same_origin_motion_proxy():
    source = (MOJO / "rc" / "main.mojo").read_text()
    assert "from max.gpu.host import DeviceContext" in source
    assert 'elif request.path == "/api/runtime"' in source
    assert "proxy_http(connection, 8000" in source
    assert "proxy_http(\n                    connection,\n                    3201" in source


def test_mojo_variant_does_not_open_robot_apis_with_wildcard_cors():
    for service in ("motion", "camera"):
        source = (MOJO / service / "main.py").read_text()
        assert "CORSMiddleware" not in source
        assert 'allow_origins=["*"]' not in source


def test_python_robot_services_stay_in_sync_between_variants():
    for service in ("motion", "camera"):
        for source in (PYTHON / service).iterdir():
            if not source.is_file():
                continue
            copy = MOJO / service / source.name
            assert copy.is_file(), f"{copy} is missing"
            assert copy.read_bytes() == source.read_bytes(), f"{copy} drifted from {source}"


def test_mojo_ui_uses_wendy_brand_and_reports_max_runtime():
    html = (MOJO / "rc" / "index.html").read_text()
    assert "--background: #171c23" in html
    assert "--seafoam: #9fe2bf" in html
    assert '/assets/wendy-logo.svg' in html
    assert 'fetch("/api/runtime")' in html
    assert "MAX 26.5.0" not in html  # populated from the live runtime response
