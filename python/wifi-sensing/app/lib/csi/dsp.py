"""Classical DSP over CSI amplitude windows.

All functions are pure and operate on a resampled amplitude matrix
``amps[time, subcarrier]``. v1 uses amplitude only; phase analytics are a
future seam.
"""

from __future__ import annotations

import numpy as np
from scipy import signal as sp_signal

_EPS = 1e-9
_MOTION_SUPPRESS = 0.5  # above this motion level, vitals are unreliable
_CONF_FLOOR = 0.15  # below this, a rate estimate is reported as None
_RATE_CONF_SCALE = 10.0  # maps peak/mean band ratio to a 0..1 confidence


def select_subcarriers(amps: np.ndarray, top_k: int | None = None) -> np.ndarray:
    """Indices of informative subcarriers, ranked by variance (drops pilots/nulls)."""
    if amps.ndim != 2 or amps.shape[1] == 0:
        return np.empty(0, dtype=int)
    var = amps.var(axis=0)
    keep = np.flatnonzero(var > _EPS)
    if keep.size == 0:
        keep = np.arange(amps.shape[1])
    ranked = keep[np.argsort(var[keep])[::-1]]
    if top_k is not None:
        ranked = ranked[:top_k]
    return ranked


def baseline_variance(amps: np.ndarray) -> float:
    """Mean per-subcarrier amplitude variance over informative subcarriers."""
    idx = select_subcarriers(amps)
    if idx.size == 0 or amps.shape[0] < 2:
        return 0.0
    return float(amps[:, idx].var(axis=0).mean())


def presence_motion(
    amps: np.ndarray, baseline: float | None, threshold: float
) -> tuple[bool, float]:
    """Return ``(occupied, motion)`` from amplitude variance vs. a calibrated baseline.

    ``baseline`` is the empty-room variance from calibration; when ``None`` a small
    default floor is used so the app still works before calibration.
    """
    base = baseline if baseline and baseline > _EPS else 0.05
    var = baseline_variance(amps)
    occupied = var > base * threshold
    excess_ratio = max(var - base, 0.0) / max(base, _EPS)
    motion = float(np.clip(np.log1p(excess_ratio) / np.log1p(threshold * 100.0), 0.0, 1.0))
    return occupied, motion


def estimate_rate(
    sig: np.ndarray, rate_hz: float, lo_hz: float, hi_hz: float
) -> tuple[float | None, float]:
    """Estimate a periodic rate (BPM) within ``[lo_hz, hi_hz]`` via windowed FFT.

    Returns ``(bpm, confidence)``; ``bpm`` is ``None`` when the band is empty or
    confidence is below the floor.
    """
    n = sig.shape[0]
    if n < 8:
        return None, 0.0
    detrended = sp_signal.detrend(sig, type="linear")
    windowed = detrended * np.hanning(n)
    spectrum = np.abs(np.fft.rfft(windowed)) ** 2
    freqs = np.fft.rfftfreq(n, d=1.0 / rate_hz)

    band = np.flatnonzero((freqs >= lo_hz) & (freqs <= hi_hz))
    if band.size == 0:
        return None, 0.0

    band_power = spectrum[band]
    peak_local = int(np.argmax(band_power))
    peak_power = band_power[peak_local]
    mean_power = float(band_power.mean()) + _EPS
    ratio = peak_power / mean_power
    confidence = float(np.tanh(max(ratio - 1.0, 0.0) / _RATE_CONF_SCALE))

    bpm = float(freqs[band[peak_local]] * 60.0)
    if confidence < _CONF_FLOOR:
        return None, confidence
    return bpm, confidence


def vitals(amps: np.ndarray, rate_hz: float, motion: float) -> dict:
    """Estimate breathing and (experimental) heart rate from the best subcarrier."""
    null = {
        "breathing_bpm": None,
        "breathing_conf": 0.0,
        "heart_bpm": None,
        "heart_conf": 0.0,
    }
    if motion > _MOTION_SUPPRESS or amps.shape[0] < 8:
        return null
    idx = select_subcarriers(amps, top_k=1)
    if idx.size == 0:
        return null
    sig = amps[:, idx[0]]
    b_bpm, b_conf = estimate_rate(sig, rate_hz, 0.1, 0.5)
    h_bpm, h_conf = estimate_rate(sig, rate_hz, 0.8, 2.0)
    return {
        "breathing_bpm": b_bpm,
        "breathing_conf": b_conf,
        "heart_bpm": h_bpm,
        "heart_conf": h_conf,
    }


def waterfall(amps: np.ndarray, max_cols: int = 64, max_rows: int = 128) -> list[list[float]]:
    """Downsample an amplitude matrix to a display-sized heatmap (rounded floats)."""
    if amps.ndim != 2 or amps.size == 0:
        return []
    rows, cols = amps.shape
    row_stride = max(1, int(np.ceil(rows / max_rows)))
    col_stride = max(1, int(np.ceil(cols / max_cols)))
    reduced = amps[::row_stride, ::col_stride]
    return np.round(reduced, 3).tolist()
