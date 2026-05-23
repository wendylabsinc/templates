import * as React from "react"
import { getAuthToken } from "./auth"

/**
 * In local mode the bot's audio never reaches the browser, so the
 * Pipecat WebSocket transport's AnalyserNodes have nothing to react
 * to and the visualizer goes static. The bot publishes RMS levels
 * (mic and bot) over a small `/api/audio-levels` WebSocket; this
 * hook subscribes and exposes the most recent values as React state.
 *
 * Returned levels are normalised int16 RMS in [0, 1]. Both `micLevel`
 * and `botLevel` decay back toward 0 on their own — values arrive at
 * ~30 Hz from the bot, so the visualizer should re-render on each
 * `useFrame` tick and pick up the freshest value without needing
 * additional smoothing here.
 *
 * The hook is no-op when `enabled` is false (e.g. in browser-mic
 * mode where AnalyserNodes already drive the visualizer).
 */
export interface AudioLevelsState {
  /** Most recent mic-channel RMS level, [0, 1]. */
  micLevel: number
  /** Most recent bot-channel RMS level, [0, 1]. */
  botLevel: number
  /** Whether the WebSocket is currently connected. */
  connected: boolean
}

export interface UseAudioLevelsOptions {
  /** ws(s):// URL for `/api/audio-levels`. Null/undefined keeps idle. */
  url: string | null | undefined
  /** Enable subscribing. Set false when AnalyserNodes are driving instead. */
  enabled?: boolean
  /** Reconnect delay on close (ms). */
  reconnectMs?: number
}

export function useAudioLevels(options: UseAudioLevelsOptions): AudioLevelsState {
  const { url, enabled = true, reconnectMs = 1500 } = options
  // Refs hold "live" values updated on every WS message — no React
  // re-render per packet (30Hz would be wasteful). The visualizer's
  // `useFrame` reads the refs each animation tick instead.
  const micRef = React.useRef(0)
  const botRef = React.useRef(0)
  // Connected state is re-rendered (rare) so the consumer can hide
  // the visualizer or surface a warning.
  const [connected, setConnected] = React.useState(false)
  // Re-render at the visualizer's animation rate to surface fresh
  // refs to React-driven components. We bump a tick once per frame
  // via requestAnimationFrame.
  const [_, setTick] = React.useState(0)

  React.useEffect(() => {
    if (!enabled || !url) {
      setConnected(false)
      return
    }
    let ws: WebSocket | null = null
    let reconnectTimer: number | null = null
    let cancelled = false

    const connect = () => {
      if (cancelled) return
      try {
        const token = getAuthToken()
        const withToken = token
          ? url + (url.includes("?") ? "&" : "?") + `token=${encodeURIComponent(token)}`
          : url
        ws = new WebSocket(withToken)
      } catch (err) {
        console.warn("useAudioLevels: WebSocket constructor failed:", err)
        return
      }

      ws.addEventListener("open", () => {
        if (cancelled) return
        setConnected(true)
      })
      ws.addEventListener("message", (event) => {
        try {
          const msg = JSON.parse(event.data as string)
          const lvl = typeof msg.level === "number" ? Math.max(0, Math.min(1, msg.level)) : 0
          if (msg.channel === "mic") micRef.current = lvl
          else if (msg.channel === "bot") botRef.current = lvl
        } catch {
          // Bad packet — ignore.
        }
      })
      ws.addEventListener("close", () => {
        if (cancelled) return
        setConnected(false)
        // Soft reconnect to survive bot rebuilds (settings POST → new pipeline).
        reconnectTimer = window.setTimeout(connect, reconnectMs)
      })
      ws.addEventListener("error", () => {
        // `close` fires right after, so cleanup happens there.
      })
    }

    connect()

    // Drive a per-frame React re-render so callers using the
    // returned values (not the refs) see fresh data. Lightweight —
    // setState with a tick value is cheap and React batches updates.
    let rafId: number | null = null
    const tick = () => {
      setTick((n) => (n + 1) & 0xffff)
      rafId = window.requestAnimationFrame(tick)
    }
    rafId = window.requestAnimationFrame(tick)

    return () => {
      cancelled = true
      if (reconnectTimer !== null) window.clearTimeout(reconnectTimer)
      if (rafId !== null) window.cancelAnimationFrame(rafId)
      if (ws) {
        try {
          ws.close()
        } catch {
          // ignore
        }
      }
      ws = null
      setConnected(false)
    }
  }, [enabled, url, reconnectMs])

  return {
    micLevel: enabled ? micRef.current : 0,
    botLevel: enabled ? botRef.current : 0,
    connected,
  }
}
