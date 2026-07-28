import os

# Bind ingest to an ephemeral port so the test never collides with 5566.
os.environ.setdefault("CSI_UDP_PORT", "0")
os.environ.setdefault("CSI_DATA_DIR", "/tmp")

from fastapi.testclient import TestClient  # noqa: E402

from app import api  # noqa: E402
from tools.csi_sender import build_csi_line  # noqa: E402


def test_status_and_config_endpoints():
    with TestClient(api) as client:
        r = client.get("/api/status")
        assert r.status_code == 200
        assert "udp_port" in r.json()

        r = client.get("/api/config")
        assert r.status_code == 200
        assert r.json()["analysis_rate_hz"] == 20.0

        r = client.get("/api/sensors")
        assert r.status_code == 200
        assert isinstance(r.json(), list)


def test_calibrate_without_data_returns_409():
    with TestClient(api) as client:
        r = client.post("/api/calibrate")
        assert r.status_code == 409


def test_ws_stream_emits_analytics_frame():
    with TestClient(api) as client:
        pipe = api.state.pipeline
        # Inject a frame and produce one analytics frame so latest is populated.
        pipe.ingest(build_csi_line("aa:bb:cc:dd:ee:01", [0, 60] * 16).encode())
        pipe.analyze()
        with client.websocket_connect("/ws/stream") as ws:
            frame = ws.receive_json()
            assert "occupied" in frame
            assert "motion" in frame
            assert "waterfall" in frame
