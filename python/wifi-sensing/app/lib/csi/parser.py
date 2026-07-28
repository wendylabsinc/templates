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

# Column indices within the CSV portion (before the '[' array). ``mac`` and
# ``rssi`` are identical across firmware variants; only the channel position
# differs between the long (ESP32/S3) and short (C6/C5/C61) esp-csi layouts.
_COL_MAC = 2
_COL_RSSI = 3
_COL_CHANNEL_LONG = 16   # ESP32 / ESP32-S3 layout (>= 20 columns)
_COL_CHANNEL_SHORT = 8   # ESP32-C6 / C5 / C61 layout (~14 columns)
_MIN_COLS = 4            # need at least through the rssi column


def parse_csi_data(payload: bytes | str, timestamp: float = 0.0) -> CSIFrame | None:
    """Parse one ``CSI_DATA`` record. Returns ``None`` on any malformed input.

    Handles both esp-csi CSV layouts: the long ESP32/S3 form (channel at field
    16) and the short ESP32-C6/C5 form (channel at field 8). The CSI array may
    be quoted (``"[...]"``) — keying off the brackets handles either.
    """
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

        cols = head.rstrip(', "').split(",")
        if len(cols) < _MIN_COLS:
            return None

        link_id = cols[_COL_MAC].strip()
        rssi = int(cols[_COL_RSSI])
        chan_idx = _COL_CHANNEL_LONG if len(cols) >= 20 else _COL_CHANNEL_SHORT
        channel = int(cols[chan_idx]) if len(cols) > chan_idx else 0
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
