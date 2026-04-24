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
          console.log("[usePipecatClient] onConnected")
          if (disposed) return
          setStatus("active")
        },
        onDisconnected: () => {
          console.log("[usePipecatClient] onDisconnected")
          if (disposed) return
          setStatus("idle")
        },
        onBotReady: (data) => {
          console.log("[usePipecatClient] onBotReady", data)
        },
        onTrackStarted: (track, participant) => {
          console.log("[usePipecatClient] onTrackStarted", { kind: track.kind, local: participant?.local, participant })
          if (disposed || track.kind !== "audio") return
          const analyser = buildAnalyser(track)
          if (participant?.local) setMicAnalyser(analyser)
          else setBotAnalyser(analyser)
        },
        onTrackStopped: (track, participant) => {
          console.log("[usePipecatClient] onTrackStopped", { kind: track.kind, local: participant?.local })
          if (disposed || track.kind !== "audio") return
          if (participant?.local) setMicAnalyser(null)
          else setBotAnalyser(null)
        },
        onLocalAudioLevel: (level) => {
          const n = Number(level)
          if (n > 0.05) console.log("[usePipecatClient] local mic level:", n.toFixed(3))
        },
        onRemoteAudioLevel: (level) => {
          const n = Number(level)
          if (n > 0.05) console.log("[usePipecatClient] remote bot level:", n.toFixed(3))
        },
        onError: (message) => {
          console.error("[usePipecatClient] onError", message)
          if (disposed) return
          setError(new Error(typeof message === "string" ? message : JSON.stringify(message)))
          setStatus("error")
        },
      },
    })
    clientRef.current = client
    console.log("[usePipecatClient] client created", { url, micEnabled: true })

    void (async () => {
      try {
        console.log("[usePipecatClient] calling initDevices...")
        await client.initDevices()
        console.log("[usePipecatClient] initDevices done; connecting...", { url })
        await client.connect({ wsUrl: url })
        console.log("[usePipecatClient] connect resolved")
      } catch (err) {
        console.error("[usePipecatClient] connect failed", err)
        if (disposed) return
        setError(err as Error)
        setStatus("error")
      }
    })()

    if (audioContext.state === "suspended") {
      void audioContext.resume().catch(() => {})
    }

    return () => {
      disposed = true
      clientRef.current = null
      void client.disconnect().catch(() => {})
      void audioContext.close().catch(() => {})
      audioContextRef.current = null
      setMicAnalyser(null)
      setBotAnalyser(null)
      setStatus("idle")
    }
  }, [url, fftSize])

  // Switch input device when the user picks a different mic.
  React.useEffect(() => {
    const client = clientRef.current
    if (!client || !inputDeviceId) return
    try {
      client.updateMic(inputDeviceId)
    } catch (err) {
      setError(err as Error)
    }
  }, [inputDeviceId])

  // Toggle the mic without tearing down the connection.
  React.useEffect(() => {
    const client = clientRef.current
    if (!client) return
    client.enableMic(!muted)
  }, [muted])

  return { micAnalyser, botAnalyser, status, error }
}
