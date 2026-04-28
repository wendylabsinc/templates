"""HTTP + WebSocket entrypoint for the voice-ai-pipecat template.

Serves the built React visualizer from `./static/` and runs a Pipecat
pipeline in one of two transports at a time:

- **local** (default): `LocalAudioTransport` reads from the host's USB mic
  and writes to its speaker via PortAudio. Lets you talk to the assistant
  with no browser involved.
- **browser**: when a client connects to `/bot-audio`, the local pipeline
  is torn down and a `FastAPIWebsocketTransport` takes over. On browser
  disconnect (or via the "Hand back to local mic" button) the local
  pipeline resumes.

Pick the local mic/speaker via the `AUDIO_INPUT_DEVICE` /
`AUDIO_OUTPUT_DEVICE` env vars (PyAudio device index, name substring, or
`default`). The startup log prints the PyAudio enumeration so you can
find the right index. The `/api/audio-devices` endpoint exposes the same
list to the frontend so users can pick a device live.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.websockets import WebSocketState

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.pipeline.runner import PipelineRunner
from pipecat.serializers.protobuf import ProtobufFrameSerializer
from pipecat.transports.base_transport import BaseTransport
from pipecat.transports.local.audio import (
    LocalAudioTransport,
    LocalAudioTransportParams,
)
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)

from pipeline import DEFAULT_SYSTEM_PROMPT, PROMPT_PRESETS, build_pipeline_task


PORT = int(os.environ.get("PORT", "3005"))
STATIC_DIR = Path(os.environ.get("STATIC_DIR", Path(__file__).parent / "static"))
HOTPLUG_POLL_SECS = float(os.environ.get("HOTPLUG_POLL_SECS", "3.0"))
# Persist user-edited settings to a writable path. /tmp survives the
# process but resets on container restart, which is fine for a demo
# template — for true persistence mount a volume and point this here.
SETTINGS_PATH = Path(os.environ.get("SETTINGS_PATH", "/tmp/voice-ai-settings.json"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d %(levelname)s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("voice-ai-pipecat")


class _MutePollingFilter(logging.Filter):
    """Drop uvicorn access logs for the polling endpoints.

    The frontend polls /api/status, /api/audio-devices, and /api/settings
    every ~1.2 s. At INFO level uvicorn emits one access line per request
    per route, which drowns useful logs. The actual data still flows;
    only the access record is suppressed.
    """

    NOISY = (
        '"GET /api/status',
        '"GET /api/audio-devices',
        '"GET /api/settings',
    )

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        return not any(p in msg for p in self.NOISY)


logging.getLogger("uvicorn.access").addFilter(_MutePollingFilter())

# Pipecat ships loguru pre-wired at DEBUG; that's a flood of frame-link
# logs and per-frame metrics for every pipeline restart. Bump it to
# INFO so the lifecycle events still show but the noise stops.
try:
    import sys as _sys

    from loguru import logger as _loguru_logger

    _loguru_logger.remove()
    _loguru_logger.add(_sys.stderr, level="INFO")
except Exception:
    pass

# Suppress the OpenAILLMContext / VAD-param `DeprecationWarning` chorus
# Pipecat 0.0.108 prints on every pipeline build. They're not actionable
# for template users.
import warnings as _warnings

_warnings.filterwarnings("ignore", category=DeprecationWarning)


def _silence_alsa_errors() -> None:
    """Install a no-op libasound error handler.

    PyAudio enumerates ALSA on every PyAudio() instance, and our hot-plug
    watchdog calls that on a 3s loop. ALSA's config probing routinely
    dumps a screenful of `Unknown PCM cards.pcm.front` / `Cannot get card
    index for N` warnings straight to stderr (not Python's logging) on
    every probe. They're harmless — surface only when libasound *can't*
    fall back through its plugin chain — and they drown the actual app
    logs. Register a no-op error handler in libasound itself so they
    stop. Real failures still raise Python exceptions from PyAudio.
    """
    try:
        from ctypes import CFUNCTYPE, c_char_p, c_int, cdll

        error_handler_t = CFUNCTYPE(None, c_char_p, c_int, c_char_p, c_int, c_char_p)
        handler = error_handler_t(lambda *_args: None)
        # Keep a strong reference so the trampoline isn't GC'd.
        _silence_alsa_errors._handler = handler  # type: ignore[attr-defined]
        cdll.LoadLibrary("libasound.so.2").snd_lib_error_set_handler(handler)
    except Exception as exc:
        logger.debug("Couldn't silence libasound errors: %s", exc)


_silence_alsa_errors()


def _silence_jack_errors() -> None:
    """Install no-op libjack error/info handlers.

    PortAudio links against libjack on Linux and probes for a JACK
    server every time PyAudio() is instantiated. When no JACK server is
    running (the common case in containers without audio servers) it
    dumps `Cannot connect to server socket err = No such file or
    directory` plus a few `JackShmReadWritePtr` lines straight to
    stderr per probe. Our hot-plug watchdog runs every 3s, so without
    this the logs become unreadable.

    libasound and libjack have separate error-handler APIs — silencing
    one doesn't silence the other.
    """
    try:
        from ctypes import CFUNCTYPE, c_char_p, cdll

        cb_t = CFUNCTYPE(None, c_char_p)
        handler = cb_t(lambda *_args: None)
        _silence_jack_errors._handler = handler  # type: ignore[attr-defined]
        for libname in ("libjack.so.0", "libjack.so"):
            try:
                lib = cdll.LoadLibrary(libname)
                if hasattr(lib, "jack_set_error_function"):
                    lib.jack_set_error_function(handler)
                if hasattr(lib, "jack_set_info_function"):
                    lib.jack_set_info_function(handler)
                break
            except OSError:
                continue
    except Exception as exc:
        logger.debug("Couldn't silence libjack errors: %s", exc)


_silence_jack_errors()


def _enumerate_devices() -> list[dict[str, Any]]:
    """Return PyAudio's current device enumeration as plain dicts.

    Each PyAudio() instance probes ALSA fresh, so calling this on a poll
    interval is what lets us detect USB hot-plug events: an unplugged
    PowerConf disappears from the list, a freshly plugged one reappears
    (often at a new index, but the `name` is stable).
    """
    try:
        import pyaudio
    except Exception as exc:
        logger.warning("PyAudio unavailable for device enumeration: %s", exc)
        return []
    pa = pyaudio.PyAudio()
    try:
        devices = []
        for i in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(i)
            name = str(info.get("name", ""))
            # Skip kernel/virtual devices that aren't useful as user-facing
            # mics. Jetson "APE/HDA" entries enumerate ~20 virtual loopback
            # PCMs that just clutter the selector; sysdefault/spdif/hdmi
            # are ALSA aliases that aren't capture-useful (and on USB
            # speakerphones like the PowerConf, picking sysdefault routes
            # to the same hw:N,M direct path that fails at 16 kHz).
            if "Orin Nano APE" in name or "Orin Nano HDA" in name:
                continue
            if "Orin NX APE" in name or "Orin NX HDA" in name:
                continue
            if name in {"sysdefault", "spdif", "hdmi", "front", "surround40",
                        "surround51", "surround71", "iec958", "dmix"}:
                continue
            # Hide raw `hw:N,M` entries from the picker. They look
            # tempting (e.g. "PowerConf: USB Audio (hw:0,0)") but
            # bypass our asound.conf plug route — picking one fails at
            # 16 kHz on devices that only support 48 kHz capture, like
            # the PowerConf. The "default" alias is what users should
            # pick; it goes through plug with rate conversion.
            if re.search(r"\(hw:\d+,\d+\)", name):
                continue
            devices.append(
                {
                    "id": i,
                    "name": name,
                    "input_channels": int(info.get("maxInputChannels", 0)),
                    "output_channels": int(info.get("maxOutputChannels", 0)),
                    "default_sample_rate": int(info.get("defaultSampleRate", 0)),
                }
            )
        return devices
    finally:
        pa.terminate()


def _resolve_device(value: Optional[str], devices: list[dict[str, Any]]) -> tuple[Optional[int], Optional[str]]:
    """Resolve a device spec against a freshly-enumerated list.

    Returns `(index, name)`. Index is None when we want PortAudio's own
    default; name is the device label we matched (used by the hot-plug
    watchdog to re-find the device after a plug event shifts indexes).

    Accepts:
      - '' or None     → PortAudio default. Often wrong on ALSA — picks
                         card 0, which on Jetson is HDMI with no input.
      - 'default'      → device whose name is literally 'default'. With
                         our /etc/asound.conf routing ALSA default →
                         plug → hw:2,0, that's the entry that gets us
                         in-kernel rate conversion to the USB mic.
      - integer string → that exact index (no name match needed).
      - any other str  → first device whose name contains the substring,
                         case-insensitive (e.g. 'powerconf').
    """
    if not value:
        return None, None
    lookup = value.strip()
    try:
        idx = int(lookup)
    except ValueError:
        idx = None
    if idx is not None:
        for d in devices:
            if d["id"] == idx:
                return idx, d["name"]
        return None, None  # index not in current enum (probably unplugged)
    wanted = lookup.lower()
    for d in devices:
        name = d["name"].lower()
        if wanted == "default" and name == "default":
            return d["id"], d["name"]
        if wanted != "default" and wanted in name:
            return d["id"], d["name"]
    return None, None


def _find_device_index_by_name(name: str, devices: list[dict[str, Any]]) -> Optional[int]:
    for d in devices:
        if d["name"] == name:
            return d["id"]
    return None


def _log_audio_devices(devices: list[dict[str, Any]]) -> None:
    logger.info("PyAudio devices:")
    for d in devices:
        logger.info(
            "  [%d] %s  in=%d out=%d  default_sr=%d",
            d["id"],
            d["name"],
            d["input_channels"],
            d["output_channels"],
            d["default_sample_rate"],
        )


DEFAULT_TTS_VOICE = "en_US-lessac-medium"
DEFAULT_ALLOW_INTERRUPTIONS = False
DEFAULT_WAKE_WORD_MODELS = ["hey_jarvis"]
DEFAULT_WAKE_WORD_DISABLED = False
DEFAULT_STT_LANGUAGE = "auto"
DEFAULT_VAD_CONFIDENCE = 0.7
DEFAULT_VAD_MIN_VOLUME = 0.6
# How long of a silence after speech before VAD considers you done.
# Pipecat's default is 0.2 s — way too short, fires mid-sentence on
# natural pauses. 1.0 s gives time to gather your thoughts without
# making the bot feel sluggish at the end of a snappy reply.
DEFAULT_VAD_STOP_SECS = 1.0
DEFAULT_VAD_START_SECS = 0.2
DEFAULT_GOOGLE_SEARCH_ENABLED = True
DEFAULT_GREETING_ENABLED = True
DEFAULT_GREETING_MESSAGE = (
    "Hi, I'm your voice assistant. How can I help you today?"
)
DEFAULT_PERSIST_CONVERSATION = False
DEFAULT_LLM_PROVIDER = "google"
DEFAULT_LLM_MODEL = "gemini-2.5-flash"
DEFAULT_STT_PROVIDER = "whisper"
DEFAULT_STT_MODEL = "tiny"

# STT providers we support. Whisper runs locally on CPU; Deepgram is
# cloud-streaming with much lower latency (~150 ms TTFB vs ~1–3 s).
STT_PROVIDERS: dict[str, list[str]] = {
    "whisper": ["tiny", "base", "small", "medium"],
    "deepgram": [
        "nova-3",
        "nova-2",
        "nova-2-general",
        "nova-2-conversationalai",
    ],
}
# Per-STT-provider API key env var, mirroring LLM_API_KEY_ENV.
STT_API_KEY_ENV: dict[str, str] = {
    "whisper": "",  # local model, no key needed
    "deepgram": "DEEPGRAM_API_KEY",
}

# Provider → list of pretrained model names the picker offers. The
# user can also type a custom name in the input — Pipecat passes it
# straight to the API.
LLM_PROVIDERS: dict[str, list[str]] = {
    "google": [
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
        "gemini-2.5-pro",
    ],
    "openai": [
        "gpt-4o-mini",
        "gpt-4o",
        "gpt-5-mini",
        "gpt-5",
    ],
    "anthropic": [
        "claude-haiku-4-5",
        "claude-sonnet-4-7",
        "claude-opus-4-7",
    ],
    "groq": [
        "llama-3.3-70b-versatile",
        "llama-3.1-8b-instant",
        "qwen2.5-72b-instruct",
    ],
}
# Maps provider key → env var name. We fall back to the env var when no
# key is present in the runtime settings store.
LLM_API_KEY_ENV: dict[str, str] = {
    "google": "GOOGLE_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "groq": "GROQ_API_KEY",
}
# Search backend for non-Google providers. Brave is the default; users
# can also drop in TAVILY_API_KEY and switch with /api/settings.
SEARCH_BACKEND_ENV = "BRAVE_API_KEY"
CONVERSATION_PATH = Path(
    os.environ.get("CONVERSATION_PATH", "/tmp/voice-ai-conversation.json")
)
# Cap on persisted history so the file doesn't grow unbounded. The
# context window will trim stale messages anyway, but loading 10k
# turns into RAM at boot is silly.
CONVERSATION_MAX_TURNS = 40

# Voices we pre-download in the Dockerfile. Keep in sync with the
# `for triple` loop there. The frontend's voice picker uses this list.
AVAILABLE_TTS_VOICES = [
    "en_US-lessac-medium",
    "en_US-ryan-high",
    "en_US-amy-medium",
    "en_GB-alan-medium",
]
AVAILABLE_WAKE_WORDS = [
    "alexa",
    "hey_jarvis",
    "hey_mycroft",
    "hey_rhasspy",
    "ok_nabu",
]
# Whisper-supported languages we surface to the picker. "auto" means
# let the model detect. Whisper supports far more — extend this list
# (and the frontend dropdown labels) if you need more.
AVAILABLE_STT_LANGUAGES = [
    "auto",
    "en",
    "es",
    "fr",
    "de",
    "it",
    "pt",
    "nl",
    "ru",
    "zh",
    "ja",
    "ko",
    "ar",
    "hi",
]


def _load_conversation_history() -> list[dict[str, str]]:
    try:
        import json

        data = json.loads(CONVERSATION_PATH.read_text())
        if isinstance(data, list):
            return [m for m in data if isinstance(m, dict) and "role" in m and "content" in m]
    except FileNotFoundError:
        return []
    except Exception:
        logger.exception("Failed to load conversation history; starting fresh")
    return []


def _save_conversation_history(history: list[dict[str, str]]) -> None:
    try:
        import json

        CONVERSATION_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONVERSATION_PATH.write_text(json.dumps(history[-CONVERSATION_MAX_TURNS * 2 :], indent=2))
    except Exception:
        logger.exception("Failed to persist conversation history to %s", CONVERSATION_PATH)


def _on_turn_complete(user_text: str, bot_text: str) -> None:
    """Append a completed user/assistant turn to disk if persistence is on."""
    if not settings_store.persist_conversation:
        return
    history = _load_conversation_history()
    history.append({"role": "user", "content": user_text})
    history.append({"role": "assistant", "content": bot_text})
    _save_conversation_history(history)


def _build_vad_analyzer() -> SileroVADAnalyzer:
    """Construct a Silero VAD with the user's current sensitivity settings.

    Read at transport build time so saving in /api/settings reaches the
    next pipeline.
    """
    return SileroVADAnalyzer(
        params=VADParams(
            confidence=settings_store.vad_confidence,
            min_volume=settings_store.vad_min_volume,
            stop_secs=settings_store.vad_stop_secs,
            start_secs=settings_store.vad_start_secs,
        )
    )


class AppSettings:
    """User-editable settings, persisted across container restarts.

    Exposed via /api/settings so the frontend can let the user tweak the
    system prompt, TTS voice, wake-word behavior, and interruption mode
    without redeploying.
    """

    def __init__(self) -> None:
        self.system_prompt: str = DEFAULT_SYSTEM_PROMPT
        self.tts_voice: str = DEFAULT_TTS_VOICE
        self.allow_interruptions: bool = DEFAULT_ALLOW_INTERRUPTIONS
        self.wake_word_models: list[str] = list(DEFAULT_WAKE_WORD_MODELS)
        self.wake_word_disabled: bool = DEFAULT_WAKE_WORD_DISABLED
        self.stt_language: str = DEFAULT_STT_LANGUAGE
        self.vad_confidence: float = DEFAULT_VAD_CONFIDENCE
        self.vad_min_volume: float = DEFAULT_VAD_MIN_VOLUME
        self.vad_stop_secs: float = DEFAULT_VAD_STOP_SECS
        self.vad_start_secs: float = DEFAULT_VAD_START_SECS
        self.google_search_enabled: bool = DEFAULT_GOOGLE_SEARCH_ENABLED
        self.greeting_enabled: bool = DEFAULT_GREETING_ENABLED
        self.greeting_message: str = DEFAULT_GREETING_MESSAGE
        self.persist_conversation: bool = DEFAULT_PERSIST_CONVERSATION
        self.llm_provider: str = DEFAULT_LLM_PROVIDER
        self.llm_model: str = DEFAULT_LLM_MODEL
        self.stt_provider: str = DEFAULT_STT_PROVIDER
        self.stt_model: str = DEFAULT_STT_MODEL
        # API keys configured via the settings UI at runtime. Stored in
        # the settings JSON file so they survive restarts. Falling back
        # to env vars (LLM_API_KEY_ENV / SEARCH_BACKEND_ENV) when not
        # set here means existing GOOGLE_API_KEY users see no change.
        self.api_keys: dict[str, str] = {}
        self.brave_api_key: str = ""
        self._load()

    def _load(self) -> None:
        try:
            import json

            data = json.loads(SETTINGS_PATH.read_text())
            self.system_prompt = data.get("system_prompt", DEFAULT_SYSTEM_PROMPT)
            self.tts_voice = data.get("tts_voice", DEFAULT_TTS_VOICE)
            self.allow_interruptions = bool(
                data.get("allow_interruptions", DEFAULT_ALLOW_INTERRUPTIONS)
            )
            wake_models = data.get("wake_word_models", DEFAULT_WAKE_WORD_MODELS)
            if isinstance(wake_models, list) and all(isinstance(m, str) for m in wake_models):
                self.wake_word_models = wake_models or list(DEFAULT_WAKE_WORD_MODELS)
            self.wake_word_disabled = bool(
                data.get("wake_word_disabled", DEFAULT_WAKE_WORD_DISABLED)
            )
            self.stt_language = str(data.get("stt_language", DEFAULT_STT_LANGUAGE))
            self.vad_confidence = float(
                data.get("vad_confidence", DEFAULT_VAD_CONFIDENCE)
            )
            self.vad_min_volume = float(
                data.get("vad_min_volume", DEFAULT_VAD_MIN_VOLUME)
            )
            self.vad_stop_secs = float(
                data.get("vad_stop_secs", DEFAULT_VAD_STOP_SECS)
            )
            self.vad_start_secs = float(
                data.get("vad_start_secs", DEFAULT_VAD_START_SECS)
            )
            self.google_search_enabled = bool(
                data.get("google_search_enabled", DEFAULT_GOOGLE_SEARCH_ENABLED)
            )
            self.greeting_enabled = bool(
                data.get("greeting_enabled", DEFAULT_GREETING_ENABLED)
            )
            self.greeting_message = str(
                data.get("greeting_message", DEFAULT_GREETING_MESSAGE)
            )
            self.persist_conversation = bool(
                data.get("persist_conversation", DEFAULT_PERSIST_CONVERSATION)
            )
            self.llm_provider = str(data.get("llm_provider", DEFAULT_LLM_PROVIDER))
            self.llm_model = str(data.get("llm_model", DEFAULT_LLM_MODEL))
            self.stt_provider = str(data.get("stt_provider", DEFAULT_STT_PROVIDER))
            self.stt_model = str(data.get("stt_model", DEFAULT_STT_MODEL))
            keys = data.get("api_keys", {})
            if isinstance(keys, dict):
                self.api_keys = {
                    str(k): str(v) for k, v in keys.items() if isinstance(v, str)
                }
            self.brave_api_key = str(data.get("brave_api_key", ""))
            logger.info("Loaded settings from %s", SETTINGS_PATH)
        except FileNotFoundError:
            pass
        except Exception:
            logger.exception("Failed to load %s; using defaults", SETTINGS_PATH)

    def has_api_key(self, provider: str) -> bool:
        """True if either the runtime store or the env var has a key."""
        if self.api_keys.get(provider):
            return True
        env = LLM_API_KEY_ENV.get(provider) or STT_API_KEY_ENV.get(provider)
        return bool(env and os.environ.get(env))

    def has_brave_key(self) -> bool:
        return bool(self.brave_api_key or os.environ.get(SEARCH_BACKEND_ENV))

    def get_api_key(self, provider: str) -> str:
        """Resolve a runtime key, falling back to the env var."""
        key = self.api_keys.get(provider, "")
        if key:
            return key
        env = LLM_API_KEY_ENV.get(provider) or STT_API_KEY_ENV.get(provider)
        return os.environ.get(env, "") if env else ""

    def get_brave_key(self) -> str:
        return self.brave_api_key or os.environ.get(SEARCH_BACKEND_ENV, "")

    def _save(self) -> None:
        try:
            import json

            SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            # Persist EVERYTHING including raw key material — to_dict()
            # is for API responses (sanitized); this is the on-disk
            # serialization that has to round-trip cleanly.
            payload = {
                "system_prompt": self.system_prompt,
                "tts_voice": self.tts_voice,
                "allow_interruptions": self.allow_interruptions,
                "wake_word_models": list(self.wake_word_models),
                "wake_word_disabled": self.wake_word_disabled,
                "stt_language": self.stt_language,
                "vad_confidence": self.vad_confidence,
                "vad_min_volume": self.vad_min_volume,
                "vad_stop_secs": self.vad_stop_secs,
                "vad_start_secs": self.vad_start_secs,
                "google_search_enabled": self.google_search_enabled,
                "greeting_enabled": self.greeting_enabled,
                "greeting_message": self.greeting_message,
                "persist_conversation": self.persist_conversation,
                "llm_provider": self.llm_provider,
                "llm_model": self.llm_model,
                "stt_provider": self.stt_provider,
                "stt_model": self.stt_model,
                "api_keys": dict(self.api_keys),
                "brave_api_key": self.brave_api_key,
            }
            SETTINGS_PATH.write_text(json.dumps(payload, indent=2))
        except Exception:
            logger.exception("Failed to persist settings to %s", SETTINGS_PATH)

    def update(
        self,
        *,
        system_prompt: Optional[str] = None,
        tts_voice: Optional[str] = None,
        allow_interruptions: Optional[bool] = None,
        wake_word_models: Optional[list[str]] = None,
        wake_word_disabled: Optional[bool] = None,
        stt_language: Optional[str] = None,
        vad_confidence: Optional[float] = None,
        vad_min_volume: Optional[float] = None,
        vad_stop_secs: Optional[float] = None,
        vad_start_secs: Optional[float] = None,
        google_search_enabled: Optional[bool] = None,
        greeting_enabled: Optional[bool] = None,
        greeting_message: Optional[str] = None,
        persist_conversation: Optional[bool] = None,
        llm_provider: Optional[str] = None,
        llm_model: Optional[str] = None,
        stt_provider: Optional[str] = None,
        stt_model: Optional[str] = None,
        api_keys: Optional[dict[str, str]] = None,
        api_keys_clear: Optional[list[str]] = None,
        brave_api_key: Optional[str] = None,
    ) -> bool:
        """Apply changes; returns True if anything actually changed."""
        changed = False
        if system_prompt is not None and system_prompt != self.system_prompt:
            self.system_prompt = system_prompt
            changed = True
        if tts_voice is not None and tts_voice != self.tts_voice:
            if tts_voice in AVAILABLE_TTS_VOICES:
                self.tts_voice = tts_voice
                changed = True
            else:
                logger.warning("Ignoring unknown TTS voice %r", tts_voice)
        if allow_interruptions is not None and allow_interruptions != self.allow_interruptions:
            self.allow_interruptions = allow_interruptions
            changed = True
        if wake_word_models is not None:
            cleaned = [m for m in wake_word_models if m in AVAILABLE_WAKE_WORDS]
            if cleaned and cleaned != self.wake_word_models:
                self.wake_word_models = cleaned
                changed = True
        if wake_word_disabled is not None and wake_word_disabled != self.wake_word_disabled:
            self.wake_word_disabled = wake_word_disabled
            changed = True
        if stt_language is not None and stt_language != self.stt_language:
            if stt_language in AVAILABLE_STT_LANGUAGES:
                self.stt_language = stt_language
                changed = True
            else:
                logger.warning("Ignoring unknown STT language %r", stt_language)
        if vad_confidence is not None:
            clamped = max(0.0, min(1.0, float(vad_confidence)))
            if clamped != self.vad_confidence:
                self.vad_confidence = clamped
                changed = True
        if vad_min_volume is not None:
            clamped = max(0.0, min(1.0, float(vad_min_volume)))
            if clamped != self.vad_min_volume:
                self.vad_min_volume = clamped
                changed = True
        if vad_stop_secs is not None:
            clamped = max(0.05, min(5.0, float(vad_stop_secs)))
            if clamped != self.vad_stop_secs:
                self.vad_stop_secs = clamped
                changed = True
        if vad_start_secs is not None:
            clamped = max(0.05, min(2.0, float(vad_start_secs)))
            if clamped != self.vad_start_secs:
                self.vad_start_secs = clamped
                changed = True
        if (
            google_search_enabled is not None
            and google_search_enabled != self.google_search_enabled
        ):
            self.google_search_enabled = google_search_enabled
            changed = True
        if greeting_enabled is not None and greeting_enabled != self.greeting_enabled:
            self.greeting_enabled = greeting_enabled
            changed = True
        if greeting_message is not None and greeting_message != self.greeting_message:
            self.greeting_message = greeting_message
            changed = True
        if (
            persist_conversation is not None
            and persist_conversation != self.persist_conversation
        ):
            self.persist_conversation = persist_conversation
            changed = True
        if llm_provider is not None and llm_provider in LLM_PROVIDERS:
            if llm_provider != self.llm_provider:
                self.llm_provider = llm_provider
                # Default model for the new provider, if the current
                # model is from a different provider's namespace.
                if self.llm_model not in LLM_PROVIDERS[llm_provider]:
                    self.llm_model = LLM_PROVIDERS[llm_provider][0]
                changed = True
        if llm_model is not None and llm_model and llm_model != self.llm_model:
            self.llm_model = llm_model
            changed = True
        if stt_provider is not None and stt_provider in STT_PROVIDERS:
            if stt_provider != self.stt_provider:
                self.stt_provider = stt_provider
                if self.stt_model not in STT_PROVIDERS[stt_provider]:
                    self.stt_model = STT_PROVIDERS[stt_provider][0]
                changed = True
        if stt_model is not None and stt_model and stt_model != self.stt_model:
            self.stt_model = stt_model
            changed = True
        if api_keys is not None:
            valid_providers = set(LLM_API_KEY_ENV) | {
                p for p, env in STT_API_KEY_ENV.items() if env
            }
            for provider, key in api_keys.items():
                if provider in valid_providers and key:
                    if self.api_keys.get(provider) != key:
                        self.api_keys[provider] = key
                        changed = True
        if api_keys_clear:
            for provider in api_keys_clear:
                if provider in self.api_keys:
                    del self.api_keys[provider]
                    changed = True
        if brave_api_key is not None and brave_api_key != self.brave_api_key:
            self.brave_api_key = brave_api_key
            changed = True
        if changed:
            self._save()
        return changed

    def to_dict(self) -> dict[str, Any]:
        return {
            "system_prompt": self.system_prompt,
            "tts_voice": self.tts_voice,
            "allow_interruptions": self.allow_interruptions,
            "wake_word_models": list(self.wake_word_models),
            "wake_word_disabled": self.wake_word_disabled,
            "stt_language": self.stt_language,
            "vad_confidence": self.vad_confidence,
            "vad_min_volume": self.vad_min_volume,
            "vad_stop_secs": self.vad_stop_secs,
            "vad_start_secs": self.vad_start_secs,
            "google_search_enabled": self.google_search_enabled,
            "greeting_enabled": self.greeting_enabled,
            "greeting_message": self.greeting_message,
            "persist_conversation": self.persist_conversation,
            "llm_provider": self.llm_provider,
            "llm_model": self.llm_model,
            "stt_provider": self.stt_provider,
            "stt_model": self.stt_model,
            # Booleans only — never expose raw key material in API
            # responses. Frontend uses these to render "configured" /
            # "not configured" badges.
            "api_keys_configured": {
                p: self.has_api_key(p)
                for p in (
                    set(LLM_PROVIDERS)
                    | {p for p, env in STT_API_KEY_ENV.items() if env}
                )
            },
            "search_api_key_configured": self.has_brave_key(),
        }


settings_store = AppSettings()


class SessionManager:
    """Keeps exactly one Pipecat pipeline running at a time.

    Default mode `local` uses `LocalAudioTransport` (host USB mic+speaker).
    When a browser connects to /bot-audio the local pipeline is torn down
    and a browser pipeline takes over.

    Tracks selected input/output devices by *name* in addition to index so
    the hot-plug watchdog can recover after an unplug/replug shifts ALSA
    enumeration order.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None
        self._mode: str = "idle"
        # Resolved at start_local time against the live enumeration. Index
        # may shift across hot-plug events; name is the stable handle.
        self._input_index: Optional[int] = None
        self._input_name: Optional[str] = None
        self._output_index: Optional[int] = None
        self._output_name: Optional[str] = None
        # Configured fallback (env vars). Used when no override is passed.
        self._configured_input = os.environ.get("AUDIO_INPUT_DEVICE")
        self._configured_output = os.environ.get("AUDIO_OUTPUT_DEVICE")
        self._last_error: Optional[str] = None
        self._device_missing: bool = False
        # Runtime telemetry surfaced via /api/status to drive the
        # frontend status pill, latency display, and wake-fired flash.
        self._processing: bool = False
        self._processing_started_mono: Optional[float] = None
        self._last_response_time_ms: Optional[int] = None
        self._last_wake_at: Optional[float] = None  # epoch seconds
        self._wake_pulse: int = 0  # increments each time wake fires
        # Currently-attached browser WebSocket, if any. Tracked so a
        # settings change can rebuild the browser pipeline in place
        # without forcing the user to reconnect.
        self._active_browser_ws: Optional[WebSocket] = None

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    @property
    def device_missing(self) -> bool:
        return self._device_missing

    @property
    def input_name(self) -> Optional[str]:
        return self._input_name

    @property
    def output_name(self) -> Optional[str]:
        return self._output_name

    def is_owned_by(self, task: asyncio.Task) -> bool:
        return self._task is task

    def on_user_stopped(self) -> None:
        self._processing = True
        self._processing_started_mono = time.monotonic()

    def on_bot_started(self) -> None:
        if self._processing_started_mono is not None:
            self._last_response_time_ms = int(
                (time.monotonic() - self._processing_started_mono) * 1000
            )
        self._processing = False
        self._processing_started_mono = None

    def on_wake_fired(self) -> None:
        self._wake_pulse += 1
        self._last_wake_at = time.time()

    async def start_local(
        self,
        *,
        input_device: Optional[str] = None,
        output_device: Optional[str] = None,
    ) -> Optional[asyncio.Task]:
        """Start (or restart) the local audio pipeline.

        `input_device`/`output_device` accept the same forms as the
        `AUDIO_*_DEVICE` env vars (index, name substring, 'default').
        Passing None falls back to whatever was configured at startup.

        Returns the running pipeline task on success, or None if the
        configured input device is currently missing (e.g. USB unplugged).
        In the missing case `device_missing` is set to True and the
        hot-plug watchdog will retry once the device reappears.
        """
        in_spec = input_device if input_device is not None else self._configured_input
        out_spec = output_device if output_device is not None else self._configured_output

        devices = _enumerate_devices()
        in_idx, in_name = _resolve_device(in_spec, devices)
        out_idx, out_name = _resolve_device(out_spec, devices)

        # Persist what the user (or env) asked for; the resolved index/name
        # may shift across hot-plug events so we re-resolve each time.
        if input_device is not None:
            self._configured_input = input_device
        if output_device is not None:
            self._configured_output = output_device

        # If a specific input was requested but isn't present, don't try
        # to start PortAudio — it would silently fail or kill the app.
        # Mark device-missing so /api/status surfaces it to the frontend
        # Alert; the watchdog will start the pipeline once the device
        # comes back.
        if in_spec and in_idx is None:
            async with self._lock:
                await self._cancel_current_locked()
                self._mode = "idle"
                self._input_index = None
                self._input_name = None
                self._output_index = out_idx
                self._output_name = out_name
                self._device_missing = True
                self._last_error = (
                    f"Audio device {in_spec!r} not found in current "
                    "PyAudio enumeration. Plug it in (or pick another "
                    "device) and the local pipeline will resume."
                )
                logger.warning(self._last_error)
            return None

        self._input_index = in_idx
        self._input_name = in_name
        self._output_index = out_idx
        self._output_name = out_name

        in_rate = int(os.environ.get("LOCAL_AUDIO_IN_SAMPLE_RATE", "16000"))
        out_rate = int(os.environ.get("LOCAL_AUDIO_OUT_SAMPLE_RATE", "48000"))
        transport = LocalAudioTransport(
            LocalAudioTransportParams(
                audio_in_enabled=True,
                audio_out_enabled=True,
                audio_in_sample_rate=in_rate,
                audio_out_sample_rate=out_rate,
                vad_analyzer=_build_vad_analyzer(),
                input_device_index=in_idx,
                output_device_index=out_idx,
            )
        )
        self._device_missing = False
        return await self._switch_to(transport, mode="local")

    async def start_browser(self, websocket: WebSocket) -> asyncio.Task:
        transport = FastAPIWebsocketTransport(
            websocket=websocket,
            params=FastAPIWebsocketParams(
                audio_in_enabled=True,
                audio_out_enabled=True,
                audio_in_sample_rate=16000,
                audio_out_sample_rate=16000,
                add_wav_header=False,
                vad_analyzer=_build_vad_analyzer(),
                serializer=ProtobufFrameSerializer(),
            ),
        )
        self._active_browser_ws = websocket
        return await self._switch_to(transport, mode="browser")

    async def restart_in_place(self) -> None:
        """Rebuild whichever pipeline is currently running, in place.

        Called from /api/settings after the store changes so the new
        values take effect on the live session — local OR browser —
        instead of waiting for the user to reconnect."""
        if self._mode == "local":
            try:
                await self.start_local()
            except Exception:
                logger.exception("restart_in_place: local restart failed")
        elif self._mode == "browser" and self._active_browser_ws is not None:
            try:
                await self.start_browser(self._active_browser_ws)
            except Exception:
                logger.exception("restart_in_place: browser restart failed")

    async def stop(self) -> None:
        async with self._lock:
            await self._cancel_current_locked()
            self._mode = "idle"

    async def mark_device_lost(self, message: str) -> None:
        """Called by the watchdog when the configured input device vanishes."""
        async with self._lock:
            await self._cancel_current_locked()
            self._mode = "idle"
            self._last_error = message
            self._device_missing = True
            logger.warning("SessionManager: device lost — %s", message)

    async def _switch_to(self, transport: BaseTransport, *, mode: str) -> asyncio.Task:
        async with self._lock:
            await self._cancel_current_locked()
            self._mode = mode
            self._last_error = None
            self._task = asyncio.create_task(self._run_pipeline(transport, mode))
            logger.info("SessionManager: %s pipeline started", mode)
            return self._task

    async def _cancel_current_locked(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("SessionManager: pipeline shutdown error")
        self._task = None

    async def _run_pipeline(self, transport: BaseTransport, mode: str) -> None:
        # Read the latest user-configured settings at pipeline start time
        # so /api/settings edits take effect on the next session.
        # Wake word only makes sense for local always-listening mode —
        # browser sessions are explicitly opened by the user and adding
        # a wake-word gate just blocks audio from reaching STT until
        # they happen to say the activation phrase.
        wake_disabled = settings_store.wake_word_disabled or mode != "local"
        greeting = (
            settings_store.greeting_message
            if settings_store.greeting_enabled
            else None
        )
        history = (
            _load_conversation_history()
            if settings_store.persist_conversation
            else None
        )
        task = build_pipeline_task(
            transport,
            system_prompt=settings_store.system_prompt,
            tts_voice=settings_store.tts_voice,
            allow_interruptions=settings_store.allow_interruptions,
            wake_word_models=settings_store.wake_word_models,
            wake_word_disabled=wake_disabled,
            stt_language=settings_store.stt_language,
            vad_confidence=settings_store.vad_confidence,
            vad_min_volume=settings_store.vad_min_volume,
            google_search_enabled=settings_store.google_search_enabled,
            greeting_message=greeting,
            conversation_history=history,
            on_user_stopped=self.on_user_stopped,
            on_bot_started=self.on_bot_started,
            on_wake_fired=self.on_wake_fired,
            on_turn_complete=_on_turn_complete,
            llm_provider=settings_store.llm_provider,
            llm_model=settings_store.llm_model,
            llm_api_key=settings_store.get_api_key(settings_store.llm_provider),
            brave_api_key=settings_store.get_brave_key(),
            stt_provider=settings_store.stt_provider,
            stt_model=settings_store.stt_model,
            stt_api_key=settings_store.get_api_key(settings_store.stt_provider),
        )
        runner = PipelineRunner(handle_sigint=False)
        try:
            await runner.run(task)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("SessionManager: %s pipeline crashed", mode)
            self._last_error = f"{mode}: {exc}"


session = SessionManager()


async def _hotplug_watchdog() -> None:
    """Detect USB hot-plug events affecting the local audio pipeline.

    Re-resolves the configured input spec against a fresh PyAudio enum
    every HOTPLUG_POLL_SECS. Two transitions matter:
      - Spec was resolvable, now isn't → tear down local pipeline, mark
        device-missing so the frontend Alert can surface it.
      - Spec wasn't resolvable, now is → restart local pipeline.

    Browser mode is unaffected: the pipeline reads audio from the WS, not
    from PortAudio, so it survives a USB unplug.
    """
    while True:
        try:
            await asyncio.sleep(HOTPLUG_POLL_SECS)
            spec = session._configured_input  # noqa: SLF001 — internal access
            if not spec:
                # No specific device was requested (PortAudio default).
                # Nothing to watch for.
                continue
            devices = _enumerate_devices()
            in_idx, _in_name = _resolve_device(spec, devices)
            present = in_idx is not None

            if session.mode == "local" and not present:
                await session.mark_device_lost(
                    f"Audio device {spec!r} disappeared (USB unplug?)"
                )
            elif session.device_missing and present:
                logger.info(
                    "Hotplug: %r reappeared, restarting local pipeline", spec
                )
                try:
                    await session.start_local()
                except Exception:
                    logger.exception("Hotplug: failed to restart local pipeline")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Hotplug watchdog error")


@asynccontextmanager
async def lifespan(_: FastAPI):
    devices = _enumerate_devices()
    _log_audio_devices(devices)
    await session.start_local()
    watchdog = asyncio.create_task(_hotplug_watchdog())
    try:
        yield
    finally:
        watchdog.cancel()
        try:
            await watchdog
        except asyncio.CancelledError:
            pass
        await session.stop()


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


if STATIC_DIR.exists():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")


# --- Audio device API for the frontend "WendyOS devices" combobox -----------


@app.get("/api/audio-devices")
async def api_audio_devices() -> dict[str, Any]:
    """Live enumeration of host audio devices for the device-side selector."""
    devices = _enumerate_devices()
    return {
        "devices": devices,
        "selected": {
            "input_name": session.input_name,
            "output_name": session.output_name,
        },
    }


@app.get("/api/status")
async def api_status() -> dict[str, Any]:
    return {
        "mode": session.mode,
        "input_name": session.input_name,
        "output_name": session.output_name,
        "device_missing": session.device_missing,
        "error": session.last_error,
        "processing": session._processing,  # noqa: SLF001
        "last_response_time_ms": session._last_response_time_ms,  # noqa: SLF001
        "last_wake_at": session._last_wake_at,  # noqa: SLF001
        "wake_pulse": session._wake_pulse,  # noqa: SLF001
    }


@app.post("/api/conversation/reset")
async def api_conversation_reset() -> dict[str, Any]:
    """Drop the persisted conversation history and restart the local
    pipeline so the fresh context takes effect immediately."""
    try:
        CONVERSATION_PATH.unlink(missing_ok=True)
    except Exception:
        logger.exception("Failed to delete %s", CONVERSATION_PATH)
    if session.mode == "local":
        try:
            await session.start_local()
        except Exception:
            logger.exception("Failed to restart pipeline after conversation reset")
    return {"ok": True}


class LocalAudioSelectBody(BaseModel):
    input_id: Optional[str] = None
    output_id: Optional[str] = None


@app.post("/api/local-audio/select")
async def api_local_audio_select(body: LocalAudioSelectBody) -> dict[str, Any]:
    """Restart the local pipeline using new input/output devices.

    Body fields accept anything `_resolve_device` understands (integer
    index, name substring, 'default'). Either field may be omitted to
    keep the current selection.
    """
    if body.input_id is None and body.output_id is None:
        raise HTTPException(status_code=400, detail="input_id or output_id required")
    try:
        await session.start_local(
            input_device=body.input_id,
            output_device=body.output_id,
        )
    except Exception as exc:
        logger.exception("Failed to switch local devices")
        raise HTTPException(status_code=500, detail=str(exc))
    return {
        "mode": session.mode,
        "input_name": session.input_name,
        "output_name": session.output_name,
    }


# --- User settings (system prompt etc.) -------------------------------------


class SettingsBody(BaseModel):
    system_prompt: Optional[str] = None
    tts_voice: Optional[str] = None
    allow_interruptions: Optional[bool] = None
    wake_word_models: Optional[list[str]] = None
    wake_word_disabled: Optional[bool] = None
    stt_language: Optional[str] = None
    vad_confidence: Optional[float] = None
    vad_min_volume: Optional[float] = None
    vad_stop_secs: Optional[float] = None
    vad_start_secs: Optional[float] = None
    google_search_enabled: Optional[bool] = None
    greeting_enabled: Optional[bool] = None
    greeting_message: Optional[str] = None
    persist_conversation: Optional[bool] = None
    llm_provider: Optional[str] = None
    llm_model: Optional[str] = None
    stt_provider: Optional[str] = None
    stt_model: Optional[str] = None
    api_keys: Optional[dict[str, str]] = None
    api_keys_clear: Optional[list[str]] = None
    brave_api_key: Optional[str] = None
    # Reset the prompt to the template's built-in default. Lets the
    # frontend offer a "Restore default" button without having to ship
    # the default text in two places.
    reset_to_default: Optional[bool] = None


@app.get("/api/settings")
async def api_get_settings() -> dict[str, Any]:
    return {
        "settings": settings_store.to_dict(),
        "default_system_prompt": DEFAULT_SYSTEM_PROMPT,
        "available_tts_voices": AVAILABLE_TTS_VOICES,
        "available_wake_words": AVAILABLE_WAKE_WORDS,
        "available_stt_languages": AVAILABLE_STT_LANGUAGES,
        # Each preset's full prompt text. Frontend renders one button per
        # preset; clicking loads that preset into the prompt textarea so
        # the user can save as-is or tweak first.
        "prompt_presets": PROMPT_PRESETS,
        "available_llm_providers": LLM_PROVIDERS,
        "available_stt_providers": STT_PROVIDERS,
    }


@app.post("/api/settings")
async def api_update_settings(body: SettingsBody) -> dict[str, Any]:
    """Apply settings changes. If anything changed AND a local pipeline
    is currently running, restart it so the new values take effect now.
    Browser sessions don't get bumped — the user's mid-conversation; the
    new settings apply on next session."""
    next_prompt = body.system_prompt
    if body.reset_to_default:
        next_prompt = DEFAULT_SYSTEM_PROMPT
    changed = settings_store.update(
        system_prompt=next_prompt,
        tts_voice=body.tts_voice,
        allow_interruptions=body.allow_interruptions,
        wake_word_models=body.wake_word_models,
        wake_word_disabled=body.wake_word_disabled,
        stt_language=body.stt_language,
        vad_confidence=body.vad_confidence,
        vad_min_volume=body.vad_min_volume,
        vad_stop_secs=body.vad_stop_secs,
        vad_start_secs=body.vad_start_secs,
        google_search_enabled=body.google_search_enabled,
        greeting_enabled=body.greeting_enabled,
        greeting_message=body.greeting_message,
        persist_conversation=body.persist_conversation,
        llm_provider=body.llm_provider,
        llm_model=body.llm_model,
        stt_provider=body.stt_provider,
        stt_model=body.stt_model,
        api_keys=body.api_keys,
        api_keys_clear=body.api_keys_clear,
        brave_api_key=body.brave_api_key,
    )
    if changed:
        # Compute a rough diff for logging — diff what the user POSTed
        # against what's now committed to the store. Skip the keys
        # themselves so we don't log secrets.
        posted_fields: list[str] = []
        for f in (
            "system_prompt",
            "tts_voice",
            "allow_interruptions",
            "wake_word_models",
            "wake_word_disabled",
            "stt_language",
            "vad_confidence",
            "vad_min_volume",
            "vad_stop_secs",
            "vad_start_secs",
            "google_search_enabled",
            "greeting_enabled",
            "greeting_message",
            "persist_conversation",
            "llm_provider",
            "llm_model",
            "stt_provider",
            "stt_model",
        ):
            if getattr(body, f, None) is not None:
                posted_fields.append(f)
        if body.api_keys:
            posted_fields.append(f"api_keys[{','.join(body.api_keys.keys())}]")
        if body.api_keys_clear:
            posted_fields.append(f"api_keys_clear[{','.join(body.api_keys_clear)}]")
        if body.brave_api_key is not None:
            posted_fields.append("brave_api_key")
        logger.info(
            "settings updated: %s | restarting %s session",
            ", ".join(posted_fields) or "(no fields)",
            session.mode,
        )
        await session.restart_in_place()
    return {
        "settings": settings_store.to_dict(),
        "changed": changed,
        # True when there's an active session (local or browser) that
        # picked up the changes immediately. Frontend uses this to show
        # "Saved · applied" vs "Saved · will apply on next session".
        "applied_to_running_session": (
            changed and session.mode in ("local", "browser")
        ),
    }


# --- Browser audio WebSocket ------------------------------------------------


async def _wait_until_ws_closed(websocket: WebSocket) -> None:
    """Poll the FastAPI WebSocket state until the client disconnects.

    Pipecat 0.0.108's FastAPIWebsocketTransport doesn't propagate WS closure
    back into the pipeline, so we watch the socket state ourselves and
    cancel the pipeline task when it goes DISCONNECTED. Without this,
    pressing "Hand back to local mic" closes the WS but leaves the pipeline
    task hanging forever.
    """
    while (
        websocket.client_state == WebSocketState.CONNECTED
        and websocket.application_state == WebSocketState.CONNECTED
    ):
        await asyncio.sleep(0.5)


@app.websocket("/bot-audio")
async def bot_audio(websocket: WebSocket) -> None:
    await websocket.accept()
    logger.info("Browser connected")

    pipeline_task: Optional[asyncio.Task] = None
    try:
        pipeline_task = await session.start_browser(websocket)
        watcher = asyncio.create_task(_wait_until_ws_closed(websocket))
        try:
            await asyncio.wait(
                {pipeline_task, watcher},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            watcher.cancel()
            if not pipeline_task.done():
                pipeline_task.cancel()
                try:
                    await pipeline_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.exception("Pipeline error during shutdown")
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    except Exception:
        logger.exception("Browser pipeline error")
    finally:
        logger.info("Browser disconnected")
        if pipeline_task is not None and session.is_owned_by(pipeline_task):
            session._active_browser_ws = None  # noqa: SLF001
            try:
                await session.start_local()
            except Exception:
                logger.exception("Failed to resume local pipeline")


def main() -> None:
    # log_config=None tells uvicorn to skip installing its own LOGGING
    # dict, which would otherwise overwrite our basicConfig + the
    # _MutePollingFilter on `uvicorn.access`. Without this the polling
    # endpoints flood stdout because uvicorn re-creates the handlers.
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=PORT,
        log_level="info",
        log_config=None,
    )


if __name__ == "__main__":
    main()
