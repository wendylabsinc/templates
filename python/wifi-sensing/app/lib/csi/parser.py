"""Parse Espressif ``CSI_DATA`` records into :class:`CSIFrame`.

Default wire format is the Espressif ``esp-csi`` CSV line, one record per UDP
datagram. The CSI payload is the trailing ``[...]`` array of ``int8`` values,
interleaved imag/real pairs per subcarrier.

To adapt to a specific firmware build, change the column indices below — they
are the only firmware-specific knowledge in the codebase.
"""

from __future__ import annotations

import numpy as np

from app.lib.csi.types import CSIFrame

# Column indices within the CSV portion (before the '[' array).
_COL_MAC = 2
_COL_RSSI = 3
_COL_CHANNEL = 16
_MIN_COLS = 17  # need at least through the channel column


def parse_csi_data(payload: bytes | str, timestamp: float = 0.0) -> CSIFrame | None:
    """Parse one ``CSI_DATA`` record. Returns ``None`` on any malformed input."""
    try:
        text = payload.decode("ascii", "ignore") if isinstance(payload, bytes) else payload
        text = text.strip()
        if not text.startswith("CSI_DATA"):
            return None

        head, _, tail = text.partition("[")
        if not tail:
            return None
        array_str = tail.split("]", 1)[0].strip()
        if not array_str:
            return None

        ints = [int(x) for x in array_str.split(",") if x.strip() != ""]
        if len(ints) == 0 or len(ints) % 2 != 0:
            return None

        cols = head.rstrip(", ").split(",")
        if len(cols) < _MIN_COLS:
            return None

        link_id = cols[_COL_MAC].strip()
        rssi = int(cols[_COL_RSSI])
        channel = int(cols[_COL_CHANNEL])
        if not link_id:
            return None

        raw = np.asarray(ints, dtype=np.float64).reshape(-1, 2)
        imag = raw[:, 0]
        real = raw[:, 1]
        amplitudes = np.hypot(real, imag)

        return CSIFrame(
            link_id=link_id,
            timestamp=timestamp,
            rssi=rssi,
            channel=channel,
            amplitudes=amplitudes,
        )
    except (ValueError, IndexError, AttributeError):
        return None
