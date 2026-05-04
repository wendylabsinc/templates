# {{.APP_ID}}

Swift entrypoint for the voice-ai-pipecat template. This variant starts as a
Swift executable that loads the Pipecat backend via
[PythonKit](https://github.com/pvieito/PythonKit).

```
browser mic --WS--> FastAPI --> faster-whisper (STT) --> Gemini 2.5 Flash --> Piper (TTS) --WS--> browser
                       |
                  Swift (PythonKit) owns process startup
```

Swift handles process lifecycle; Pipecat owns the audio pipeline. This template
uses the standard Swift runtime image and is CPU-only for faster-whisper. Use
`python/voice-ai-pipecat/` when you need the Jetson CUDA/cuDNN build and
on-device Ollama runtime.

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

- USB hot-plug requires a `wendy run` restart until the `usb-hotplug`
  entitlement lands in wendy-agent.
- First boot downloads ~500 MB of model weights (`faster-whisper` tiny +
  Piper voice) into `/models`.
- CPU-only Whisper is slower than the Python template's Jetson CUDA path.
