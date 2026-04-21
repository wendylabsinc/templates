# {{.APP_ID}}

Always-on voice assistant built on [Pipecat](https://github.com/pipecat-ai/pipecat):

```
browser mic --WS--> FastAPI --> faster-whisper (STT) --> Gemini 2.5 Flash --> Piper (TTS) --WS--> browser
                                                          + Google Search grounding
```

The React visualizer renders two reactive line groups: **blue** for your microphone
and **emerald** for the bot's TTS.

## Requirements

- A [Google AI Studio](https://aistudio.google.com/) API key for Gemini.
- USB audio device (Anker PowerConf or similar) if running on a Wendy device.

## Deploy

```bash
wendy run .
```

The `postStart` hook opens the visualizer at `http://${WENDY_HOSTNAME}:{{.PORT}}`.

## Running the frontend standalone

If you want to iterate on the UI against a local Pipecat backend:

```bash
cd frontend
npm install
VITE_BOT_WS_URL=ws://localhost:{{.PORT}}/bot-audio npm run dev
```

The canonical source for the frontend lives at
`common/voice-ai-pipecat-frontend/` in the `wendylabsinc/templates` repo. The
`frontend/` directory here is a vendored copy — if you change code upstream,
re-copy it into this directory before shipping.

## Entitlements

| Entitlement | Why |
| --- | --- |
| `network` (host) | outbound to Gemini API, plus serving the frontend |
| `audio` | ALSA mic + speaker access for the PowerConf |
| `gpu` | faster-whisper CUDA acceleration on Jetson |
| `persist` (`/models`) | caches faster-whisper (~500 MB) and Piper voices across restarts |

## First-run note

The first time the container starts, faster-whisper downloads its tiny model
(~500 MB) and Piper downloads the selected voice. Both land in `/models/`,
which is backed by a persistent volume, so subsequent starts are instant.
Expect the readiness probe to take up to a minute on the first boot.

## Picking a model

`pipeline.py` uses `WhisperSTTService(model=Model.TINY)` and Piper's
`en_US-lessac-medium` voice by default. Swap those lines to upgrade:

- Whisper: `Model.BASE`, `Model.SMALL`, etc. (check VRAM on Jetson.)
- Piper: any voice from [rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices).

## Known limitation: USB hot-plug

ALSA binds to the PowerConf at container start. If you unplug the device
mid-conversation, the visualizer shows an error via the `Alert` overlay but
reconnecting currently requires a `wendy run` restart. A follow-up
`usb-hotplug` entitlement in wendy-agent is on the roadmap to enable live
re-detection.

## Troubleshooting

- **No audio in the visualizer** — check the microphone selector (upper right);
  on first load the browser may block mic access. Reload after granting
  permission.
- **Gemini returns an auth error** — confirm `GOOGLE_API_KEY` is set in the
  container (baked in from the template variable). Rotate by rebuilding.
- **Jetson CUDA not detected** — swap the base image in `Dockerfile` to
  `nvcr.io/nvidia/l4t-pytorch:r36.2.0-pth2.2-py3` (or match your JetPack).
