"""Per-link ring buffers of recent CSI frames, with resampling helpers."""

from __future__ import annotations

from collections import deque

import numpy as np

from app.lib.csi.types import CSIFrame, SensorStat


class LinkBuffer:
    """A bounded, time-ordered ring buffer of frames for a single link."""

    def __init__(self, capacity: int = 4096):
        self._frames: deque[CSIFrame] = deque(maxlen=capacity)

    def add(self, frame: CSIFrame) -> None:
        self._frames.append(frame)

    def __len__(self) -> int:
        return len(self._frames)

    @property
    def width(self) -> int:
        """Subcarrier count of the most recent frame (0 if empty)."""
        return self._frames[-1].amplitudes.shape[0] if self._frames else 0

    def window(self, seconds: float, now: float) -> tuple[np.ndarray, np.ndarray]:
        """Return ``(times, amps[time, subcarrier])`` for frames within ``seconds`` of ``now``.

        Subcarrier widths are normalized to the most recent frame's width by
        truncating/zero-padding older frames.
        """
        cutoff = now - seconds
        width = self.width
        times: list[float] = []
        rows: list[np.ndarray] = []
        for f in self._frames:
            if f.timestamp < cutoff:
                continue
            a = f.amplitudes
            if a.shape[0] != width:
                fixed = np.zeros(width, dtype=np.float64)
                k = min(width, a.shape[0])
                fixed[:k] = a[:k]
                a = fixed
            times.append(f.timestamp)
            rows.append(a)
        if not rows:
            return np.empty(0), np.empty((0, width))
        return np.asarray(times), np.vstack(rows)

    def resampled(self, rate_hz: float, seconds: float, now: float) -> np.ndarray:
        """Resample the trailing window onto a regular grid of ``rate_hz * seconds`` samples."""
        n = int(round(rate_hz * seconds))
        times, amps = self.window(seconds, now)
        grid = np.linspace(now - seconds, now, n, endpoint=False)
        if amps.shape[0] < 2:
            base = amps[0] if amps.shape[0] == 1 else np.zeros(self.width)
            return np.tile(base, (n, 1))
        # Interpolate each subcarrier column onto the regular grid.
        out = np.empty((n, amps.shape[1]), dtype=np.float64)
        for c in range(amps.shape[1]):
            out[:, c] = np.interp(grid, times, amps[:, c])
        return out


class BufferStore:
    """Routes frames to per-link buffers and tracks per-link stats."""

    def __init__(self, capacity: int = 4096):
        self._capacity = capacity
        self._buffers: dict[str, LinkBuffer] = {}
        self._stat: dict[str, SensorStat] = {}

    def add(self, frame: CSIFrame) -> None:
        buf = self._buffers.get(frame.link_id)
        if buf is None:
            buf = LinkBuffer(self._capacity)
            self._buffers[frame.link_id] = buf
            self._stat[frame.link_id] = SensorStat(link_id=frame.link_id)
        buf.add(frame)
        st = self._stat[frame.link_id]
        st.packets += 1
        st.rssi = frame.rssi
        st.channel = frame.channel
        st.last_seen = frame.timestamp

    def mark_malformed(self, link_id: str = "?") -> None:
        st = self._stat.get(link_id)
        if st is None:
            st = SensorStat(link_id=link_id)
            self._stat[link_id] = st
        st.malformed += 1

    def links(self) -> list[str]:
        return list(self._buffers.keys())

    def get(self, link_id: str) -> LinkBuffer:
        return self._buffers[link_id]

    def stats(self, now: float = 0.0) -> dict[str, SensorStat]:
        return self._stat
