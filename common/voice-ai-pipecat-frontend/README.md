# Pipecat audio visualizer reference

This directory is a reusable React audio-visualization reference. It contains
microphone, WebSocket PCM, WebRTC, and generic `MediaStream` hooks that feed a
shared `AnalyserNode` visualizer.

```text
┌───────────────────────────────────────────┐
│ mic / WebSocket / WebRTC / generic stream │
└─────────────────────┬─────────────────────┘
                      │
                      ▼
             ┌──────────────────┐    ┌──────────────┐    ┌──────────────────────┐
             │ AudioSource hook │ ─▶ │ AnalyserNode │ ─▶ │ LifestreamVisualizer │
             └──────────────────┘    └──────────────┘    └──────────────────────┘
```

It is not the source of truth for the selectable `voice-ai-pipecat` template.
That production frontend is `python/voice-ai-pipecat/frontend/` and includes
settings, local-device handoff, authentication, status, and conversation logic
that is not present here.

## Run locally

```sh
npm install
npm run dev
```

The default app visualizes the browser microphone and a PCM16 WebSocket source.
Override the WebSocket during development with:

```sh
VITE_BOT_WS_URL=ws://localhost:3005/bot-audio npm run dev
```

## Extend it

Audio hooks live in `src/audio/`. Each returns an `AudioSource` containing an
`AnalyserNode`, status, and error. Add source types there and select them in
`src/App.tsx`. The default WebSocket decoder expects little-endian PCM16, mono,
at 24 kHz; change its decoder and sample settings together for another format.

Port useful changes to the production template deliberately. Do not copy this
tree over the production frontend.
