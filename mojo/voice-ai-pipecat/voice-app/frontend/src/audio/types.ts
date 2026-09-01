export type AudioSourceStatus = "idle" | "connecting" | "active" | "error"

export interface AudioSource {
  /** AnalyserNode for reading frequency/time-domain data. Null until the source is active. */
  analyser: AnalyserNode | null
  status: AudioSourceStatus
  error: Error | null
}
