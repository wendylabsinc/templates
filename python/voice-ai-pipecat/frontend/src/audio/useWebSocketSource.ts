import * as React from "react"
import type { AudioSource, AudioSourceStatus } from "./types"

export interface WebSocketSourceOptions {
  /** WebSocket URL. Null/undefined keeps the hook idle. */
  url: string | null | undefined
  /** Sample rate of incoming PCM. Default 24000. */
  sampleRate?: number
  /** Channel count of incoming PCM (interleaved). Default 1. */
  channels?: number
  /** Analyser FFT size. Default 256. */
  fftSize?: number
  /** WebSocket subprotocols. */
  protocols?: string | string[]
  /** If true, also route audio to speakers so the user hears the feed. Default true. */
  playback?: boolean
  /**
   * Decode an incoming WebSocket message into interleaved Float32 PCM samples.
   * Return null to skip the frame (e.g. control messages).
   *
   * Default: assumes binary ArrayBuffer frames of little-endian Int16 PCM.
   * Override for Opus, Ogg, base64-wrapped JSON, etc.
   *
   * The function identity is read via ref, so you do not need to memoize it.
   */
  decode?: (data: MessageEvent["data"]) => Float32Array | Promise<Float32Array> | null | undefined
}

const defaultPcm16Decoder: NonNullable<WebSocketSourceOptions["decode"]> = (data) => {
  if (!(data instanceof ArrayBuffer)) return null
  const view = new DataView(data)
  const sampleCount = Math.floor(data.byteLength / 2)
  const out = new Float32Array(sampleCount)
  for (let i = 0; i < sampleCount; i++) {
    out[i] = view.getInt16(i * 2, true) / 0x8000
  }
  return out
}

/**
 * Audio source backed by a WebSocket carrying a streaming audio feed.
 *
 * Incoming messages are decoded to Float32 PCM (via `decode`), wrapped in an
 * AudioBuffer, and scheduled back-to-back so playback stays gapless. The
 * AnalyserNode taps the same signal chain for visualization.
 */
export function useWebSocketSource(options: WebSocketSourceOptions): AudioSource {
  const {
    url,
    sampleRate = 24000,
    channels = 1,
    fftSize = 256,
    protocols,
    playback = true,
    decode = defaultPcm16Decoder,
  } = options

  const [analyser, setAnalyser] = React.useState<AnalyserNode | null>(null)
  const [wsOpen, setWsOpen] = React.useState(false)
  const [error, setError] = React.useState<Error | null>(null)

  const decodeRef = React.useRef(decode)
  React.useEffect(() => {
    decodeRef.current = decode
  }, [decode])

  React.useEffect(() => {
    if (!url) return

    let disposed = false
    let ws: WebSocket | null = null
    let audioContext: AudioContext | null = null
    let analyserNode: AnalyserNode | null = null
    let playbackTime = 0

    try {
      audioContext = new (window.AudioContext ||
        (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext)({
        sampleRate,
      })
      analyserNode = audioContext.createAnalyser()
      analyserNode.fftSize = fftSize
      if (playback) {
        analyserNode.connect(audioContext.destination)
      }

      ws = protocols ? new WebSocket(url, protocols) : new WebSocket(url)
      ws.binaryType = "arraybuffer"

      ws.onopen = () => {
        if (disposed || !audioContext) return
        playbackTime = audioContext.currentTime
        setAnalyser(analyserNode)
        setWsOpen(true)
      }

      ws.onerror = () => {
        if (disposed) return
        setError(new Error(`WebSocket error: ${url}`))
        setWsOpen(false)
      }

      ws.onclose = () => {
        if (disposed) return
        setWsOpen(false)
      }

      ws.onmessage = async (event) => {
        if (disposed || !audioContext || !analyserNode) return
        try {
          const pcm = await decodeRef.current(event.data)
          if (!pcm || pcm.length === 0) return

          const frames = Math.floor(pcm.length / channels)
          if (frames === 0) return

          const buffer = audioContext.createBuffer(channels, frames, sampleRate)
          for (let ch = 0; ch < channels; ch++) {
            const channelData = buffer.getChannelData(ch)
            for (let i = 0; i < frames; i++) {
              channelData[i] = pcm[i * channels + ch]
            }
          }

          const bufferSource = audioContext.createBufferSource()
          bufferSource.buffer = buffer
          bufferSource.connect(analyserNode)

          const now = audioContext.currentTime
          if (playbackTime < now) playbackTime = now
          bufferSource.start(playbackTime)
          playbackTime += buffer.duration
        } catch (err) {
          console.error("WebSocket audio decode error:", err)
        }
      }
    } catch (err) {
      setError(err as Error)
    }

    return () => {
      disposed = true
      if (ws && ws.readyState !== WebSocket.CLOSED && ws.readyState !== WebSocket.CLOSING) {
        ws.close()
      }
      audioContext?.close()
      setAnalyser(null)
      setWsOpen(false)
      setError(null)
    }
  }, [url, sampleRate, channels, fftSize, protocols, playback])

  const status: AudioSourceStatus = error
    ? "error"
    : !url
      ? "idle"
      : wsOpen
        ? "active"
        : "connecting"

  return { analyser, status, error }
}
