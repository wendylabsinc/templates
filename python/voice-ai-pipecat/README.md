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

The `postStart` hook opens the visualizer at `https://${WENDY_HOSTNAME}:{{.PORT}}`.

### TLS and the browser warning

The visualizer uses `navigator.mediaDevices.getUserMedia` to capture mic
audio. Browsers gate that API behind a secure origin, so the server has
to be reached over HTTPS (or `localhost`). On first boot the entrypoint
generates a self-signed cert at `/models/tls/{cert,key}.pem` (persisted
across container restarts) and uvicorn serves HTTPS on `{{.PORT}}`.

Because the cert is self-signed, the **first time you open the page on a
machine you'll see "Not secure" / "Your connection is not private"**. Click
through (Chrome: *Advanced → Proceed*) once per browser; the exception is
remembered and the mic API will work on subsequent visits.

For zero browser warnings (and reliable Safari support, which is strict
about self-signed certs), generate a trusted cert with
[`mkcert`](https://github.com/FiloSottile/mkcert) on your dev machine and
push it onto the device:

```bash
brew install mkcert
mkcert -install
mkcert ${WENDY_HOSTNAME} localhost 127.0.0.1
# copy the resulting *.pem files into /models/tls/cert.pem and /models/tls/key.pem
# (e.g. via `wendy device file push`), then restart the app.
```

The entrypoint detects existing cert files and skips the self-signed
regen automatically.

## Running the frontend standalone

If you want to iterate on the UI against a running Pipecat backend, start
the backend first (e.g. `wendy run .` on a device, or `python main.py`
locally), then point Vite's dev proxy at it:

```bash
cd frontend
npm ci
DEV_BACKEND_URL=https://localhost:{{.PORT}} npm run dev
```

`vite.config.ts` proxies `/api/*` and `/bot-audio` to `DEV_BACKEND_URL`,
so the standalone frontend uses the same relative paths that the
production build does — no per-call origin overrides needed. If you want
to override just the WebSocket (e.g. point at a different backend host),
`VITE_BOT_WS_URL` still wins over the page origin.

The canonical source for the frontend lives at
`common/voice-ai-pipecat-frontend/` in the `wendylabsinc/templates` repo. The
`frontend/` directory here is a vendored copy — if you change code upstream,
re-copy it into this directory before shipping.

## Entitlements

| Entitlement | Why |
| --- | --- |
| `network` (host) | outbound to cloud LLM/STT APIs, plus serving the frontend |
| `audio` | ALSA mic + speaker access for the PowerConf |
| `persist` (`/models`) | caches the seeded Piper voice + Whisper TINY across restarts, and persists TLS cert + saved settings |

## First-run note

On first boot the container seeds the **faster-whisper** tiny model (~75 MB)
and the default **Piper** voice (`en_US-lessac-medium`, ~63 MB) from the image
into `/models/` (a persistent volume), so subsequent starts are instant. The
seed copy happens automatically in `entrypoint.sh`.

This is the cloud-LLM build — pick a provider (Google / OpenAI / Anthropic /
Groq) in the settings drawer. The "ollama" option remains in the dropdown for
backward compatibility but the daemon isn't bundled in this image; selecting
it surfaces a `Local LLM disabled in this build` banner via `/api/status`.
For an offline LLM build see the WIP `voice-ai-pipecat-jetson` template.

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
- **Base image pull returns `401 Unauthorized`** — the template defaults to the
  public `dustynv/tensorrt:8.6-r36.2.0` Jetson image so normal builds do not
  require an NGC login. Override `JETSON_BASE_IMAGE` only if you manage registry
  credentials for your builder.
- **Jetson CUDA not detected** — check the CTranslate2 build log in
  `Dockerfile`. The template targets JetPack 6.0's CUDA 12.2 + cuDNN 8.9
  stack with CTranslate2 4.4.0.
