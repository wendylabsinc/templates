# {{.APP_ID}}

A Pipecat voice assistant for WendyOS. It uses local wake-word detection,
faster-whisper speech recognition, and Piper speech synthesis with a cloud LLM.
The React UI shows audio activity, device state, settings, and conversation
status.

## Requirements

- A reachable WendyOS device and Wendy CLI access
- A microphone and speaker available through ALSA
- Internet access for the selected cloud LLM and optional search tools
- A Google AI Studio API key for the initial Gemini configuration
- Enough disk for the container and the persistent speech-model cache

The default transport uses the target's microphone and speaker without an open
browser. When a browser connects to `/bot-audio`, Pipecat hands the session to
the browser microphone and audio output; it returns to local audio after the
browser disconnects.

## Run and verify

```sh
wendy run
```

Open `https://<device-hostname>:{{.PORT}}`. The first start creates a self-signed
certificate in persistent storage, so the browser may require a one-time
security exception. Grant microphone permission if you use browser audio.

Select the intended audio devices. To verify local audio, say the configured
wake word and ask a short question. For browser audio, connect the browser
session and speak directly; browser sessions do not use the wake-word gate.
`GET /api/status` reports the active transport and any pipeline error;
`GET /api/audio-devices` shows the device enumeration.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `APP_ID` | required | Application and model-volume name |
| `PORT` | `3005` | HTTPS, API, and WebSocket port |
| `GOOGLE_API_KEY` | required | Initial Gemini API key; rendered into the image environment |
| `AUDIO_INPUT_DEVICE` | `default` | Local input index, name substring, or `default` |
| `AUDIO_OUTPUT_DEVICE` | `default` | Local output index, name substring, or `default` |
| `WAKE_WORD_MODELS` | `hey_jarvis` | Comma-separated bundled openWakeWord models |
| `WAKE_WORD_THRESHOLD` | `0.5` | Wake confidence from `0.0` to `1.0` |
| `WAKE_LISTEN_SECS` | `8.0` | Listening window after wake detection |
| `LOG_TRANSCRIPTS` | `false` | Include speech and response text in logs |

The UI persists provider, model, prompt, wake-word, audio, and conversation
settings under `/models/state`. Keep `LOG_TRANSCRIPTS=false` unless log content
is appropriate for the environment. Regenerate or rebuild after changing a
secret that was rendered into the Dockerfile.

## How it works

```text
┌────────────────┐ wake-word gate ┌───────────────┐
│ local ALSA mic │ ─────────────▶ │               │
└────────────────┘                │ session input │
┌────────────────┐ /bot-audio WS  │               │
│ browser mic    │ ─────────────▶ │               │
└────────────────┘                └───────┬───────┘
                                          │
                                          ▼
                                  ┌────────────────┐    ┌─────────────────┐    ┌───────┐
                                  │ faster-whisper │ ─▶ │ cloud LLM/tools │ ─▶ │ Piper │
                                  └────────────────┘    └─────────────────┘    └───┬───┘
                                                                                   │
                                  ┌───────────────────────────┐
                                  │ matching session output   │ ◀───────────────────┘
                                  │ ALSA speaker / browser WS │
                                  └───────────────────────────┘
```

- `main.py` serves the UI and API, manages local/browser transport handoff,
  audio device selection, settings, TLS, and session lifecycle.
- `pipeline.py` builds the Pipecat STT, LLM, tools, TTS, wake-word, and
  conversation pipeline.
- `entrypoint.sh` configures ALSA, seeds `/models`, and creates TLS files.
- `frontend/` is the production React source for this template.
- `wendy.json` grants host networking, audio access, and persistent `/models`
  storage. It does not run a bundled Ollama service.

Write routes can optionally be protected with a runtime bearer token:

```sh
wendy run --env WENDY_AUTH_TOKEN=<secret>
```

## Extend it

- Change STT, LLM, tools, or TTS construction in `pipeline.py`.
- Add settings through the models and handlers in `main.py` and the settings UI
  in `frontend/src/components/SettingsDrawer.tsx`.
- Add another audio transport by following the local and browser session paths
  in `main.py`.
- Run `npm install` and `DEV_BACKEND_URL=https://localhost:{{.PORT}} npm run dev`
  in `frontend/` for standalone UI development.

## Operations and troubleshooting

```sh
wendy device logs {{.APP_ID}} --tail 200
wendy device apps stop {{.APP_ID}}
```

- If the UI cannot use its microphone, open the HTTPS URL, accept the
  certificate, and grant browser permission.
- If local audio fails, use `/api/audio-devices` or the UI picker and check the
  PortAudio enumeration near startup.
- If the LLM rejects a request, check the selected provider, model, and API key.
- If an audio device is removed while active, reconnect it and restart the app
  if the pipeline does not recover.
