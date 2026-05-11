# voice-ai-pipecat buffering / thinking investigation

Date: 2026-05-11

Scope:
- Local checkout: `main` at `71ad731` (merge of PR #30).
- GitHub context: PR #30 (`fix(voice-ai-pipecat): kill duplicate-answer with Gemini AFC + diagnostic logging`) and PR #29 (`trim(voice-ai-pipecat): switch to lightweight cloud-LLM build`).
- Linear context: WDY-1165 (`Fix Pipe Cat voice-assistant buffering on CPU-only stack`).
- This was a code and PR review only. I did not reproduce on device hardware in this pass.

## Summary

PR #30 added useful turn-boundary diagnostics and one suppression mechanism for LLM text emitted in the same response cycle as a tool call. That change helps one duplicate-answer path, but it does not fully address the CPU-only startup buffering report from WDY-1165.

The main remaining risks are:

1. The new tool-call text gate does not cover the default Google native `google_search` path used by this template.
2. The same gate is order-racy for registered function tools because the current code decides whether to release buffered text at `LLMFullResponseEndFrame`, while Pipecat can emit function-call progress frames from background tasks.
3. The frontend can remain stuck in `Thinking...` because backend `processing` is set on `UserStoppedSpeakingFrame` and only cleared by `BotStartedSpeakingFrame`.
4. Startup/greeting input suppression is still a fixed 2 second audio-only timer. It is not tied to output readiness, greeting completion, or a complete drop of VAD/transcription frames, which matches the shape of WDY-1165.

## Sources inspected

- PR #30: https://github.com/wendylabsinc/templates/pull/30
- PR #29: https://github.com/wendylabsinc/templates/pull/29
- WDY-1165: https://linear.app/wendylabsinc/issue/WDY-1165/fix-pipe-cat-voice-assistant-buffering-on-cpu-only-stack
- Pipecat control-frame docs: https://docs.pipecat.ai/api-reference/server/frames/control-frames
- Pipecat system-frame docs: https://docs.pipecat.ai/server/frames/system-frames
- Pipecat LLM service source reference: https://reference-server.pipecat.ai/en/latest/_modules/pipecat/services/llm_service.html
- Pipecat Google LLM service source reference: https://reference-server.pipecat.ai/en/latest/_modules/pipecat/services/google/llm.html

## Finding 1: PR #30's text gate does not cover default Gemini native search

Severity: High

The default template path is Google Gemini with native `google_search` enabled:

- `DEFAULT_LLM_PROVIDER = "google"` and `DEFAULT_GOOGLE_SEARCH_ENABLED = True` in `main.py`.
- `_build_llm_service()` returns `GoogleLLMService(**kwargs), None, None` when Google native search is enabled (`pipeline.py:507-516`).

That means the default search path has no registered function handlers and does not rely on the `FunctionCallInProgressFrame` signal that PR #30's suppression logic watches.

The new suppression logic only drops buffered text when `_function_call_in_round` is set by a `FunctionCallInProgressFrame` (`pipeline.py:821-824`, `pipeline.py:862-878`). If no such frame arrives, all buffered text is released to TTS at `LLMFullResponseEndFrame` (`pipeline.py:880-916`).

This also aligns with PR #30's own verification log for a weather query, which ended with `function_call=False`. That is the same class of query WDY-1165 describes, but the new hard gate was not active for it.

Practical impact:
- If Gemini native search emits any pre-grounding or chitchat text in `part.text`, this code still speaks it because native `google_search` is not represented as a registered Pipecat function call in this pipeline.
- The prompt change in PR #30 may reduce the behavior, but the code-level suppression is not covering the default search path.

Recommended next step:
- Decide whether default Google search should stay on native `google_search` or move to an explicit function-tool path that the app can gate deterministically.
- If native search stays, inspect whether `GoogleLLMService` exposes a reliable search/grounding boundary frame for this Pipecat version. A `FunctionCallInProgressFrame`-only gate is not enough for the current default.

## Finding 2: The function-call gate is order-racy for registered tools

Severity: Medium

Even for providers that do use registered function tools, the current suppression gate can miss tool calls depending on frame ordering.

Current behavior:
- `BotResponseLogger` buffers LLM text between `LLMFullResponseStartFrame` and `LLMFullResponseEndFrame`.
- It releases or drops buffered text when `LLMFullResponseEndFrame` arrives.
- It only drops when a `FunctionCallInProgressFrame` has already passed through the logger.

Pipecat's LLM service can schedule function runners as background tasks. In the current reference source, `run_function_calls()` calls `_run_parallel_function_calls()`, which creates tasks for `_run_function_call()`; the `FunctionCallInProgressFrame` is broadcast inside `_run_function_call()`. The Google service calls `run_function_calls(function_calls)` and then pushes `LLMFullResponseEndFrame` in `finally`.

That creates a possible sequence:

1. `LLMFullResponseStartFrame`
2. one or more text frames
3. `run_function_calls()` schedules function runner tasks
4. `LLMFullResponseEndFrame` reaches `BotResponseLogger`
5. `BotResponseLogger` sees `_function_call_in_round == False` and releases text to TTS
6. function runner task starts and broadcasts `FunctionCallInProgressFrame`

Practical impact:
- For OpenAI/Anthropic/Groq/Brave tool paths, a preamble can still leak if `LLMFullResponseEndFrame` wins the race.

Recommended next step:
- Gate on `FunctionCallsStartedFrame` as well as `FunctionCallInProgressFrame`, because Pipecat broadcasts `FunctionCallsStartedFrame` before scheduling runner tasks.
- Alternatively, use the LLM service's function-call detection before the end boundary, or hold the LLM response until either a no-tool decision is explicit or the response is known not to contain tool calls.

## Finding 3: `Thinking...` has no recovery path when no bot speech starts

Severity: High

The backend status flag is set here:

- `PipelineStateTracker` calls `on_user_stopped()` for every `UserStoppedSpeakingFrame` it sees (`pipeline.py:643-660`).
- `SessionManager.on_user_stopped()` sets `_processing = True` (`main.py:1350-1352`).

It is cleared here:

- `SessionManager.on_bot_started()` clears `_processing` (`main.py:1354-1360`).

There is no timeout, no clear on empty/no transcription, and no clear when the LLM/tool path ends without TTS. The PR #30 watchdog does not recover the app state either:

- It only arms after a non-empty `TranscriptionFrame` (`pipeline.py:727-741`).
- It only logs when no LLM start is observed (`pipeline.py:700-721`).
- It does not clear `_processing`, reset the turn, reopen the wake/follow-up path, or surface a recoverable error.

The frontend directly maps `processing` to `Thinking...` for local mode and browser mode (`frontend/src/App.tsx:116-125`). So any forwarded `UserStoppedSpeakingFrame` that does not eventually produce `BotStartedSpeakingFrame` can leave the UI in `Thinking...` indefinitely.

Practical impact:
- Empty or low-confidence Whisper results can stick the UI in `Thinking...`.
- A dropped transcript, LLM error, search/tool failure that produces no TTS, or a suppressed tool preamble without a follow-up answer can also stick it.
- In continuous-conversation mode, follow-up listening depends on the bot-speaking to bot-quiet transition. If no bot speech starts/stops, follow-up mode will not reopen for a no-wake follow-up.

Recommended next step:
- Track processing by turn, and clear it on explicit terminal states: empty transcription, LLM error, LLM end with no TTS, tool timeout/failure, and a bounded wall-clock timeout.
- Make the STT stall watchdog call a backend recovery callback, not only log.
- Consider setting `processing` after a non-empty `TranscriptionFrame` reaches the user aggregator rather than on raw `UserStoppedSpeakingFrame`.

## Finding 4: Startup/greeting input suppression is still time-based and audio-only

Severity: Medium

WDY-1165 says the CPU-only image buffers user speech during the first few seconds after the greeting. The current startup guard is still a fixed timer:

- `GreetingAnnouncer` sends the greeting 1.5 seconds after `StartFrame` (`pipeline.py:1047-1066`).
- `StartupAudioGate` drops only `InputAudioRawFrame` for 2.0 seconds after `StartFrame` (`pipeline.py:1396-1423`).
- The local transport starts audio input as soon as the local pipeline starts (`main.py:1463-1472`).
- Default `allow_interruptions` is false (`main.py:421-423`, `pipeline.py:1864-1872`).

Pipecat's frame model queues data/control frames in order, and its docs describe TTS pausing while synthesizing a `TTSSpeakFrame`; text arriving during that synthesis is queued until TTS resumes. That is consistent with users hearing queued responses after the greeting or after a slow CPU path catches up.

The current gate also only drops raw audio frames. It does not drop `UserStartedSpeakingFrame`, `UserStoppedSpeakingFrame`, or `TranscriptionFrame` in configurations where those frames can reach it without the wake-word gate. In local wake-word mode, `WakeWordGate` tries to suppress wake detection while the bot is speaking (`pipeline.py:1255`, `pipeline.py:1380-1390`), but the startup guard itself is still not tied to greeting completion or output readiness.

Practical impact:
- The initial 2 second gate can open while the greeting is still speaking.
- A CPU-only first run with slower STT/TTS/model warmup has a larger window for user audio and turn-boundary frames to queue.
- This path was not changed by PR #30, so the specific WDY-1165 startup-buffering report is still likely unresolved.

Recommended next step:
- Make startup input suppression state-based instead of timer-only: keep input muted until output is ready, the greeting has completed, and the first post-greeting listen window is deliberately opened.
- Drop the full turn surface during startup/greeting, not just raw audio: `InputAudioRawFrame`, user speaking frames, interim/final transcription frames, and interruptions.
- Consider starting the local input stream after the greeting, or using Pipecat's user-mute strategy around bot speech instead of a custom audio-only warmup gate.

## Finding 5: PR #30 added a watchdog task without teardown cleanup

Severity: Low

`STTUserTextCapture` now creates `_watchdog_task` per finalized transcript (`pipeline.py:735-741`), but the class does not implement `cleanup()`. Copilot called this out on PR #30.

Practical impact:
- Pipeline restart or teardown inside the watchdog window can leave a pending task that wakes after the processor graph has been torn down.
- This is probably not the cause of the buffering/thinking symptoms, but it is a small lifecycle regression in the recent change.

Recommended next step:
- Add a `cleanup()` override matching `GreetingAnnouncer.cleanup()` that cancels and awaits `_watchdog_task`, then clears it.

## Suggested verification plan

On a CPU-only device with `LOG_TRANSCRIPTS=true`, capture logs for three scenarios:

1. Fresh start, speak immediately during/after the greeting.
2. Ask a current-data question that uses default Google native search.
3. Trigger the stuck `Thinking...` state, then wait at least 10 seconds without restarting.

For each run, inspect the exact sequence of:

- `WakeWordGate`
- `STT`
- `LLM start/end`
- `LLM preamble suppressed`
- `TTS`
- `BOT speaking/done`
- `TURN overlap`
- `STT stalled`

Interpretation:

- `TTS` lines queued before `BOT done` for the greeting indicate downstream TTS queueing.
- `function_call=False` on search-backed Gemini turns means PR #30's hard suppression gate was not active for native search.
- `processing=true` in `/api/status` with no recent `BOT speaking` confirms the stuck-state path in Finding 3.
- `UserStoppedSpeakingFrame` without a following non-empty `STT` line points at the VAD/no-transcript stuck path.

## Resolution

Branch `ed/voice-ai-pipecat-buffering-fixes` lands the following:

- **F5** — `STTUserTextCapture.cleanup()` cancels its watchdog task on teardown so the leaked task no longer wakes into a destroyed transport.
- **F2** — `BotResponseLogger` now snapshots per-round buffers at `LLMFullResponseEndFrame` and waits `LLM_END_GRACE_MS` (default 200 ms) before deciding to release or drop, so a `FunctionCallInProgressFrame` arriving from a background runner task after the end frame still wins the gate. The logger also gates on `FunctionCallsStartedFrame` if the installed Pipecat exposes it. A `cleanup()` override cancels the grace task on teardown. The next round's `LLMFullResponseStartFrame` cancels any pending grace task, and the cancel handler flushes the old round's frames downstream before the new round begins so frame ordering is preserved.
- **F3** — `SessionManager` gains three layered recovery paths for the `_processing` flag: a wall-clock watchdog (`PROCESSING_TIMEOUT_SECS`, default 20 s) armed on `on_user_stopped()` and cancelled on `on_bot_started()`; `on_stt_stalled()` wired to `STTUserTextCapture._watchdog`; and `on_empty_llm_round()` wired to `BotResponseLogger` for the LLM-ended-with-no-text case.
- **F4** — `StartupAudioGate` is now a state machine. The gate stays closed until both the warmup floor (2 s) has elapsed and the first `BotStoppedSpeakingFrame` (greeting completion) has been observed. A ceiling (10 s) opens the gate unconditionally so a failed greeting can't permanently mute input. The closed-state surface now drops `InputAudioRawFrame`, `UserStartedSpeakingFrame`, `UserStoppedSpeakingFrame`, and `InterruptionFrame` (when exported by the installed Pipecat).
- **F1** — partial. A code comment at the native-search return site in `_build_llm_service` documents the gap. A `GroundingDetectedFrame` sentinel and `BotResponseLogger` gate for it are in place, opt-in via `GOOGLE_GROUNDING_GATE=true`. The producer (a `GoogleLLMService` wrapper that inspects `grounding_metadata`) is deferred — implementing it cleanly requires reading Pipecat's `services/google/llm.py` directly, which is best done in a follow-up so the wrapper can be verified against the actual pipecat version in the running container.
