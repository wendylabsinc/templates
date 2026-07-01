import asyncio
from pathlib import Path

from app.config import Config
from app.lib.csi.ingest import UDPCSISource
from app.lib.csi.pipeline import Pipeline
from tools.csi_sender import build_csi_line, modulated_amps


def test_pipeline_detects_presence_and_breathing(tmp_path: Path):
    """Feed 30 s of synthetic 15-BPM CSI through the pipeline (controlled clock)."""
    rate = 20.0
    cfg = Config(analysis_rate_hz=rate, data_dir=tmp_path)
    clock = {"t": 0.0}
    pipe = Pipeline(cfg, now=lambda: clock["t"])

    # Calibrate against a flat (empty) baseline first.
    for _ in range(int(rate * 4)):
        flat = build_csi_line("aa:bb:cc:dd:ee:01", [0, 50] * 32)
        pipe.ingest(flat.encode())
        clock["t"] += 1.0 / rate
    pipe.calibrate()

    # Now feed a breathing person at 15 BPM for 30 s.
    for i in range(int(rate * 30)):
        line = build_csi_line("aa:bb:cc:dd:ee:01", modulated_amps(clock["t"], 15.0, 32))
        pipe.ingest(line.encode())
        clock["t"] += 1.0 / rate

    frame = pipe.analyze()
    assert frame.occupied is True
    assert frame.breathing_bpm is not None
    assert abs(frame.breathing_bpm - 15.0) < 2.0
    assert "aa:bb:cc:dd:ee:01" in frame.waterfall


def test_udp_source_receives_datagram():
    """UDPCSISource hands a sent datagram to the pipeline buffer."""

    async def scenario():
        cfg = Config(udp_port=0)  # ephemeral
        source = UDPCSISource(port=0)
        await source.start()
        port = source._transport.get_extra_info("sockname")[1]

        pipe = Pipeline(cfg, now=lambda: 1.0)
        task = asyncio.create_task(pipe.run(source))

        import socket

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        line = build_csi_line("aa:bb:cc:dd:ee:09", [0, 60] * 16)
        sock.sendto(line.encode(), ("127.0.0.1", port))
        sock.close()

        for _ in range(50):
            await asyncio.sleep(0.02)
            if "aa:bb:cc:dd:ee:09" in pipe.store.links():
                break

        task.cancel()
        await source.close()
        assert "aa:bb:cc:dd:ee:09" in pipe.store.links()

    asyncio.run(scenario())
