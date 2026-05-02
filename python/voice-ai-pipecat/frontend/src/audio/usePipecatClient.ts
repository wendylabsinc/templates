import * as React from "react"
import { PipecatClient } from "@pipecat-ai/client-js"
import { WebSocketTransport } from "@pipecat-ai/websocket-transport"
import type { AudioSourceStatus } from "./types"

export interface PipecatClientOptions {
  /** ws(s):// URL for the Pipecat server's audio WebSocket. */
  url: string | null
  /** Browser audio input device id from MicrophoneSelector. */
  inputDeviceId: string | null
  /** When true, mic is disabled on the client but the connection stays open. */
  muted?: boolean
  /** Analyser FFT size applied to both mic and bot analysers. Default 256. */
  fftSize?: number
}

export interface PipecatClientState {
  micAnalyser: AnalyserNode | null
  botAnalyser: AnalyserNode | null
  /** True while the server reports the bot is producing TTS audio.
   *  Used to drive the bot visualizer when the WebSocket transport
   *  doesn't expose bot audio as a MediaStreamTrack (so botAnalyser
   *  stays null). */
  botSpeaking: boolean
  /** Most recent finalized user STT transcript. */
  userTranscript: string | null
  /** Most recent bot reply text (assembled from TTS chunks). */
  botTranscript: string | null
  status: AudioSourceStatus
  error: Error | null
}

/**
 * Drives a Pipecat WebSocket session: captures mic via the Pipecat client,
 * receives bot TTS audio, and exposes AnalyserNodes for visualization.
 */
export function usePipecatClient(options: PipecatClientOptions): PipecatClientState {
  const { url, inputDeviceId, muted = false, fftSize = 256 } = options

  const [micAnalyser, setMicAnalyser] = React.useState<AnalyserNode | null>(null)
  const [botAnalyser, setBotAnalyser] = React.useState<AnalyserNode | null>(null)
  const [botSpeaking, setBotSpeaking] = React.useState(false)
  const [userTranscript, setUserTranscript] = React.useState<string | null>(null)
  const [botTranscript, setBotTranscript] = React.useState<string | null>(null)
  const [status, setStatus] = React.useState<AudioSourceStatus>("idle")
  const [error, setError] = React.useState<Error | null>(null)

  const clientRef = React.useRef<PipecatClient | null>(null)
  const audioContextRef = React.useRef<AudioContext | null>(null)

  React.useEffect(() => {
    if (!url) {
      setStatus("idle")
      return
    }

    let disposed = false
    setStatus("connecting")
    setError(null)

    const audioContext = new (window.AudioContext ||
      (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext)()
    audioContextRef.current = audioContext

    const buildAnalyser = (track: MediaStreamTrack): AnalyserNode => {
      const src = audioContext.createMediaStreamSource(new MediaStream([track]))
      const analyser = audioContext.createAnalyser()
      analyser.fftSize = fftSize
      src.connect(analyser)
      return analyser
    }

    const transport = new WebSocketTransport({
      recorderSampleRate: 16_000,
      playerSampleRate: 16_000,
    })

    const client = new PipecatClient({
      transport,
      enableMic: true,
      enableCam: false,
      callbacks: {
        onConnected: () => {
          if (disposed) return
          setStatus("active")
        },
        onDisconnected: () => {
          if (disposed) return
          setStatus("idle")
        },
        onTrackStarted: (track, participant) => {
          if (disposed || track.kind !== "audio") return
          if (participant?.local) setMicAnalyser(buildAnalyser(track))
          else setBotAnalyser(buildAnalyser(track))
        },
        onTrackStopped: (track, participant) => {
          if (disposed || track.kind !== "audio") return
          if (participant?.local) setMicAnalyser(null)
          else setBotAnalyser(null)
        },
        onBotStartedSpeaking: () => {
          if (disposed) return
          setBotSpeaking(true)
          // New bot turn — clear the previous transcript so it doesn't
          // linger across replies.
          setBotTranscript("")
        },
        onBotStoppedSpeaking: () => {
          if (disposed) return
          setBotSpeaking(false)
        },
        onUserTranscript: (data: { text?: string; final?: boolean } = {}) => {
          if (disposed) return
          if (data.final && data.text) setUserTranscript(data.text)
        },
        onBotTranscript: (data: { text?: string } = {}) => {
          if (disposed) return
          if (data.text) {
            // Bot transcript arrives in chunks as TTS streams. Append
            // unless we just reset on bot-started-speaking (in which
            // case we're at "" and the chunk becomes the start).
            setBotTranscript((prev) => (prev ? prev + data.text : data.text ?? null))
          }
        },
        onError: (message) => {
          if (disposed) return
          let text: string
          if (typeof message === "string") {
            text = message
          } else if (message instanceof Error) {
            text = message.message
          } else if (message && typeof message === "object" && "message" in message) {
            text = String((message as { message: unknown }).message)
          } else {
            text = String(message)
          }
          setError(new Error(text))
          setStatus("error")
        },
      },
    })
    clientRef.current = client

    void (async () => {
      try {
        await client.initDevices()
        await client.connect({ wsUrl: url })
      } catch (err) {
        if (disposed) return
        setError(err as Error)
        setStatus("error")
      }
    })()

    if (audioContext.state === "suspended") {
      void audioContext.resume().catch((err) => {
        if (disposed) return
        // Most often a browser autoplay-policy block — without surfacing
        // it the user sees a flat visualizer with no error to act on.
        setError(err instanceof Error ? err : new Error(String(err)))
      })
    }

    return () => {
      disposed = true
      clientRef.current = null
      void client.disconnect().catch((err) => {
        // eslint-disable-next-line no-console
        console.warn("PipecatClient disconnect failed", err)
      })
      void audioContext.close().catch((err) => {
        // eslint-disable-next-line no-console
        console.warn("AudioContext close failed", err)
      })
      audioContextRef.current = null
      setMicAnalyser(null)
      setBotAnalyser(null)
      setBotSpeaking(false)
      setUserTranscript(null)
      setBotTranscript(null)
      setStatus("idle")
    }
  }, [url, fftSize])

  // Switch input device when the user picks a different mic. The client
  // throws if updateMic is called pre-connect, and a failure halfway
  // through leaves the previous mic active — disable it so the UI's
  // "switched device" state matches reality.
  React.useEffect(() => {
    const client = clientRef.current
    if (!client || !inputDeviceId || status !== "active") return
    try {
      client.updateMic(inputDeviceId)
    } catch (err) {
      try {
        client.enableMic(false)
      } catch {
        // already torn down — nothing to do
      }
      setError(
        new Error(
          `Failed to switch microphone to ${inputDeviceId}: ${
            err instanceof Error ? err.message : String(err)
          }`,
        ),
      )
    }
  }, [inputDeviceId, status])

  // Toggle the mic without tearing down the connection. Gated on status
  // because @pipecat-ai/client-js throws if enableMic is called before
  // connect() resolves; without this guard the first mute toggle on
  // mount silently breaks the WebSocket.
  React.useEffect(() => {
    const client = clientRef.current
    if (!client || status !== "active") return
    try {
      client.enableMic(!muted)
    } catch (err) {
      setError(err instanceof Error ? err : new Error(String(err)))
    }
  }, [muted, status])

  return {
    micAnalyser,
    botAnalyser,
    botSpeaking,
    userTranscript,
    botTranscript,
    status,
    error,
  }
}
