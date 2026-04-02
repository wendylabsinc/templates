import { useEffect, useRef, useState } from "react"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"

export default function AudioPage() {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const [status, setStatus] = useState<"connecting" | "live" | "error">("connecting")
  const audioDataRef = useRef(new Float32Array(0))

  useEffect(() => {
    let ws: WebSocket | null = null
    let animId: number

    function draw() {
      const canvas = canvasRef.current
      if (!canvas) { animId = requestAnimationFrame(draw); return }
      const ctx = canvas.getContext("2d")!
      const rect = canvas.getBoundingClientRect()
      canvas.width = rect.width * devicePixelRatio
      canvas.height = rect.height * devicePixelRatio
      ctx.scale(devicePixelRatio, devicePixelRatio)

      ctx.fillStyle = "black"
      ctx.fillRect(0, 0, rect.width, rect.height)

      const data = audioDataRef.current
      if (data.length > 0) {
        const bars = Math.min(data.length, Math.floor(rect.width / 3))
        const step = Math.floor(data.length / bars)
        const barW = Math.max(1, rect.width / bars - 1)
        const midY = rect.height / 2
        ctx.fillStyle = "white"
        for (let i = 0; i < bars; i++) {
          let sum = 0
          for (let j = 0; j < step; j++) sum += Math.abs(data[i * step + j] || 0)
          const amp = sum / step
          const h = Math.max(2, amp * rect.height * 0.8)
          ctx.fillRect(i * (rect.width / bars), midY - h / 2, barW, h)
        }
      }

      animId = requestAnimationFrame(draw)
    }
    animId = requestAnimationFrame(draw)

    function connect() {
      const proto = location.protocol === "https:" ? "wss:" : "ws:"
      ws = new WebSocket(`${proto}//${location.host}/api/audio/stream`)
      ws.binaryType = "arraybuffer"

      ws.onopen = () => setStatus("live")
      ws.onmessage = (e) => {
        if (e.data instanceof ArrayBuffer) {
          const pcm = new Int16Array(e.data)
          const floats = new Float32Array(pcm.length)
          for (let i = 0; i < pcm.length; i++) floats[i] = pcm[i] / 32768
          audioDataRef.current = floats
        }
      }
      ws.onclose = () => {
        setStatus("connecting")
        audioDataRef.current = new Float32Array(0)
        setTimeout(connect, 2000)
      }
      ws.onerror = () => setStatus("error")
    }

    connect()
    return () => {
      cancelAnimationFrame(animId)
      ws?.close()
    }
  }, [])

  return (
    <div className="flex flex-col gap-4 p-4 md:gap-6 md:p-6">
      <h1 className="text-2xl font-bold tracking-tight">Audio</h1>
      <Card>
        <CardHeader className="flex flex-row items-center justify-between pb-2">
          <CardTitle className="text-sm font-medium">Microphone Waveform</CardTitle>
          <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <span
              className={`h-2 w-2 rounded-full ${
                status === "live"
                  ? "bg-green-500"
                  : status === "error"
                    ? "bg-red-500"
                    : "bg-yellow-500"
              }`}
            />
            {status === "live" ? "Live" : status === "error" ? "Error" : "Connecting..."}
          </span>
        </CardHeader>
        <CardContent>
          <canvas
            ref={canvasRef}
            className="h-48 w-full rounded-lg"
            style={{ background: "black" }}
          />
        </CardContent>
      </Card>
      <p className="text-sm text-muted-foreground">
        Connect a USB microphone. The waveform streams PCM S16LE 16kHz mono via WebSocket.
        Add a <code className="rounded bg-muted px-1 py-0.5">/api/audio/stream</code> WebSocket endpoint to your backend.
      </p>
    </div>
  )
}
