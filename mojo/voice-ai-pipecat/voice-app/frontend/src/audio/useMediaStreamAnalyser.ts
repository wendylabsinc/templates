import * as React from "react"
import type { AudioSource, AudioSourceStatus } from "./types"

/**
 * Wraps a MediaStream in an AnalyserNode so the visualizer can read frequency data.
 *
 * Shared building block used by both the microphone and WebRTC sources — anything
 * that can produce a MediaStream with an audio track goes through here.
 */
export function useMediaStreamAnalyser(
  stream: MediaStream | null,
  fftSize = 256,
): AudioSource {
  const [analyser, setAnalyser] = React.useState<AnalyserNode | null>(null)
  const [error, setError] = React.useState<Error | null>(null)

  React.useEffect(() => {
    if (!stream) return

    let audioContext: AudioContext | null = null
    let source: MediaStreamAudioSourceNode | null = null

    try {
      audioContext = new (window.AudioContext ||
        (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext)()
      const node = audioContext.createAnalyser()
      node.fftSize = fftSize
      source = audioContext.createMediaStreamSource(stream)
      source.connect(node)
      setAnalyser(node)
      setError(null)
    } catch (err) {
      setError(err as Error)
    }

    return () => {
      source?.disconnect()
      audioContext?.close()
      setAnalyser(null)
      setError(null)
    }
  }, [stream, fftSize])

  const status: AudioSourceStatus = error
    ? "error"
    : !stream
      ? "idle"
      : analyser
        ? "active"
        : "connecting"

  return { analyser, status, error }
}
