import { useMediaStreamAnalyser } from "./useMediaStreamAnalyser"
import type { AudioSource } from "./types"

/**
 * Audio source backed by a remote WebRTC MediaStream.
 *
 * You own the RTCPeerConnection and signaling — once `ontrack` fires and you have
 * a remote MediaStream, pass it here. The hook wraps it in an AnalyserNode.
 *
 * Typical usage:
 *
 *   const [remote, setRemote] = React.useState<MediaStream | null>(null)
 *   React.useEffect(() => {
 *     const pc = new RTCPeerConnection(rtcConfig)
 *     pc.ontrack = (e) => setRemote(e.streams[0])
 *     // ...perform your signaling handshake...
 *     return () => pc.close()
 *   }, [])
 *   const { analyser } = useWebRtcSource(remote)
 */
export function useWebRtcSource(
  remoteStream: MediaStream | null,
  fftSize = 256,
): AudioSource {
  return useMediaStreamAnalyser(remoteStream, fftSize)
}
