import ctypes
import math
import pathlib
import struct
import sys
import types

import pytest


REAL = pathlib.Path(__file__).parent.parent / "onboard"
sys.path.insert(0, str(REAL))

from fast_crc import FastHGLowCmdCRC  # noqa: E402
from hotpath import DeadlineLoop, cpu_from_env  # noqa: E402


def _message(seed=0.0):
    motors = []
    for i in range(35):
        motors.append(types.SimpleNamespace(
            mode=(i + 1) % 256,
            q=seed + i * 0.01,
            dq=seed - i * 0.02,
            tau=seed + i * 0.03,
            kp=40.0 + i,
            kd=1.5 + i * 0.1,
            reserve=0x12340000 + i,
        ))
    return types.SimpleNamespace(mode_pr=2, mode_machine=7, motor_cmd=motors,
                                 reserve=[11, 22, 33, 44], crc=0)


def _crc_words(words, length):
    crc = 0xFFFFFFFF
    polynomial = 0x04C11DB7
    for current in words[:length]:
        bit = 1 << 31
        for _ in range(32):
            crc = ((crc << 1) & 0xFFFFFFFF) ^ (
                polynomial if crc & 0x80000000 else 0)
            if current & bit:
                crc ^= polynomial
            bit >>= 1
    return crc


class _Library:
    @staticmethod
    def crc32_core(words, length):
        return _crc_words(words, length)


class _SDKCRC:
    def __init__(self, with_library=True):
        if with_library:
            self.crc_lib = _Library()
        self.calls = 0

    def Crc(self, message):
        self.calls += 1
        packed = FastHGLowCmdCRC.pack(message)
        words = struct.unpack("<251I", packed)
        return _crc_words(words, 250)


def test_fast_crc_is_bit_exact_and_only_uses_sdk_for_startup_parity():
    sdk = _SDKCRC()
    crc = FastHGLowCmdCRC(sdk)
    first = crc.Crc(_message(0.125))
    second = crc.Crc(_message(-0.25))
    assert first == _SDKCRC().Crc(_message(0.125))
    assert second == _SDKCRC().Crc(_message(-0.25))
    assert sdk.calls == 1, "the SDK path is a one-time layout/parity guard"
    assert crc.backend == "fast_ctypes"
    assert crc.parity_ok is True


def test_fast_crc_falls_back_when_the_sdk_has_no_native_library():
    sdk = _SDKCRC(with_library=False)
    crc = FastHGLowCmdCRC(sdk)
    assert crc.Crc(_message()) == _SDKCRC().Crc(_message())
    assert crc.backend == "sdk_fallback"
    assert sdk.calls == 1


class _Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def test_deadline_loop_is_anchored_and_skips_missed_ticks_without_bursting():
    clock = _Clock()
    loop = DeadlineLoop(0.01, clock=clock, sleep=clock.sleep)
    clock.now = 0.003
    loop.wait()
    assert clock.now == pytest.approx(0.01)
    clock.now = 0.037  # deadlines at .02 and .03 were missed
    loop.wait()
    assert clock.now == pytest.approx(0.04), "wait for the next future deadline"
    assert loop.missed == 2
    clock.now = 0.041
    loop.wait()
    assert clock.now == pytest.approx(0.05)


def test_cpu_selection_is_explicit_bounded_and_auto_uses_the_last_core():
    available = {2, 4, 7}
    assert cpu_from_env({}, "MOTOR", available=available) is None
    assert cpu_from_env({"MOTOR": "auto"}, "MOTOR", available=available) == 7
    assert cpu_from_env({}, "MOTOR", available=available, default="auto",
                        auto_offset=1) == 4
    assert cpu_from_env({"MOTOR": "off"}, "MOTOR", available=available,
                        default="auto") is None
    assert cpu_from_env({"MOTOR": "4"}, "MOTOR", available=available) == 4
    with pytest.raises(ValueError):
        cpu_from_env({"MOTOR": "3"}, "MOTOR", available=available)
