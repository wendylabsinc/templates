import { useEffect, useRef, useState, useCallback } from "react"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Label } from "@/components/ui/label"

interface Device { id: string; name: string }

export default function AudioPage() {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const wsRef = useRef<WebSocket | null>(null)
  const [status, setStatus] = useState<"connecting" | "live" | "error">("connecting")
  const audioDataRef = useRef(new Float32Array(0))
  const [microphones, setMicrophones] = useState<Device[]>([])
  const [selectedMic, setSelectedMic] = useState<string>("")

  const fetchMics = useCallback(() => {
    fetch("/api/microphones")
      .then((r) => r.json())
      .then((list: Device[]) => {
        setMicrophones(list)
        if (!selectedMic && list.length > 0) setSelectedMic(list[0].id)
      })
      .catch(() => {})
  }, [selectedMic])

  // Poll for mic changes every 3s
  useEffect(() => {
    fetchMics()
    const id = setInterval(fetchMics, 3000)
    return () => clearInterval(id)
  }, [fetchMics])

  // Switch mic via WS
  useEffect(() => {
    if (selectedMic && wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ switch_microphone: selectedMic }))
    }
  }, [selectedMic])

  useEffect(() => {
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
      const ws = new WebSocket(`${proto}//${location.host}/api/audio/stream`)
      ws.binaryType = "arraybuffer"
      wsRef.current = ws

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
      wsRef.current?.close()
    }
  }, [])

  return (
    <div className="flex flex-col gap-4 p-4 md:gap-6 md:p-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold tracking-tight">Audio</h1>
        {microphones.length > 0 && (
          <div className="flex items-center gap-2">
            <Label className="text-sm text-muted-foreground">Microphone</Label>
            <Select value={selectedMic} onValueChange={(v) => v && setSelectedMic(v)}>
              <SelectTrigger className="w-[260px]">
                <SelectValue placeholder="Select microphone" />
              </SelectTrigger>
              <SelectContent>
                {microphones.map((m) => (
                  <SelectItem key={m.id} value={m.id}>{m.name}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        )}
      </div>
      <Card>
        <CardHeader className="flex flex-row items-center justify-between pb-2">
          <CardTitle className="text-sm font-medium">Microphone Waveform</CardTitle>
          <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <span className={`h-2 w-2 rounded-full ${status === "live" ? "bg-green-500" : status === "error" ? "bg-red-500" : "bg-yellow-500"}`} />
            {status === "live" ? "Live" : status === "error" ? "Error" : "Connecting..."}
          </span>
        </CardHeader>
        <CardContent>
          {microphones.length === 0 ? (
            <div className="flex h-48 items-center justify-center rounded-lg bg-black text-sm text-muted-foreground">
              No microphones detected. Plug in a USB audio device.
            </div>
          ) : (
            <canvas ref={canvasRef} className="h-48 w-full rounded-lg" style={{ background: "black" }} />
          )}
        </CardContent>
      </Card>
    </div>
  )
}
