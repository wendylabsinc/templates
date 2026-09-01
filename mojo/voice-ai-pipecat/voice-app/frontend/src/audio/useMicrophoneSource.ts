import * as React from "react"
import { useMediaStreamAnalyser } from "./useMediaStreamAnalyser"
import type { AudioSource } from "./types"

export interface MicrophoneSourceOptions {
  fftSize?: number
  /** When true, the mic tracks are disabled — visualizer sees silence, but the
   *  stream stays open so toggling back on is instant (no permission re-prompt). */
  muted?: boolean
}

/**
 * Audio source backed by the browser microphone (getUserMedia).
 * Pass a deviceId from MicrophoneSelector, or null to disconnect.
 */
export function useMicrophoneSource(
  deviceId: string | null,
  options: MicrophoneSourceOptions = {},
): AudioSource {
  const { fftSize = 256, muted = false } = options
  const [stream, setStream] = React.useState<MediaStream | null>(null)
  const [error, setError] = React.useState<Error | null>(null)

  React.useEffect(() => {
    if (!deviceId) return

    let cancelled = false
    let acquired: MediaStream | null = null

    navigator.mediaDevices
      .getUserMedia({ audio: { deviceId: { exact: deviceId } } })
      .then((s) => {
        if (cancelled) {
          s.getTracks().forEach((t) => t.stop())
          return
        }
        acquired = s
        setStream(s)
        setError(null)
      })
      .catch((err: Error) => {
        if (cancelled) return
        setError(err)
      })

    return () => {
      cancelled = true
      acquired?.getTracks().forEach((t) => t.stop())
      setStream(null)
      setError(null)
    }
  }, [deviceId])

  React.useEffect(() => {
    if (!stream) return
    stream.getAudioTracks().forEach((t) => {
      t.enabled = !muted
    })
  }, [stream, muted])

  const downstream = useMediaStreamAnalyser(stream, fftSize)
  return { ...downstream, error: error ?? downstream.error }
}
