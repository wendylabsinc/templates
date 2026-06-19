"""Operator-mic → dog-speaker audio publisher.

Receives 8 kHz mono Int16 PCM frames from the browser over WebSocket
and publishes them to `/audioreceiver` (`unitree_go/AudioData`) so the
dog's onboard audio service plays them out the speaker.

Codec chain:

    browser mic (getUserMedia)
        │  Web Audio: native rate (48k) → decimate to 8k → Int16 mono
        ▼
    binary WebSocket frames @ ~20 ms (160 samples = 320 bytes)
        │
        ▼  audioop.lin2ulaw  (Int16 → µ-law, halves the bytes)
    DDS publish: rt/audioreceiver  (unitree_go/AudioData)
        │
        ▼
    dog's audio service → speaker

µ-law @ 8 kHz is what the Unitree audio stack uses internally
(watchtower's go2_audio_bridge.py reads the inbound mic stream the
same way).

QoS: defaults — RELIABLE/KEEP_LAST(10). Same as watchtower's audio
bridge subscribes with. BEST_EFFORT is tempting for low latency but
makes µ-law glitches very audible if a packet drops.

NOTE: the AudioData IDL field names are best-guess from the public
SDK headers (`time_frame: uint64; data: sequence<octet>`). If wire
discovery works but playback is silent, the most likely cause is a
field-name / typename mismatch — adjust by reading
`unitree_ros2/cyclonedds_ws/src/unitree/unitree_go/msg/AudioData.idl`
and we re-render here.
"""

import audioop
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from cyclonedds.domain import DomainParticipant
from cyclonedds.idl import IdlStruct
from cyclonedds.idl.types import sequence, uint8, uint64
from cyclonedds.pub import DataWriter, Publisher
from cyclonedds.topic import Topic


log = logging.getLogger("go2-camera.audio")

# Empirically, /audioreceiver IS the topic the dog's audio service
# subscribes to for playback (publishing there made the speaker audibly
# do *something*; /audiosender is where the dog's mic publishes and
# writes to it are ignored). watchtower's docstring naming is
# misleading — go by the behaviour, not the name.
AUDIO_OUT_TOPIC = "rt/audioreceiver"

# Codec for /audioreceiver. watchtower's bridge claims µ-law @ 8 kHz
# but publishing µ-law produced pure noise (the dog tried to play our
# bytes interpreted as something else). Try raw 16-bit linear PCM
# first; can be flipped via env if a different format wins.
#   "pcm16"  → publish Int16 PCM bytes as-is
#   "ulaw"   → audioop.lin2ulaw(pcm, 2) (G.711)
#   "alaw"   → audioop.lin2alaw(pcm, 2) (G.711 alternate)
AUDIO_OUT_CODEC = os.environ.get("AUDIO_OUT_CODEC", "pcm16").lower()


@dataclass
class _AudioData(IdlStruct, typename="unitree_go::msg::dds_::AudioData_"):
    """Wire-compatible mirror of unitree_go/msg/AudioData."""
    time_frame: uint64 = 0
    data: sequence[uint8] = field(default_factory=list)


class AudioOutPublisher:
    """Thread-safe DDS publisher for /audioreceiver.

    The WebSocket handler calls `push_pcm16(bytes)` on every audio
    frame it receives. We hold one DataWriter for the lifetime of the
    process — re-creating per chunk would burn CPU on participant
    discovery and drop the first ~50 ms of every burst.
    """

    def __init__(self, domain: int = 0) -> None:
        self._domain = domain
        self._dp: Optional[DomainParticipant] = None
        self._writer: Optional[DataWriter] = None
        self._lock = threading.Lock()
        self._frame_counter = 0
        self._setup_attempted = False

    def _ensure_writer(self) -> Optional[DataWriter]:
        with self._lock:
            if self._writer is not None:
                return self._writer
            if self._setup_attempted:
                # Already tried once and failed; don't retry on every
                # frame — the WS handler will see None and drop frames
                # silently rather than spinning DDS init forever.
                return None
            self._setup_attempted = True
            try:
                self._dp = DomainParticipant(self._domain)
                topic = Topic(self._dp, AUDIO_OUT_TOPIC, _AudioData)
                pub = Publisher(self._dp)
                self._writer = DataWriter(pub, topic)
                log.info("audio publisher up: %s, domain=%d, codec=%s",
                         AUDIO_OUT_TOPIC, self._domain, AUDIO_OUT_CODEC)
                return self._writer
            except Exception:
                log.exception("Failed to set up audio DDS publisher")
                return None

    def push_pcm16(self, pcm: bytes) -> None:
        """Encode 8 kHz mono Int16 PCM per AUDIO_OUT_CODEC and publish."""
        if not pcm:
            return
        writer = self._ensure_writer()
        if writer is None:
            return
        try:
            if AUDIO_OUT_CODEC == "ulaw":
                payload = audioop.lin2ulaw(pcm, 2)
            elif AUDIO_OUT_CODEC == "alaw":
                payload = audioop.lin2alaw(pcm, 2)
            else:  # "pcm16" (default)
                payload = pcm
        except audioop.error as exc:
            log.warning("audio encode (%s) failed (len=%d): %s",
                        AUDIO_OUT_CODEC, len(pcm), exc)
            return
        self._frame_counter += 1
        msg = _AudioData(
            time_frame=self._frame_counter,
            data=list(payload),
        )
        try:
            writer.write(msg)
        except Exception:
            # Don't tear down the publisher on a transient write failure
            # — the next frame will retry. Log throttled.
            now = time.monotonic()
            if not hasattr(self, "_last_warn_t") or now - self._last_warn_t > 1.0:
                log.exception("audio publish failed")
                self._last_warn_t = now
