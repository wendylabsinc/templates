import { createContext, useContext } from "react"

export interface SensorStat {
  link_id: string
  rssi: number
  channel: number
  packets: number
  last_seen: number
  malformed: number
}

export interface AnalyticsFrame {
  timestamp: number
  occupied: boolean
  motion: number
  breathing_bpm: number | null
  breathing_conf: number
  heart_bpm: number | null
  heart_conf: number
  sensors: SensorStat[]
  waterfall: Record<string, number[][]>
}

export type StreamStatus = "connecting" | "open" | "closed"

export interface SensingState {
  frame: AnalyticsFrame | null
  status: StreamStatus
}

export const SensingContext = createContext<SensingState>({
  frame: null,
  status: "connecting",
})

export function useSensing(): SensingState {
  return useContext(SensingContext)
}
