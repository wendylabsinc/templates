# Frontend development

This is the production React frontend for the generated Pipecat voice assistant.
It handles browser audio, local-device handoff, settings, authentication,
conversation state, status polling, and the audio visualizer.

## Run against a backend

Start the backend, then run:

```sh
npm install
DEV_BACKEND_URL=https://localhost:{{.PORT}} npm run dev
```

Vite proxies `/api/*` and `/bot-audio` to `DEV_BACKEND_URL`. Set
`VITE_BOT_WS_URL` only when the WebSocket needs a different target. Accept the
backend's self-signed certificate in the browser before expecting microphone or
WebSocket access.

## Structure and extension

- `src/App.tsx` coordinates the UI and `/bot-audio` URL.
- `src/audio/` contains Pipecat, local-device, settings, authentication, and
  visualization hooks.
- `src/components/SettingsDrawer.tsx` edits the backend settings contract.
- `src/components/LifestreamVisualizer.tsx` renders microphone and bot audio.

When adding a setting, update the backend request/response models and this UI in
the same change. When changing audio framing, update the Pipecat serializer and
browser source together.

The similarly named directory under `common/` is a reusable visualizer
reference, not the source of truth for this template.
