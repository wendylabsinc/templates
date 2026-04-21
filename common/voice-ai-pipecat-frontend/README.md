# voice-ai-pipecat-frontend

Real-time audio visualizer for the `voice-ai-pipecat` Wendy template. Forked from the
`voice-ai-frontend` scratchpad. This directory is the **source of truth** — the same
tree is vendored into `python/voice-ai-pipecat/frontend/` and
`swift/voice-ai-pipecat/frontend/` at template-ship time. If you edit code here,
re-copy into those template directories (see the template READMEs).

The visualizer reads frequency data from two Web Audio `AnalyserNode`s:

- **blue** lines react to the user's microphone (`useMicrophoneSource`)
- **emerald** lines react to the bot's TTS audio coming back over a WebSocket
  (`useWebSocketSource`, configured in `src/App.tsx`)

Everything else — microphone selection, WebSocket URL, error surfacing — is wiring
around those two `AnalyserNode`s.

## Connecting to a backend

`src/App.tsx` derives the bot WebSocket URL from the current page origin
(`ws://<host>/bot-audio`). When developing the frontend standalone against a
running Pipecat backend, override it:

```bash
VITE_BOT_WS_URL=ws://localhost:3005/bot-audio npm run dev
```

## Quick start

```bash
npm install
npm run dev
```

Open the printed URL, grant mic permission, speak.

## How audio flows

```
  ┌─────────────────────────┐
  │  Audio source hook      │        ┌───────────────────────┐
  │  (mic / ws / webrtc)    │──────▶│ AnalyserNode          │──▶ LifestreamVisualizer
  │  returns AudioSource    │        │ (frequencyBinCount)   │
  └─────────────────────────┘        └───────────────────────┘
```

Every source hook returns the same shape:

```ts
interface AudioSource {
  analyser: AnalyserNode | null           // plug into LifestreamVisualizer
  status: "idle" | "connecting" | "active" | "error"
  error: Error | null
}
```

Because the visualizer only cares about the `AnalyserNode`, swapping audio sources is
one line in `src/App.tsx`.

Available hooks in `src/audio`:

| Hook | Input | Use when |
| --- | --- | --- |
| `useMicrophoneSource(deviceId)` | device id from `MicrophoneSelector` | local microphone |
| `useWebRtcSource(remoteStream)` | remote `MediaStream` from your `RTCPeerConnection` | WebRTC peer audio |
| `useWebSocketSource({ url, … })` | WebSocket URL + decoder | streaming PCM over WS |
| `useMediaStreamAnalyser(stream)` | any `MediaStream` with an audio track | roll your own |

## Using the microphone (default)

`src/App.tsx` already wires this up:

```tsx
import { useMicrophoneSource } from "./audio"

const { analyser } = useMicrophoneSource(selectedDeviceId)
return <LifestreamVisualizer analyser={analyser} />
```

## Switching to a WebSocket audio feed

The WebSocket source expects binary PCM frames by default
(little-endian Int16, mono, 24 kHz). Point it at your server and it will decode,
schedule gapless playback, and produce the `AnalyserNode` for the visualizer.

```tsx
import { LifestreamVisualizer } from "./components/LifestreamVisualizer"
import { useWebSocketSource } from "./audio"

function App() {
  const { analyser, status, error } = useWebSocketSource({
    url: "wss://your-server.example.com/audio",
    sampleRate: 24000,   // must match what the server sends
    channels: 1,
    // playback: false,  // set to false to visualize only, no speakers
  })

  return (
    <>
      <LifestreamVisualizer analyser={analyser} />
      {error && <p>error: {error.message}</p>}
      {status === "connecting" && <p>connecting…</p>}
    </>
  )
}
```

### Custom message format

If your server sends something other than raw PCM16 (Opus packets, base64-wrapped
JSON, Ogg, etc.), pass a `decode` function. It receives the raw
`MessageEvent["data"]` and returns interleaved Float32 samples in `[-1, 1]`:

```tsx
const { analyser } = useWebSocketSource({
  url,
  sampleRate: 48000,
  channels: 2,
  decode: (data) => {
    // example: server sends JSON { audio: base64 float32 }
    if (typeof data !== "string") return null
    const { audio } = JSON.parse(data) as { audio: string }
    const bytes = Uint8Array.from(atob(audio), (c) => c.charCodeAt(0))
    return new Float32Array(bytes.buffer)
  },
})
```

`decode` may also be `async`, so you can call `AudioContext.decodeAudioData` for
container formats (WebM/Opus, Ogg, MP3) if your server produces them.

## Switching to a WebRTC audio feed

WebRTC signaling lives in your app — once you get a remote `MediaStream` from
`RTCPeerConnection.ontrack`, hand it to `useWebRtcSource`:

```tsx
import * as React from "react"
import { LifestreamVisualizer } from "./components/LifestreamVisualizer"
import { useWebRtcSource } from "./audio"

function App() {
  const [remoteStream, setRemoteStream] = React.useState<MediaStream | null>(null)

  React.useEffect(() => {
    const pc = new RTCPeerConnection({
      iceServers: [{ urls: "stun:stun.l.google.com:19302" }],
    })
    pc.addTransceiver("audio", { direction: "recvonly" })
    pc.ontrack = (e) => setRemoteStream(e.streams[0])

    ;(async () => {
      const offer = await pc.createOffer()
      await pc.setLocalDescription(offer)
      // ─── send offer.sdp to your signaling server, get answer back ───
      const answer = await negotiateWithYourServer(offer)
      await pc.setRemoteDescription(answer)
    })()

    return () => pc.close()
  }, [])

  const { analyser } = useWebRtcSource(remoteStream)
  return <LifestreamVisualizer analyser={analyser} />
}
```

If your remote audio also needs to be heard, attach the stream to a hidden
`<audio autoPlay />` element — the `AnalyserNode` in this hook only taps the
signal for visualization, it does not route to speakers. (The WebSocket source
is different: it owns playback because it decodes PCM itself.)

## Writing a custom source

Anything that can produce an `AnalyserNode` works. The lowest-level primitive
is `useMediaStreamAnalyser`, which takes any `MediaStream`:

```tsx
import { useMediaStreamAnalyser } from "./audio"

// e.g. screen-share audio, file-backed MediaStream, etc.
const { analyser } = useMediaStreamAnalyser(someStream)
```

For sources that aren't a `MediaStream` (raw bytes, generated tones, decoded
files), build your own hook: create an `AudioContext`, an `AnalyserNode`, and
a source node (e.g. `AudioBufferSourceNode`), connect them, and return the
`AnalyserNode` as state. `useWebSocketSource` is a working reference.

## Project structure

```
src/
  App.tsx                          # picks the audio source
  audio/
    index.ts                       # re-exports
    types.ts                       # AudioSource, AudioSourceStatus
    useMediaStreamAnalyser.ts      # MediaStream → AnalyserNode
    useMicrophoneSource.ts         # deviceId → AnalyserNode
    useWebRtcSource.ts             # remote MediaStream → AnalyserNode
    useWebSocketSource.ts          # WS URL + PCM decoder → AnalyserNode
  components/
    LifestreamVisualizer.tsx       # takes { analyser }, renders Three.js
    MicrophoneSelector.tsx         # device picker UI
    ui/                            # shadcn/ui
```

## Adding shadcn components

```bash
npx shadcn@latest add button
```
