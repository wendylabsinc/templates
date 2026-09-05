import { useEffect, useRef, useState, useCallback } from "react"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Label } from "@/components/ui/label"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Empty, EmptyHeader, EmptyMedia, EmptyTitle, EmptyDescription } from "@/components/ui/empty"
import { type Device, resolveDeviceSelection, storeDevice } from "@/lib/device-storage"
import { AudioLinesIcon, MicOffIcon, AlertCircleIcon, WifiOffIcon } from "lucide-react"

const MICROPHONE_STORAGE_KEY = "wendy.fullstack.microphoneDevice"

export default function AudioPage() {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const wsRef = useRef<WebSocket | null>(null)
  const selectedMicRef = useRef("")
  const receivedDataRef = useRef(false)
  const [status, setStatus] = useState<"connecting" | "live" | "no-feed" | "error">("connecting")
  const [errorMsg, setErrorMsg] = useState<string | null>(null)
  const audioDataRef = useRef(new Float32Array(0))
  const [microphones, setMicrophones] = useState<Device[]>([])
  const [micsLoaded, setMicsLoaded] = useState(false)
  const [selectedMic, setSelectedMic] = useState<string>("")

  const sendSelectedMic = useCallback((micId: string) => {
    if (micId && wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ switch_microphone: micId }))
    }
  }, [])

  const fetchMics = useCallback(() => {
    fetch("/api/microphones")
      .then((r) => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json() })
      .then((list: Device[]) => {
        setMicrophones(list)
        setMicsLoaded(true)
        setSelectedMic((current) => resolveDeviceSelection(list, current, MICROPHONE_STORAGE_KEY))
      })
      .catch((e) => {
        setMicsLoaded(true)
        setErrorMsg(`Failed to list microphones: ${e.message}`)
      })
  }, [])

  useEffect(() => {
    fetchMics()
    const id = setInterval(fetchMics, 3000)
    return () => clearInterval(id)
  }, [fetchMics])

  useEffect(() => {
    selectedMicRef.current = selectedMic
    if (!selectedMic) return

    storeDevice(MICROPHONE_STORAGE_KEY, selectedMic)
    sendSelectedMic(selectedMic)
  }, [selectedMic, sendSelectedMic])

  useEffect(() => {
    let animId: number
    let noDataTimer: ReturnType<typeof setTimeout> | null = null

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
      receivedDataRef.current = false

      ws.onopen = () => {
        setStatus("connecting")
        setErrorMsg(null)
        sendSelectedMic(selectedMicRef.current)
        noDataTimer = setTimeout(() => {
          if (!receivedDataRef.current) setStatus("no-feed")
        }, 5000)
      }
      ws.onmessage = (e) => {
        if (e.data instanceof ArrayBuffer) {
          const pcm = new Int16Array(e.data)
          const floats = new Float32Array(pcm.length)
          for (let i = 0; i < pcm.length; i++) floats[i] = pcm[i] / 32768
          audioDataRef.current = floats
          receivedDataRef.current = true
          setStatus("live")
          if (noDataTimer) { clearTimeout(noDataTimer); noDataTimer = null }
        }
      }
      ws.onclose = (e) => {
        if (noDataTimer) { clearTimeout(noDataTimer); noDataTimer = null }
        if (e.code === 1011) {
          setStatus("error")
          setErrorMsg("Audio capture pipeline failed to start. Check device logs.")
        } else {
          setStatus("connecting")
        }
        audioDataRef.current = new Float32Array(0)
        setTimeout(connect, 2000)
      }
      ws.onerror = () => {
        setStatus("error")
        setErrorMsg("WebSocket connection failed")
      }
    }

    connect()
    return () => {
      cancelAnimationFrame(animId)
      if (noDataTimer) clearTimeout(noDataTimer)
      wsRef.current?.close()
    }
  }, [sendSelectedMic])

  const showWaveform = status === "live" && microphones.length > 0

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

      {errorMsg && (
        <Alert variant="destructive">
          <AlertCircleIcon className="h-4 w-4" />
          <AlertDescription>{errorMsg}</AlertDescription>
        </Alert>
      )}

      <Card>
        <CardHeader className="flex flex-row items-center justify-between pb-2">
          <CardTitle className="text-sm font-medium">Microphone Waveform</CardTitle>
          {showWaveform && (
            <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <span className="h-2 w-2 rounded-full bg-green-500" />
              Live
            </span>
          )}
        </CardHeader>
        <CardContent>
          {showWaveform ? (
            <canvas ref={canvasRef} className="h-48 w-full rounded-lg" style={{ background: "black" }} />
          ) : micsLoaded && microphones.length === 0 ? (
            <Empty className="min-h-[200px] border">
              <EmptyHeader>
                <EmptyMedia variant="icon">
                  <MicOffIcon />
                </EmptyMedia>
                <EmptyTitle>No microphones detected</EmptyTitle>
                <EmptyDescription>
                  Plug in a USB audio device and it will appear here automatically.
                  Make sure the <code className="rounded bg-muted px-1 py-0.5 text-xs">audio</code> entitlement is enabled in your wendy.json.
                </EmptyDescription>
              </EmptyHeader>
            </Empty>
          ) : status === "no-feed" && microphones.length > 0 ? (
            <Empty className="min-h-[200px] border">
              <EmptyHeader>
                <EmptyMedia variant="icon">
                  <WifiOffIcon />
                </EmptyMedia>
                <EmptyTitle>No audio data</EmptyTitle>
                <EmptyDescription>
                  A microphone was detected but no audio samples are being received.
                  The GStreamer pipeline may have failed to start — check the device logs.
                </EmptyDescription>
              </EmptyHeader>
            </Empty>
          ) : status === "error" ? (
            <Empty className="min-h-[200px] border">
              <EmptyHeader>
                <EmptyMedia variant="icon">
                  <AlertCircleIcon />
                </EmptyMedia>
                <EmptyTitle>Audio error</EmptyTitle>
                <EmptyDescription>
                  {errorMsg || "Failed to connect to the audio stream. The backend may not be running."}
                </EmptyDescription>
              </EmptyHeader>
            </Empty>
          ) : (
            <Empty className="min-h-[200px] border">
              <EmptyHeader>
                <EmptyMedia variant="icon">
                  <AudioLinesIcon />
                </EmptyMedia>
                <EmptyTitle>Connecting...</EmptyTitle>
                <EmptyDescription>
                  Waiting for audio stream from the device.
                </EmptyDescription>
              </EmptyHeader>
            </Empty>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
