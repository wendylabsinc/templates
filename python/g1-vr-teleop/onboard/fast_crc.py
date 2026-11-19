"""Allocation-light CRC adapter for Unitree's HG LowCmd message.

The SDK first builds a Python list, packs it, expands every four bytes back to
another Python integer list, then copies that list into ctypes.  The native CRC
already accepts contiguous uint32 words, so the middle two Python passes are
unnecessary.  A one-time comparison with the installed SDK guards the exact
message layout; mismatch or missing native support retains the SDK path.
"""
from __future__ import annotations

import ctypes
import struct


class FastHGLowCmdCRC:
    _STRUCT = struct.Struct("<2B2x" + "B3x5fI" * 35 + "5I")
    _WORD_COUNT = _STRUCT.size // 4

    def __init__(self, sdk_crc):
        self._sdk = sdk_crc
        self._native = getattr(getattr(sdk_crc, "crc_lib", None), "crc32_core", None)
        self._buffer = bytearray(self._STRUCT.size)
        self._words = (ctypes.c_uint32 * self._WORD_COUNT).from_buffer(self._buffer)
        self._checked = False
        self.parity_ok = None
        self.backend = "pending_parity" if self._native is not None else "sdk_fallback"
        self.errors = 0
        self.last_values = None

    @classmethod
    def values(cls, message):
        values = [message.mode_pr, message.mode_machine]
        for motor in message.motor_cmd:
            values.extend((motor.mode, motor.q, motor.dq, motor.tau,
                           motor.kp, motor.kd, motor.reserve))
        values.extend(message.reserve)
        values.append(message.crc)
        return values

    @classmethod
    def pack(cls, message):
        return cls._STRUCT.pack(*cls.values(message))

    def _fast(self, message):
        values = self.values(message)
        self._STRUCT.pack_into(self._buffer, 0, *values)
        # Unitree excludes the final crc uint32 from the calculation.
        result = int(self._native(self._words, self._WORD_COUNT - 1))
        # Recording wants the payload as actually written, including the CRC
        # this call is about to return rather than the previous tick's field.
        values[-1] = result
        self.last_values = tuple(values)
        return result

    def Crc(self, message):  # noqa: N802 -- mirrors the Unitree SDK API
        if self._native is None or self.backend == "sdk_fallback":
            return self._sdk.Crc(message)
        try:
            fast = self._fast(message)
            if not self._checked:
                reference = int(self._sdk.Crc(message))
                self._checked = True
                self.parity_ok = fast == reference
                if not self.parity_ok:
                    self.backend = "sdk_fallback"
                    return reference
                self.backend = "fast_ctypes"
            return fast
        except Exception:  # the safety fallback is the already-used SDK path
            self.errors += 1
            self.backend = "sdk_fallback"
            return self._sdk.Crc(message)
