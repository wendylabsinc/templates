import numpy as np

from app.lib.csi.buffer import BufferStore, LinkBuffer
from app.lib.csi.types import CSIFrame


def frame(link, t, val, n=4):
    return CSIFrame(link_id=link, timestamp=t, rssi=-50, channel=6,
                    amplitudes=np.full(n, float(val)))


def test_link_buffer_window_filters_by_time():
    b = LinkBuffer(capacity=100)
    for i in range(10):
        b.add(frame("a", float(i), i))
    times, amps = b.window(seconds=3.0, now=9.0)
    # frames with t >= 6.0 -> t in {6,7,8,9}
    assert times.min() >= 6.0
    assert amps.shape[0] == times.shape[0] == 4


def test_link_buffer_eviction_past_capacity():
    b = LinkBuffer(capacity=5)
    for i in range(20):
        b.add(frame("a", float(i), i))
    times, _ = b.window(seconds=1000.0, now=19.0)
    assert times.shape[0] == 5
    assert times.min() == 15.0


def test_resampled_returns_fixed_sample_count():
    b = LinkBuffer(capacity=1000)
    for i in range(100):
        b.add(frame("a", i * 0.1, np.sin(i * 0.1)))
    out = b.resampled(rate_hz=20.0, seconds=4.0, now=9.9)
    assert out.shape[0] == 80  # 20 Hz * 4 s


def test_store_routes_and_isolates_links():
    s = BufferStore(capacity=100)
    s.add(frame("a", 1.0, 5))
    s.add(frame("b", 1.0, 9))
    assert set(s.links()) == {"a", "b"}
    _, amps_a = s.get("a").window(10.0, 2.0)
    assert np.allclose(amps_a, 5.0)


def test_store_stats_tracks_packets_and_rssi():
    s = BufferStore(capacity=100)
    s.add(frame("a", 1.0, 5))
    s.add(frame("a", 2.0, 5))
    stats = s.stats(now=2.0)
    assert stats["a"].packets == 2
    assert stats["a"].rssi == -50
    assert stats["a"].channel == 6
