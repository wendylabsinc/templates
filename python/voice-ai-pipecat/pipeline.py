"""Pipecat pipeline: faster-whisper STT -> Gemini 2.5 Flash -> Piper TTS.

Gemini is used with its native Google Search grounding tool, so the assistant
can answer real-world questions ("what's the weather in San Francisco?")
without a separate search API.
"""

from __future__ import annotations

import os
from pathlib import Path

from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
from pipecat.services.google.llm import GoogleLLMService
from pipecat.services.piper.tts import PiperTTSService
from pipecat.services.whisper.stt import WhisperSTTService, Model
from pipecat.transports.base_transport import BaseTransport


SYSTEM_PROMPT = (
    "You are a helpful voice assistant running on a Wendy device. "
    "Keep replies short (one or two sentences) and speakable. "
    "Use Google Search when the user asks for fresh or real-world information."
)


def build_pipeline_task(transport: BaseTransport) -> PipelineTask:
    """Build the Pipecat pipeline task wired around `transport`."""

    stt = WhisperSTTService(
        settings=WhisperSTTService.Settings(model=Model.TINY.value),
    )

    # Native Google Search grounding: Gemini decides when to search.
    # If the Pipecat version pinned here expects a different shape, see
    # https://docs.pipecat.ai/server/services/llm/google for the current API.
    tools = [{"google_search": {}}]

    llm = GoogleLLMService(
        api_key=os.environ["GOOGLE_API_KEY"],
        settings=GoogleLLMService.Settings(model="gemini-2.5-flash"),
        tools=tools,
    )

    tts = PiperTTSService(
        settings=PiperTTSService.Settings(voice="en_US-lessac-medium"),
        download_dir=Path("/app/models/piper"),
        sample_rate=16000,
    )

    context = OpenAILLMContext(
        messages=[{"role": "system", "content": SYSTEM_PROMPT}],
        tools=tools,
    )
    context_aggregator = llm.create_context_aggregator(context)

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            context_aggregator.user(),
            llm,
            tts,
            transport.output(),
            context_aggregator.assistant(),
        ]
    )

    return PipelineTask(
        pipeline,
        params=PipelineParams(
            # Bot finishes its reply before listening again. Without this,
            # near-field mic+speaker setups (e.g. PowerConf on a desk) pick
            # up the bot's own TTS through residual leakage past hardware
            # AEC, trigger VAD's "User started speaking", and self-cancel
            # mid-sentence — so the user never hears a complete reply.
            allow_interruptions=False,
            enable_metrics=True,
        ),
    )
