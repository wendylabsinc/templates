import { useEffect, useRef, useState } from "react"
import {
  SensingContext,
  type AnalyticsFrame,
  type StreamStatus,
} from "@/hooks/use-sensing-stream"

/**
 * Holds a single WebSocket to /ws/stream and shares the latest AnalyticsFrame
 * with the whole app. Auto-reconnects after a short delay on close.
 */
export function SensingProvider({ children }: { children: React.ReactNode }) {
  const [frame, setFrame] = useState<AnalyticsFrame | null>(null)
  const [status, setStatus] = useState<StreamStatus>("connecting")
  const wsRef = useRef<WebSocket | null>(null)

  useEffect(() => {
    let closed = false
    let retry: ReturnType<typeof setTimeout> | undefined

    function connect() {
      if (closed) return
      setStatus("connecting")
      const proto = location.protocol === "https:" ? "wss" : "ws"
      const ws = new WebSocket(`${proto}://${location.host}/ws/stream`)
      wsRef.current = ws

      ws.onopen = () => setStatus("open")
      ws.onmessage = (ev) => {
        try {
          setFrame(JSON.parse(ev.data) as AnalyticsFrame)
        } catch {
          /* ignore malformed frame */
        }
      }
      ws.onclose = () => {
        setStatus("closed")
        if (!closed) retry = setTimeout(connect, 2000)
      }
      ws.onerror = () => ws.close()
    }

    connect()
    return () => {
      closed = true
      if (retry) clearTimeout(retry)
      wsRef.current?.close()
    }
  }, [])

  return (
    <SensingContext.Provider value={{ frame, status }}>
      {children}
    </SensingContext.Provider>
  )
}
