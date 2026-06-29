import numpy as np

from app.lib.csi import dsp

RATE = 20.0


def make_signal(freq_hz, n_sub=8, secs=30, amp=5.0, noise=0.1, seed=0):
    t = np.linspace(0, secs, int(RATE * secs), endpoint=False)
    base = amp * np.sin(2 * np.pi * freq_hz * t)
    rng = np.random.default_rng(seed)
    cols = [base + rng.normal(0, noise, t.size) + 50 for _ in range(n_sub)]
    return np.stack(cols, axis=1)


def test_breathing_15bpm():
    amps = make_signal(0.25)  # 0.25 Hz -> 15 BPM
    bpm, conf = dsp.estimate_rate(amps[:, 0], RATE, 0.1, 0.5)
    assert bpm is not None and abs(bpm - 15) < 1.5
    assert conf > 0.3


def test_heart_72bpm():
    amps = make_signal(1.2)  # 1.2 Hz -> 72 BPM
    bpm, conf = dsp.estimate_rate(amps[:, 0], RATE, 0.8, 2.0)
    assert bpm is not None and abs(bpm - 72) < 3


def test_presence_true_on_high_variance():
    amps = make_signal(0.25, amp=8.0)
    occ, motion = dsp.presence_motion(amps, baseline=0.05, threshold=1.5)
    assert occ and motion > 0


def test_presence_false_when_flat():
    rng = np.random.default_rng(1)
    amps = rng.normal(50, 0.01, (600, 8))
    occ, _ = dsp.presence_motion(amps, baseline=0.05, threshold=1.5)
    assert not occ


def test_noise_only_low_confidence():
    rng = np.random.default_rng(2)
    amps = rng.normal(50, 0.5, (600, 8))
    bpm, conf = dsp.estimate_rate(amps[:, 0], RATE, 0.1, 0.5)
    assert conf < 0.3 or bpm is None


def test_vitals_suppressed_under_motion():
    amps = make_signal(0.25)
    v = dsp.vitals(amps, RATE, motion=0.9)
    assert v["breathing_bpm"] is None
    assert v["heart_bpm"] is None


def test_vitals_reports_breathing_when_still():
    amps = make_signal(0.25)
    v = dsp.vitals(amps, RATE, motion=0.1)
    assert v["breathing_bpm"] is not None and abs(v["breathing_bpm"] - 15) < 2


def test_waterfall_downsamples():
    amps = make_signal(0.25, n_sub=128, secs=20)
    wf = dsp.waterfall(amps, max_cols=64, max_rows=128)
    assert len(wf) <= 128
    assert all(len(row) <= 64 for row in wf)


def test_baseline_variance_low_for_flat():
    rng = np.random.default_rng(3)
    amps = rng.normal(50, 0.01, (200, 8))
    assert dsp.baseline_variance(amps) < 0.01
