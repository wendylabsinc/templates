# {{.APP_ID}}

Swift entrypoint for the voice-ai-pipecat template. Mirrors the Python
template, but starts as a Swift executable that loads Pipecat via
[PythonKit](https://github.com/pvieito/PythonKit).

```
browser mic --WS--> FastAPI --> faster-whisper (STT) --> Gemini 2.5 Flash --> Piper (TTS) --WS--> browser
                       |
                  Swift (PythonKit) owns process startup
```

Swift handles process lifecycle; Pipecat owns the audio pipeline. Behavior is
identical to `python/voice-ai-pipecat/` — see that template's README for
frontend, entitlements, first-run, and hot-plug notes.

## Requirements

- A [Google AI Studio](https://aistudio.google.com/) API key for Gemini.
- USB audio device (Anker PowerConf or similar).

## Deploy

```bash
wendy run .
```

## Running the frontend standalone

```bash
cd frontend
npm install
VITE_BOT_WS_URL=ws://localhost:{{.PORT}}/bot-audio npm run dev
```

The canonical frontend lives at `common/voice-ai-pipecat-frontend/` upstream;
this `frontend/` directory is a vendored copy. Keep them in sync when
editing.

## Known limitations

Same as the Python template:

- USB hot-plug requires a `wendy run` restart until the `usb-hotplug`
  entitlement lands in wendy-agent.
- First boot downloads ~500 MB of model weights (`faster-whisper` tiny +
  Piper voice) into `/models`.
