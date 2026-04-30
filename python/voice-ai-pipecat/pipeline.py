"""Pipecat pipeline: pluggable STT -> LLM -> Piper TTS.

STT routes through faster-whisper (local CPU) by default, with optional
Deepgram streaming for lower latency. The LLM is one of Google Gemini,
OpenAI, Anthropic, or Groq, picked at runtime via /api/settings. Google
uses its native ``google_search`` grounding tool when search is enabled;
the other providers get a function-calling ``web_search`` backed by
Brave Search instead. Either way the assistant can answer real-world
questions ("what's the weather in San Francisco?") without the user
having to know which provider is wired in.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Optional

import numpy as np
from google.genai import types as genai_types

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InputAudioRawFrame,
    OutputAudioRawFrame,
    StartFrame,
    TextFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.google.llm import GoogleLLMService
from pipecat.services.piper.tts import PiperTTSService
from pipecat.services.whisper.stt import WhisperSTTService, Model
from pipecat.transports.base_transport import BaseTransport

_log = logging.getLogger("voice-ai-pipecat.pipeline")


# Lazy imports for optional providers — pipecat-ai's [deepgram], [openai],
# [anthropic], and [groq] extras pull in real SDKs we want to import only
# when used. Catch ImportError narrowly: a SyntaxError or AttributeError
# inside the imported module means the package IS installed but broken,
# and surfacing that is more useful than telling the user "extra not
# installed" when they clearly have it.
#
# Capture the post-ImportError exception per-provider so _build_*_service
# can quote the real reason ("Deepgram extra installed but import failed:
# <reason>") instead of misleading the user into reinstalling something
# that's already there.
_DEEPGRAM_LOAD_ERROR: Optional[str] = None
_OPENAI_LOAD_ERROR: Optional[str] = None
_ANTHROPIC_LOAD_ERROR: Optional[str] = None
_GROQ_LOAD_ERROR: Optional[str] = None
try:
    from pipecat.services.deepgram.stt import DeepgramSTTService
except ImportError:  # pragma: no cover
    DeepgramSTTService = None  # type: ignore[assignment]
except Exception as _exc:  # pragma: no cover
    _log.exception("Deepgram extra installed but failed to import")
    DeepgramSTTService = None  # type: ignore[assignment]
    _DEEPGRAM_LOAD_ERROR = str(_exc)
try:
    from pipecat.services.openai.llm import OpenAILLMService
except ImportError:  # pragma: no cover
    OpenAILLMService = None  # type: ignore[assignment]
except Exception as _exc:  # pragma: no cover
    _log.exception("OpenAI extra installed but failed to import")
    OpenAILLMService = None  # type: ignore[assignment]
    _OPENAI_LOAD_ERROR = str(_exc)
try:
    from pipecat.services.anthropic.llm import AnthropicLLMService
except ImportError:  # pragma: no cover
    AnthropicLLMService = None  # type: ignore[assignment]
except Exception as _exc:  # pragma: no cover
    _log.exception("Anthropic extra installed but failed to import")
    AnthropicLLMService = None  # type: ignore[assignment]
    _ANTHROPIC_LOAD_ERROR = str(_exc)
try:
    from pipecat.services.groq.llm import GroqLLMService
except ImportError:  # pragma: no cover
    GroqLLMService = None  # type: ignore[assignment]
except Exception as _exc:  # pragma: no cover
    _log.exception("Groq extra installed but failed to import")
    GroqLLMService = None  # type: ignore[assignment]
    _GROQ_LOAD_ERROR = str(_exc)


def _provider_unavailable(name: str, load_error: Optional[str]) -> RuntimeError:
    """Build a RuntimeError that distinguishes "extra not installed" from
    "extra installed but import failed" so the user gets actionable
    advice instead of being told to install something they already have."""
    if load_error:
        return RuntimeError(
            f"{name} extra installed but import failed: {load_error}. "
            "Check the server log for the full traceback."
        )
    return RuntimeError(
        f"{name} extra not installed. Reinstall pipecat-ai with the "
        f"matching extra (e.g. `pipecat-ai[{name.lower()}]`)."
    )


def _build_stt_service(
    provider: str,
    model: str,
    api_key: str,
    *,
    language: Optional[str] = None,
):
    """Construct the STT service for `provider`. Whisper is local
    (CPU, int8); Deepgram streams over WebSocket from their API and
    typically reaches first-token latency ~150–300 ms vs Whisper's
    1–3 s on CPU.

    `language` is an ISO-639-1 code or None for auto-detect.
    """
    if provider == "deepgram":
        if DeepgramSTTService is None:
            raise _provider_unavailable("Deepgram", _DEEPGRAM_LOAD_ERROR)
        if not api_key:
            raise RuntimeError(
                "Deepgram requires an API key. Set DEEPGRAM_API_KEY or "
                "configure it in the settings drawer."
            )
        kwargs: dict = {
            "api_key": api_key,
            "sample_rate": 16000,
        }
        if model:
            kwargs["model"] = model
        if language and language.lower() not in {"auto", ""}:
            kwargs["language"] = language
        return DeepgramSTTService(**kwargs)

    # Default to Whisper.
    whisper_settings_kwargs: dict = {"model": model or Model.TINY.value}
    if language and language.lower() not in {"auto", ""}:
        whisper_settings_kwargs["language"] = language
    try:
        return WhisperSTTService(
            settings=WhisperSTTService.Settings(**whisper_settings_kwargs),
        )
    except TypeError as exc:
        # Older pipecat 0.0.x didn't expose `language` on Settings (it
        # moved up to the STT base class around 0.0.105). Fall back to
        # auto-detect rather than crashing pipeline construction.
        if "language" in whisper_settings_kwargs and "language" in str(exc):
            _log.warning(
                "WhisperSTTService.Settings rejected `language=%r` (%s); "
                "falling back to auto-detect. Upgrade pipecat-ai to "
                ">=0.0.105 to honor the language picker.",
                whisper_settings_kwargs["language"],
                exc,
            )
            whisper_settings_kwargs.pop("language")
            return WhisperSTTService(
                settings=WhisperSTTService.Settings(**whisper_settings_kwargs),
            )
        raise


# --- Built-in function tools (time / date / math). Always registered for
# non-Google providers, and for Google when google_search_enabled is
# False — Gemini's API treats google_search as mutually exclusive with
# function declarations, so search-on means search-only and search-off
# means function-tools instead. Each tool is a (FunctionSchema,
# async-handler) pair. The handler signature matches Pipecat's
# FunctionCallParams contract.


_GET_CURRENT_TIME_SCHEMA = FunctionSchema(
    name="get_current_time",
    description=(
        "Get the current local time. Use this when the user asks 'what "
        "time is it', 'what's the time in <city>', or anything time-of-"
        "day related. Optional `timezone` argument is an IANA name like "
        "'America/Los_Angeles' or 'Europe/London'."
    ),
    properties={
        "timezone": {
            "type": "string",
            "description": "IANA timezone name, or empty for the device's local time.",
        }
    },
    required=[],
)


_GET_CURRENT_DATE_SCHEMA = FunctionSchema(
    name="get_current_date",
    description=(
        "Get the current date. Use this when the user asks 'what's "
        "today's date', 'what day is it', or anything date-related."
    ),
    properties={},
    required=[],
)


_DO_MATH_SCHEMA = FunctionSchema(
    name="do_math",
    description=(
        "Evaluate a basic arithmetic expression. Use this for any "
        "calculation rather than guessing — e.g. '12 * 34', "
        "'(100 - 32) * 5/9'. Supports +, -, *, /, **, parentheses."
    ),
    properties={
        "expression": {
            "type": "string",
            "description": "The expression to evaluate.",
        }
    },
    required=["expression"],
)


async def _fn_get_current_time(params) -> None:  # type: ignore[no-untyped-def]
    from datetime import datetime

    tz_name = ((params.arguments or {}).get("timezone") or "").strip()
    tz = None
    if tz_name:
        try:
            from zoneinfo import ZoneInfo

            tz = ZoneInfo(tz_name)
        except Exception:
            await params.result_callback(
                {"error": f"Unknown timezone: {tz_name}"}
            )
            return
    now = datetime.now(tz=tz) if tz else datetime.now()
    await params.result_callback(
        {
            "iso": now.isoformat(),
            "human": now.strftime("%I:%M %p").lstrip("0"),
            "timezone": tz_name or "local",
        }
    )


async def _fn_get_current_date(params) -> None:  # type: ignore[no-untyped-def]
    from datetime import datetime

    now = datetime.now()
    await params.result_callback(
        {
            "iso": now.date().isoformat(),
            "human": now.strftime("%A, %B %-d, %Y"),
        }
    )


async def _fn_do_math(params) -> None:  # type: ignore[no-untyped-def]
    expression = ((params.arguments or {}).get("expression") or "").strip()
    if not expression:
        await params.result_callback({"error": "No expression provided"})
        return
    try:
        # simpleeval only allows a small whitelist of operations and
        # functions, so it's safe to feed user-driven expressions in.
        from simpleeval import simple_eval

        result = simple_eval(expression)
        await params.result_callback(
            {"expression": expression, "result": result}
        )
    except Exception as exc:
        await params.result_callback({"error": f"Couldn't evaluate: {exc}"})


def _trace_tool(name: str, fn):  # type: ignore[no-untyped-def]
    """Wrap a tool handler with entry/exit logging + duration."""

    async def wrapped(params) -> None:  # type: ignore[no-untyped-def]
        args = params.arguments or {}
        # Trim args for the log so we don't spam huge prompts.
        trimmed = {
            k: (v[:80] + "…") if isinstance(v, str) and len(v) > 80 else v
            for k, v in args.items()
        }
        _log.info("tool %s start: args=%s", name, trimmed)
        start = time.monotonic()
        try:
            await fn(params)
        except Exception:
            _log.exception("tool %s crashed", name)
            raise
        finally:
            elapsed = int((time.monotonic() - start) * 1000)
            _log.info("tool %s done: %dms", name, elapsed)

    return wrapped


def _builtin_function_tools() -> list[tuple]:
    """Pairs of (FunctionSchema, handler) registered for non-Google
    providers regardless of search settings. Web search is added
    separately when a Brave key is configured."""
    return [
        (_GET_CURRENT_TIME_SCHEMA, _fn_get_current_time),
        (_GET_CURRENT_DATE_SCHEMA, _fn_get_current_date),
        (_DO_MATH_SCHEMA, _fn_do_math),
    ]


def _build_llm_service(
    provider: str,
    model: str,
    api_key: str,
    *,
    google_search_enabled: bool,
    function_search_enabled: bool,
    brave_api_key: str,
):
    """Construct the right Pipecat LLM service for `provider`.

    Returns ``(service, tools_schema, handlers)``:
      - ``service``: the LLMService instance to insert in the pipeline.
      - ``tools_schema``: ToolsSchema covering all built-in tools for
        OpenAI-style providers, or None when Google's native
        google_search grounding is in use.
      - ``handlers``: list of ``(tool_name, handler_or_marker)`` pairs
        the caller must register on the service. ``handler_or_marker``
        is either an async callable wrapped by ``_trace_tool``, or a
        ``("__brave__", api_key)`` marker telling the caller to build
        a Brave-search closure with the captured key. Markers exist
        because the closure can't be constructed at factory time
        without leaking the key out of this function. None when no
        function tools apply (Google with native search).
    """
    if provider == "google":
        kwargs = {
            "api_key": api_key,
            "settings": GoogleLLMService.Settings(model=model),
        }
        if google_search_enabled:
            kwargs["tools"] = [
                genai_types.Tool(google_search=genai_types.GoogleSearch())
            ]
            return GoogleLLMService(**kwargs), None, None
        # Search disabled — register built-in function tools so the model
        # still has time/date/math. Gemini doesn't allow mixing
        # google_search with function declarations, so we never register
        # both. Web search via Brave is also off in this branch by design
        # (a Google user with search disabled has opted out of the web).
        google_schemas: list[FunctionSchema] = []
        google_handlers: list[tuple] = []
        for schema, fn in _builtin_function_tools():
            google_schemas.append(schema)
            google_handlers.append((schema.name, _trace_tool(schema.name, fn)))
        google_tools_schema = ToolsSchema(standard_tools=google_schemas)
        return GoogleLLMService(**kwargs), google_tools_schema, google_handlers

    # OpenAI / Anthropic / Groq — function-call based.
    schemas: list[FunctionSchema] = []
    handlers: list[tuple] = []  # list of (name, callable)
    # Built-in tools (time, date, math) always available.
    for schema, fn in _builtin_function_tools():
        schemas.append(schema)
        handlers.append((schema.name, _trace_tool(schema.name, fn)))
    # Web search via Brave when enabled.
    if function_search_enabled and brave_api_key:
        search_fn = FunctionSchema(
            name="web_search",
            description=(
                "Search the web for up-to-date information about news, "
                "weather, sports scores, prices, business hours, or "
                "anything that might have changed recently. ALWAYS use "
                "this when the user asks about current state."
            ),
            properties={
                "query": {
                    "type": "string",
                    "description": "The search query to send to the web.",
                }
            },
            required=["query"],
        )
        schemas.append(search_fn)
        # Lazy: actual handler closure built in build_pipeline_task
        # because it needs the captured Brave key. We just mark it.
        handlers.append(("web_search", ("__brave__", brave_api_key)))
    tools_schema = ToolsSchema(standard_tools=schemas) if schemas else None
    handler = handlers if handlers else None

    if provider == "openai":
        if OpenAILLMService is None:
            raise _provider_unavailable("OpenAI", _OPENAI_LOAD_ERROR)
        return OpenAILLMService(api_key=api_key, model=model), tools_schema, handler
    if provider == "anthropic":
        if AnthropicLLMService is None:
            raise _provider_unavailable("Anthropic", _ANTHROPIC_LOAD_ERROR)
        return AnthropicLLMService(api_key=api_key, model=model), tools_schema, handler
    if provider == "groq":
        if GroqLLMService is None:
            raise _provider_unavailable("Groq", _GROQ_LOAD_ERROR)
        return GroqLLMService(api_key=api_key, model=model), tools_schema, handler

    raise ValueError(f"Unknown LLM provider: {provider!r}")


def _make_chime(freq_hz: int, duration_ms: int, sample_rate: int, volume: float = 0.3) -> bytes:
    """Synthesize a short tone with linear fade in/out as int16 PCM bytes."""
    n = int(sample_rate * duration_ms / 1000)
    t = np.linspace(0, duration_ms / 1000.0, n, endpoint=False)
    wave = np.sin(2 * np.pi * freq_hz * t)
    fade_n = int(sample_rate * 0.010)  # 10 ms fade
    env = np.ones(n)
    env[:fade_n] = np.linspace(0, 1, fade_n)
    env[-fade_n:] = np.linspace(1, 0, fade_n)
    wave = wave * env * volume
    return (wave * 32767).astype(np.int16).tobytes()


class PipelineStateTracker(FrameProcessor):
    """Surface 'thinking' state and per-turn latency to the backend.

    Hooks into the user-stopped / bot-started transitions and notifies
    the SessionManager (via callbacks) so /api/status can drive the
    frontend status pill (Listening → Thinking → Speaking) and show
    the time-to-first-byte for the most recent turn.
    """

    def __init__(
        self,
        on_user_stopped: Optional[callable] = None,  # type: ignore[type-arg]
        on_bot_started: Optional[callable] = None,  # type: ignore[type-arg]
        on_bot_stopped: Optional[callable] = None,  # type: ignore[type-arg]
    ) -> None:
        super().__init__()
        self._on_user_stopped = on_user_stopped
        self._on_bot_started = on_bot_started
        self._on_bot_stopped = on_bot_stopped

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, UserStoppedSpeakingFrame) and self._on_user_stopped:
            try:
                self._on_user_stopped()
            except Exception:
                _log.exception("PipelineStateTracker: user-stopped callback failed")
        elif isinstance(frame, BotStartedSpeakingFrame) and self._on_bot_started:
            try:
                self._on_bot_started()
            except Exception:
                _log.exception("PipelineStateTracker: bot-started callback failed")
        elif isinstance(frame, BotStoppedSpeakingFrame) and self._on_bot_stopped:
            try:
                self._on_bot_stopped()
            except Exception:
                _log.exception("PipelineStateTracker: bot-stopped callback failed")
        await self.push_frame(frame, direction)


class STTUserTextCapture(FrameProcessor):
    """Logs each finalized user transcription and stashes the last one.

    Place RIGHT AFTER the STT service. The user-context aggregator
    downstream consumes TranscriptionFrame to build the LLM's message
    list, so a logger placed past it never sees these frames. The
    captured text is exposed via ``last_user_text`` so a downstream
    BotResponseLogger can pair it with the bot's reply for persistence.
    """

    def __init__(self) -> None:
        super().__init__()
        self.last_user_text: Optional[str] = None

    def consume_user_text(self) -> Optional[str]:
        text = self.last_user_text
        self.last_user_text = None
        return text

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame):
            text = (getattr(frame, "text", "") or "").strip()
            if text:
                self.last_user_text = text
                _log.info("STT ▸ %s", text)
        await self.push_frame(frame, direction)


class BotResponseLogger(FrameProcessor):
    """Accumulates LLM-emitted text per turn, logs it, and optionally
    persists the (user, bot) pair via ``on_turn_complete``.

    Place RIGHT AFTER the LLM service, before TTS — LLM streams TextFrame
    chunks on their way to TTS, and the assistant context aggregator
    placed at the tail of the pipeline consumes them, so a processor
    placed any later never sees them. The user transcript is read at
    bot-stopped time from a paired ``STTUserTextCapture`` so we don't
    need a second processor placement.
    """

    def __init__(
        self,
        user_capture: Optional["STTUserTextCapture"] = None,
        on_turn_complete: Optional[callable] = None,  # type: ignore[type-arg]
    ) -> None:
        super().__init__()
        self._user_capture = user_capture
        self._on_turn_complete = on_turn_complete
        self._chunks: list[str] = []
        self._collecting = False

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, BotStartedSpeakingFrame):
            self._chunks = []
            self._collecting = True
        elif isinstance(frame, TextFrame) and self._collecting:
            text = getattr(frame, "text", None)
            if text:
                self._chunks.append(text)
        elif isinstance(frame, BotStoppedSpeakingFrame):
            if self._chunks:
                full = "".join(self._chunks).strip()
                if full:
                    _log.info("TTS ▸ %s", full)
                    if self._on_turn_complete and self._user_capture is not None:
                        user_text = self._user_capture.consume_user_text()
                        if user_text:
                            try:
                                self._on_turn_complete(user_text, full)
                            except Exception:
                                _log.exception("BotResponseLogger: on_turn_complete failed")
                        else:
                            # Bot reply with no paired user text — the
                            # greeting, a tool-only response, or a
                            # dropped STT transcript. Without this log
                            # the turn is silently absent from the
                            # persisted history with no signal.
                            _log.warning(
                                "BotResponseLogger: bot reply (%d chars) "
                                "without paired user text — turn not persisted",
                                len(full),
                            )
            self._chunks = []
            self._collecting = False
        await self.push_frame(frame, direction)


class TurnTelemetry(FrameProcessor):
    """Single-line timing summary per user/bot turn.

    Hooks the frame transitions Pipecat already emits and logs once
    per turn at INFO. Output looks like:

        turn complete: user='What time is it?' stt=187ms llm_ttft=412ms tts_ttft=189ms total=2104ms

    `stt`     time from end-of-user-speech to finalized transcription
    `llm_ttft` time from STT-done to first LLM text token
    `tts_ttft` time from first LLM token to first bot audio out
    `total`    end-of-user-speech to first bot audio out (= what the
               user perceives as "how long did the bot take to start
               replying")
    """

    def __init__(self) -> None:
        super().__init__()
        self._reset()

    def _reset(self) -> None:
        self._user_stopped_at: Optional[float] = None
        self._stt_done_at: Optional[float] = None
        self._llm_first_at: Optional[float] = None
        self._tts_first_at: Optional[float] = None
        self._user_text: str = ""

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        now = time.monotonic()

        if isinstance(frame, UserStoppedSpeakingFrame):
            self._reset()
            self._user_stopped_at = now
        elif isinstance(frame, TranscriptionFrame):
            text = (getattr(frame, "text", "") or "").strip()
            if text and self._user_stopped_at is not None:
                self._stt_done_at = now
                self._user_text = text
        elif isinstance(frame, TextFrame):
            # First LLM text chunk — time-to-first-token.
            if self._stt_done_at is not None and self._llm_first_at is None:
                self._llm_first_at = now
        elif isinstance(frame, BotStartedSpeakingFrame):
            # Only count this as a turn's TTS-first if STT actually
            # produced a transcript. Otherwise the greeting / a stray
            # bot utterance not triggered by the user gets logged as
            # "turn complete: user='' total=4s" which is misleading.
            if self._tts_first_at is None and self._stt_done_at is not None:
                self._tts_first_at = now
        elif isinstance(frame, BotStoppedSpeakingFrame):
            # Only emit when there was a real user-driven turn.
            if self._user_text and self._tts_first_at is not None:
                self._emit(now)
            self._reset()

        await self.push_frame(frame, direction)

    def _emit(self, end: float) -> None:
        if self._user_stopped_at is None:
            return

        def ms(a: Optional[float], b: Optional[float]) -> str:
            if a is None or b is None:
                return "?"
            return f"{int((b - a) * 1000)}ms"

        stt = ms(self._user_stopped_at, self._stt_done_at)
        llm = ms(self._stt_done_at, self._llm_first_at)
        tts = ms(self._llm_first_at, self._tts_first_at)
        total = ms(self._user_stopped_at, self._tts_first_at)
        snippet = self._user_text[:60] + ("…" if len(self._user_text) > 60 else "")
        _log.info(
            "turn complete: user=%r stt=%s llm_ttft=%s tts_ttft=%s total=%s",
            snippet,
            stt,
            llm,
            tts,
            total,
        )


class GreetingAnnouncer(FrameProcessor):
    """Speak a greeting once when the pipeline first becomes ready.

    Schedules a TTSSpeakFrame downstream after a small delay following
    the first StartFrame. The frame skips the LLM (it carries pre-formed
    text) and goes straight to TTS, so the bot announces itself as soon
    as the STT / LLM / TTS services are loaded — useful UX cue that the
    user can start talking. The flag is per-instance, so a new pipeline
    (e.g. after a browser hand-off) re-greets; that's intentional.

    The delay matters: pushing audio frames the same tick as StartFrame
    races with PortAudio's output stream setup. The first attempt to
    write a TTSAudioRawFrame can hit `paUnanticipatedHostError` and kill
    the playback stream. Waiting ~1.5s lets the output transport fully
    prime before any audio reaches it.
    """

    def __init__(self, message: str, delay_secs: float = 1.5) -> None:
        super().__init__()
        self._message = message
        self._delay_secs = delay_secs
        self._announced = False
        self._delayed_task: Optional[asyncio.Task] = None

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)
        if isinstance(frame, StartFrame) and not self._announced and self._message:
            self._announced = True
            self._delayed_task = asyncio.create_task(self._delayed_announce())

    async def _delayed_announce(self) -> None:
        try:
            await asyncio.sleep(self._delay_secs)
            await self.push_frame(
                TTSSpeakFrame(self._message), FrameDirection.DOWNSTREAM
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("GreetingAnnouncer: failed to push greeting")

    async def cleanup(self) -> None:
        """Cancel a pending greeting if the pipeline tears down before it
        fires. Without this the leaked task can wake into a torn-down
        transport and raise "Task was destroyed but it is pending"."""
        await super().cleanup()
        if self._delayed_task is not None and not self._delayed_task.done():
            self._delayed_task.cancel()
            try:
                await self._delayed_task
            except asyncio.CancelledError:
                pass
            except Exception:
                _log.exception(
                    "GreetingAnnouncer: delayed announce errored during cleanup"
                )
        self._delayed_task = None


class WakeWordGate(FrameProcessor):
    """Wake-word activated audio gate.

    Sits between the input transport and STT. Always runs openWakeWord
    over incoming audio. Until the wake word fires the gate drops audio
    and VAD speaking events so STT/LLM never see anything — saves cost
    and stops the bot from replying to ambient conversation. When the
    wake word triggers we play a short rising chime, open a listening
    window, and forward the user's next utterance through to STT. The
    window closes after the user stops speaking (one full utterance) or
    after `max_listen_secs`, whichever comes first; a falling chime
    plays as the window closes so the user knows it's no longer
    listening.

    Models come from openwakeword's pretrained set: "alexa",
    "hey_jarvis", "hey_mycroft", "hey_rhasspy", "ok_nabu". Configure
    them through the settings drawer (which writes to
    /api/settings.wake_word_models); the WAKE_WORD_MODELS env var is a
    fallback for first-boot before the user opens the drawer. There is
    no pretrained "Hey Wendy" — that requires training a custom model.

    If openwakeword's predict() starts raising every tick (model file
    corrupt, ONNX runtime mismatch), the gate stays closed forever and
    the user has no way to wake the bot. After ``predict_error_limit``
    consecutive failures we invoke ``on_predict_error`` so the
    SessionManager can surface the problem on /api/status.
    """

    def __init__(
        self,
        models: list[str],
        threshold: float = 0.5,
        max_listen_secs: float = 8.0,
        output_sample_rate: int = 48000,
        on_wake_fired: Optional[callable] = None,  # type: ignore[type-arg]
        is_bot_speaking: Optional[callable] = None,  # type: ignore[type-arg]
        on_predict_error: Optional[callable] = None,  # type: ignore[type-arg]
        predict_error_limit: int = 20,
        continuous_conversation: bool = False,
        continuous_window_secs: float = 6.0,
    ) -> None:
        super().__init__()
        from openwakeword.model import Model as OWWModel

        # openWakeWord mutates the list it's given to contain resolved
        # model paths. Pass a copy so the caller's `models` list keeps
        # its friendly names like ["hey_jarvis"] for our build logs.
        self._oww = OWWModel(
            wakeword_models=list(models), inference_framework="onnx"
        )
        self._models = list(models)
        self._threshold = threshold
        self._max_listen_secs = max_listen_secs
        self._listening_until: Optional[float] = None
        self._on_wake_fired = on_wake_fired
        # Pre-synthesize chimes at the output transport's sample rate so
        # they play through cleanly without resampling. Caller must pass
        # the actual transport rate — defaulting to 48000 here would
        # play the chime at 1/3 speed if the transport runs at 16000.
        self._chime_open = _make_chime(880, 120, output_sample_rate)
        self._chime_close = _make_chime(550, 100, output_sample_rate, volume=0.2)
        self._output_sample_rate = output_sample_rate
        # Gate the wake-word inference itself for ~250 ms after we play
        # the chime so the chime doesn't echo back into the detector.
        self._mute_oww_until: float = 0.0
        # Callable that reports whether the bot is currently producing
        # TTS audio. The frame events (BotStartedSpeakingFrame /
        # BotStoppedSpeakingFrame) flow downstream from the output
        # transport and never reach this gate, which sits at the front
        # of the pipeline. So we ask the SessionManager via callback
        # instead. Without this guard, the bot's own TTS leaks back
        # through the mic and openWakeWord matches phonemes in it as
        # "hey jarvis" at score 0.99–1.00 every ~8 s.
        self._is_bot_speaking = is_bot_speaking
        self._on_predict_error = on_predict_error
        self._predict_error_limit = max(1, predict_error_limit)
        self._consecutive_predict_errors: int = 0
        self._predict_error_notified: bool = False
        # Continuous-conversation: re-open the listening window
        # automatically after the bot finishes speaking, so the user can
        # ask a follow-up without re-saying the wake word. We detect the
        # bot's speaking→silent transition by polling is_bot_speaking()
        # each input frame and watching the edge.
        self._continuous_conversation = continuous_conversation
        self._continuous_window_secs = continuous_window_secs
        self._bot_was_speaking: bool = False

    @property
    def _in_window(self) -> bool:
        return (
            self._listening_until is not None
            and time.monotonic() < self._listening_until
        )

    async def _push_chime(self, audio: bytes) -> None:
        await self.push_frame(
            OutputAudioRawFrame(
                audio=audio,
                sample_rate=self._output_sample_rate,
                num_channels=1,
            ),
            FrameDirection.DOWNSTREAM,
        )

    async def _close_window(self) -> None:
        if self._listening_until is None:
            return
        self._listening_until = None
        self._mute_oww_until = time.monotonic() + 0.25
        await self._push_chime(self._chime_close)
        _log.debug("WakeWordGate: closed listening window")

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        now = time.monotonic()

        # Auto-close window if max-listen elapsed without UserStoppedSpeaking.
        if self._listening_until is not None and now >= self._listening_until:
            await self._close_window()

        bot_speaking = bool(self._is_bot_speaking and self._is_bot_speaking())

        # Continuous-conversation edge detect: bot just stopped talking.
        # Re-open the listening window for a follow-up so the user can
        # speak again without re-saying the wake word. Done here (not on
        # a downstream frame) because the gate sits at the front of the
        # pipeline and never sees BotStoppedSpeakingFrame. The 500 ms
        # tail in is_bot_currently_speaking() means this fires safely
        # after any TTS audio has cleared the speakers.
        if (
            self._continuous_conversation
            and self._bot_was_speaking
            and not bot_speaking
            and not self._in_window
        ):
            self._listening_until = now + self._continuous_window_secs
            self._mute_oww_until = now + 0.25
            _log.info(
                "WakeWordGate: continuous mode — follow-up window opened (%.1fs)",
                self._continuous_window_secs,
            )
            # Deliberately no chime and no on_wake_fired callback here —
            # that's reserved for actual wake-word triggers. A follow-up
            # is meant to feel like "still talking", not a fresh wake.
        self._bot_was_speaking = bot_speaking

        if isinstance(frame, InputAudioRawFrame):
            # Run wake detector unless we just played a chime, are
            # already inside a listening window, or the bot is
            # currently speaking (its TTS leaks back into the mic).
            if (
                now >= self._mute_oww_until
                and not self._in_window
                and not bot_speaking
            ):
                audio = np.frombuffer(frame.audio, dtype=np.int16)
                try:
                    prediction = self._oww.predict(audio)
                    self._consecutive_predict_errors = 0
                except Exception as exc:
                    _log.exception("WakeWordGate: predict failed")
                    prediction = {}
                    self._consecutive_predict_errors += 1
                    if (
                        self._consecutive_predict_errors >= self._predict_error_limit
                        and not self._predict_error_notified
                        and self._on_predict_error
                    ):
                        # Without this notification the gate stays closed
                        # forever and the user has no signal — the device
                        # appears unresponsive to the wake word.
                        self._predict_error_notified = True
                        try:
                            self._on_predict_error(str(exc))
                        except Exception:
                            _log.exception("WakeWordGate: on_predict_error failed")
                if prediction:
                    score = max(prediction.values())
                    if score >= self._threshold:
                        triggered = max(prediction, key=prediction.get)
                        _log.info(
                            "WakeWordGate: '%s' fired (score=%.2f), opening window",
                            triggered,
                            score,
                        )
                        self._listening_until = now + self._max_listen_secs
                        self._mute_oww_until = now + 0.25
                        if self._on_wake_fired:
                            try:
                                self._on_wake_fired()
                            except Exception:
                                _log.exception("WakeWordGate: on_wake_fired failed")
                        await self._push_chime(self._chime_open)
                        return  # don't forward this frame to STT
            if self._in_window:
                await self.push_frame(frame, direction)
            return

        if isinstance(frame, (UserStartedSpeakingFrame, UserStoppedSpeakingFrame)):
            if self._in_window:
                await self.push_frame(frame, direction)
                if isinstance(frame, UserStoppedSpeakingFrame):
                    # One utterance complete — close the window.
                    await self._close_window()
            return

        # Pass everything else through (StartFrame, EndFrame, etc.).
        await self.push_frame(frame, direction)


class StartupAudioGate(FrameProcessor):
    """Drop audio frames for `warmup_secs` after pipeline start.

    PyAudio begins capturing the moment the input stream opens, but the
    pipeline takes a few seconds to fully initialize (Whisper, Piper,
    LLM). Anything the user said during that gap would otherwise queue
    up in Pipecat's frame queue and get processed in a burst as soon as
    the pipeline goes live — the bot tries to answer the last three
    stale questions in sequence. Eat the audio for an initial window so
    every session starts clean.

    Non-audio frames (StartFrame, EndFrame, CancelFrame, etc.) pass
    through untouched.
    """

    def __init__(self, warmup_secs: float = 2.0) -> None:
        super().__init__()
        self._warmup_secs = warmup_secs
        self._gate_open_at: Optional[float] = None

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, StartFrame) and self._gate_open_at is None:
            self._gate_open_at = time.monotonic() + self._warmup_secs
        if isinstance(frame, InputAudioRawFrame) and self._gate_open_at is not None:
            if time.monotonic() < self._gate_open_at:
                return
        await self.push_frame(frame, direction)


# Push the model toward search so questions about current state ("weather
# in SF today", "score of the Lakers game", "who won the election")
# actually hit the grounding tool instead of getting the canned "I don't
# have real-time information" disclaimer.
SYSTEM_PROMPT = (
    "You are a terse voice assistant on a Wendy device.\n"
    "\n"
    "Output rules:\n"
    "- Reply in ONE short sentence. Aim for ~15 words.\n"
    "- Answer only what was asked. Do NOT add definitions, context, "
    "conversions, follow-up offers, or 'is there anything else' lines.\n"
    "- No bullet points, no lists, no explanations of how something "
    "works unless explicitly asked.\n"
    "- If you don't know, say 'I don't know' in one sentence — don't "
    "explain why.\n"
    "\n"
    "Tools:\n"
    "- You have `google_search` (or `web_search`, whichever is "
    "available) for live data. ALWAYS use it for current weather, "
    "news, scores, prices, business hours, or anything date-dependent. "
    "Don't say you can't access real-time information — search first "
    "and answer from the result.\n"
    "\n"
    "Examples (this is the level of brevity required):\n"
    "Q: What's the weather in San Francisco?\n"
    "A: 54°F and sunny in San Francisco.\n"
    "Q: What time is it in Tokyo?\n"
    "A: It's 3:42 AM in Tokyo.\n"
    "Q: What's my name?\n"
    "A: I don't know your name.\n"
)


DEFAULT_SYSTEM_PROMPT = SYSTEM_PROMPT


# Preset prompts the frontend exposes as one-click buttons. Each button
# loads its preset into the system-prompt textarea; the user can save
# as-is or edit further. The tool block names `google_search` *or*
# `web_search` so the same prompt works whether Google native search is
# active (Gemini provider) or the Brave-backed function tool is active
# (OpenAI / Anthropic / Groq).
_TOOL_BLOCK = (
    "\n\nTools:\n- You have `google_search` (or `web_search`, whichever is "
    "available) for live data. ALWAYS use it for current weather, news, "
    "scores, prices, business hours, or anything date-dependent. Don't "
    "say you can't access real-time information — search first and "
    "answer from the result.\n"
)

PROMPT_PRESETS: dict[str, str] = {
    "concise": SYSTEM_PROMPT,
    "conversational": (
        "You are a friendly voice assistant on a Wendy device. Reply in "
        "one or two natural-sounding sentences (~25 words). Be warm but "
        "not chatty — answer the question, optionally add one sentence "
        "of helpful context, then stop. No bullet points, no lists, no "
        "'is there anything else' lines unless the user clearly wants "
        "to keep going."
        + _TOOL_BLOCK
    ),
    "playful": (
        "You are a playful, slightly witty voice assistant on a Wendy "
        "device. Reply in ONE short sentence with a light touch — a "
        "small joke or wry observation is fine, but never at the "
        "expense of the actual answer. Aim for ~15 words. No bullet "
        "points, no lists. If you don't know, admit it crisply."
        + _TOOL_BLOCK
    ),
}


def build_pipeline_task(
    transport: BaseTransport,
    *,
    system_prompt: Optional[str] = None,
    tts_voice: Optional[str] = None,
    allow_interruptions: Optional[bool] = None,
    wake_word_models: Optional[list[str]] = None,
    wake_word_disabled: Optional[bool] = None,
    continuous_conversation: bool = False,
    continuous_window_secs: float = 6.0,
    stt_language: Optional[str] = None,
    google_search_enabled: Optional[bool] = None,
    greeting_message: Optional[str] = None,
    conversation_history: Optional[list[dict[str, str]]] = None,
    on_user_stopped: Optional[callable] = None,  # type: ignore[type-arg]
    on_bot_started: Optional[callable] = None,  # type: ignore[type-arg]
    on_bot_stopped: Optional[callable] = None,  # type: ignore[type-arg]
    on_wake_fired: Optional[callable] = None,  # type: ignore[type-arg]
    on_wake_predict_error: Optional[callable] = None,  # type: ignore[type-arg]
    on_turn_complete: Optional[callable] = None,  # type: ignore[type-arg]
    is_bot_speaking: Optional[callable] = None,  # type: ignore[type-arg]
    llm_provider: str = "google",
    llm_model: str = "gemini-2.5-flash",
    llm_api_key: str = "",
    brave_api_key: str = "",
    stt_provider: str = "whisper",
    stt_model: str = "tiny",
    stt_api_key: str = "",
    output_sample_rate: int = 48000,
) -> PipelineTask:
    """Build the Pipecat pipeline task wired around `transport`.

    All optional kwargs come from the user-editable settings store
    (/api/settings). The SessionManager re-reads them on every pipeline
    start so saving in the UI applies on the next utterance.

    `stt_language`: "auto" or "" → STT provider auto-detects; otherwise
    an ISO-639-1 code like "en", "es", "fr". Both Whisper and Deepgram
    accept the same code set.
    `google_search_enabled`: gates BOTH Gemini's native ``google_search``
    tool AND the Brave-backed ``web_search`` function for non-Google
    providers (offline / privacy mode). When off, Google falls back to
    the built-in time/date/math function tools instead — Gemini's API
    treats search and function declarations as mutually exclusive.
    """

    prompt = system_prompt or SYSTEM_PROMPT
    voice = tts_voice or "en_US-lessac-medium"
    interrupt = bool(allow_interruptions) if allow_interruptions is not None else False
    wake_disabled = bool(wake_word_disabled) if wake_word_disabled is not None else False
    search_enabled = bool(google_search_enabled) if google_search_enabled is not None else True

    _log.info(
        "building pipeline: llm=%s/%s stt=%s/%s tts=%s wake=%s "
        "continuous=%s search=%s interrupt=%s history=%d",
        llm_provider,
        llm_model,
        stt_provider,
        stt_model,
        voice,
        "off"
        if wake_disabled
        else (",".join(wake_word_models or ["hey_jarvis"])),
        f"on({continuous_window_secs:.1f}s)" if continuous_conversation else "off",
        "google-native"
        if (llm_provider == "google" and search_enabled)
        else (
            "brave-fn"
            if (search_enabled and brave_api_key)
            else "off"
        ),
        "on" if interrupt else "off",
        len(conversation_history) if conversation_history else 0,
    )

    stt = _build_stt_service(
        stt_provider,
        stt_model,
        stt_api_key,
        language=stt_language,
    )

    # Pick the right LLM service based on provider. For Google we use
    # native google_search grounding (no function calls). For
    # OpenAI/Anthropic/Groq we register a `web_search` function backed
    # by Brave Search — the LLM decides when to call it.
    api_key_for_llm = llm_api_key or os.environ.get("GOOGLE_API_KEY", "")
    llm, tools_schema, handler_spec = _build_llm_service(
        llm_provider,
        llm_model,
        api_key_for_llm,
        google_search_enabled=(llm_provider == "google" and search_enabled),
        function_search_enabled=(llm_provider != "google" and search_enabled),
        brave_api_key=brave_api_key,
    )
    if handler_spec is not None:
        # `handler_spec` is a list of (name, handler_or_marker) pairs.
        # Markers like `("__brave__", api_key)` indicate "build a Brave
        # search closure with this key" — done here so the closure
        # doesn't leak through the factory return value.
        for name, ref in handler_spec:
            if (
                isinstance(ref, tuple)
                and len(ref) == 2
                and ref[0] == "__brave__"
            ):
                captured_key = ref[1]

                # The Brave key is baked into _web_search's default arg
                # at pipeline-build time. Rotating the key via
                # /api/settings won't reach this closure — but the
                # settings endpoint calls SessionManager.restart_in_place
                # on local mode, which rebuilds the pipeline (and this
                # closure) with the new value. Browser sessions pick up
                # the new key on next reconnect.
                async def _web_search(params, _key=captured_key) -> None:  # type: ignore[no-untyped-def]
                    import httpx

                    query = (params.arguments or {}).get("query", "")
                    if not _key:
                        await params.result_callback(
                            {"error": "Web search not configured"}
                        )
                        return
                    try:
                        async with httpx.AsyncClient(timeout=10) as client:
                            resp = await client.get(
                                "https://api.search.brave.com/res/v1/web/search",
                                headers={
                                    "X-Subscription-Token": _key,
                                    "Accept": "application/json",
                                },
                                params={"q": query, "count": 3},
                            )
                        if resp.status_code != 200:
                            await params.result_callback(
                                {"error": f"Search returned {resp.status_code}"}
                            )
                            return
                        data = resp.json()
                        results = data.get("web", {}).get("results", [])
                        top = [
                            {
                                "title": r.get("title", ""),
                                "url": r.get("url", ""),
                                "snippet": r.get("description", ""),
                            }
                            for r in results[:3]
                        ]
                        await params.result_callback(
                            {"results": top, "query": query}
                        )
                    except Exception as exc:
                        _log.exception("Brave search failed")
                        await params.result_callback({"error": f"Search failed: {exc}"})

                llm.register_function(name, _trace_tool(name, _web_search))
            else:
                # Regular handler — already wrapped by _trace_tool at
                # factory time.
                llm.register_function(name, ref)

    tts = PiperTTSService(
        settings=PiperTTSService.Settings(voice=voice),
        download_dir=Path("/app/models/piper"),
        sample_rate=16000,
    )

    # Function-call tools (OpenAI/Anthropic/Groq, plus Google when
    # google_search is off) live on the context aggregator. Google's
    # native google_search tool is set on the service itself in
    # _build_llm_service instead and tools_schema comes back as None.
    initial_messages: list[dict] = [{"role": "system", "content": prompt}]
    if conversation_history:
        initial_messages.extend(conversation_history)
    if tools_schema is not None:
        context = OpenAILLMContext(messages=initial_messages, tools=tools_schema)
    else:
        context = OpenAILLMContext(messages=initial_messages)
    context_aggregator = llm.create_context_aggregator(context)

    # Wake-word config: prefer the API-supplied list, fall back to env.
    wake_models_resolved = wake_word_models or [
        m.strip()
        for m in os.environ.get("WAKE_WORD_MODELS", "hey_jarvis").split(",")
        if m.strip()
    ]
    wake_threshold = float(os.environ.get("WAKE_WORD_THRESHOLD", "0.5"))
    wake_listen_secs = float(os.environ.get("WAKE_LISTEN_SECS", "8.0"))

    processors: list[FrameProcessor] = [
        transport.input(),
        StartupAudioGate(warmup_secs=2.0),
    ]
    if not wake_disabled:
        processors.append(
            WakeWordGate(
                models=wake_models_resolved,
                threshold=wake_threshold,
                max_listen_secs=wake_listen_secs,
                output_sample_rate=output_sample_rate,
                on_wake_fired=on_wake_fired,
                is_bot_speaking=is_bot_speaking,
                on_predict_error=on_wake_predict_error,
                continuous_conversation=continuous_conversation,
                continuous_window_secs=continuous_window_secs,
            )
        )
    if greeting_message:
        processors.append(GreetingAnnouncer(greeting_message))
    if on_user_stopped or on_bot_started or on_bot_stopped:
        processors.append(
            PipelineStateTracker(
                on_user_stopped=on_user_stopped,
                on_bot_started=on_bot_started,
                on_bot_stopped=on_bot_stopped,
            )
        )
    user_capture = STTUserTextCapture()
    processors.extend(
        [
            stt,
            # Per-turn timing + transcript capture. Both must sit
            # AFTER STT so they see TranscriptionFrame, and BEFORE
            # the user-context aggregator which consumes it.
            TurnTelemetry(),
            user_capture,
            context_aggregator.user(),
            llm,
            # Catch the LLM's streamed TextFrame chunks here, on their
            # way to TTS. Logged as a single line on bot-stopped, and
            # paired with the captured user text for persistence.
            BotResponseLogger(
                user_capture=user_capture,
                on_turn_complete=on_turn_complete,
            ),
            tts,
            transport.output(),
            context_aggregator.assistant(),
        ]
    )

    pipeline = Pipeline(processors)

    return PipelineTask(
        pipeline,
        params=PipelineParams(
            # User-toggleable. Default is False because near-field
            # mic+speaker setups (e.g. PowerConf) pick up the bot's own
            # TTS past hardware AEC and self-cancel mid-sentence. Flip
            # to True for headphone / clean-room setups.
            allow_interruptions=interrupt,
            enable_metrics=True,
        ),
    )
