import { useEffect, useRef, useState, useCallback } from "react"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Label } from "@/components/ui/label"

interface Device { id: string; name: string }

export default function CameraPage() {
  const imgRef = useRef<HTMLImageElement>(null)
  const wsRef = useRef<WebSocket | null>(null)
  const [status, setStatus] = useState<"connecting" | "live" | "error">("connecting")
  const [fps, setFps] = useState(0)
  const [cameras, setCameras] = useState<Device[]>([])
  const [selectedCamera, setSelectedCamera] = useState<string>("")

  const fetchCameras = useCallback(() => {
    fetch("/api/cameras")
      .then((r) => r.json())
      .then((list: Device[]) => {
        setCameras(list)
        if (!selectedCamera && list.length > 0) setSelectedCamera(list[0].id)
      })
      .catch(() => {})
  }, [selectedCamera])

  // Poll for camera changes every 3s
  useEffect(() => {
    fetchCameras()
    const id = setInterval(fetchCameras, 3000)
    return () => clearInterval(id)
  }, [fetchCameras])

  // Switch camera via WS
  useEffect(() => {
    if (selectedCamera && wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ switch_camera: selectedCamera }))
    }
  }, [selectedCamera])

  useEffect(() => {
    let frameCount = 0
    let lastTime = performance.now()
    let prevBlob: string | null = null

    const fpsInterval = setInterval(() => {
      const now = performance.now()
      const elapsed = now - lastTime
      if (elapsed >= 1000) {
        setFps(Math.round((frameCount * 1000) / elapsed))
        frameCount = 0
        lastTime = now
      }
    }, 1000)

    function connect() {
      const proto = location.protocol === "https:" ? "wss:" : "ws:"
      const ws = new WebSocket(`${proto}//${location.host}/api/camera/stream`)
      ws.binaryType = "blob"
      wsRef.current = ws

      ws.onopen = () => setStatus("live")
      ws.onmessage = (e) => {
        if (e.data instanceof Blob) {
          const url = URL.createObjectURL(e.data)
          if (imgRef.current) imgRef.current.src = url
          if (prevBlob) URL.revokeObjectURL(prevBlob)
          prevBlob = url
          frameCount++
        }
      }
      ws.onclose = () => {
        setStatus("connecting")
        setTimeout(connect, 2000)
      }
      ws.onerror = () => setStatus("error")
    }

    connect()
    return () => {
      clearInterval(fpsInterval)
      wsRef.current?.close()
    }
  }, [])

  return (
    <div className="flex flex-col gap-4 p-4 md:gap-6 md:p-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold tracking-tight">Camera</h1>
        {cameras.length > 0 && (
          <div className="flex items-center gap-2">
            <Label className="text-sm text-muted-foreground">Device</Label>
            <Select value={selectedCamera} onValueChange={(v) => v && setSelectedCamera(v)}>
              <SelectTrigger className="w-[260px]">
                <SelectValue placeholder="Select camera" />
              </SelectTrigger>
              <SelectContent>
                {cameras.map((c) => (
                  <SelectItem key={c.id} value={c.id}>{c.name}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        )}
      </div>
      <Card>
        <CardHeader className="flex flex-row items-center justify-between pb-2">
          <CardTitle className="text-sm font-medium">Live Feed</CardTitle>
          <div className="flex items-center gap-3 text-xs text-muted-foreground">
            <span className="flex items-center gap-1.5">
              <span className={`h-2 w-2 rounded-full ${status === "live" ? "bg-green-500" : status === "error" ? "bg-red-500" : "bg-yellow-500"}`} />
              {status === "live" ? "Live" : status === "error" ? "Error" : "Connecting..."}
            </span>
            {status === "live" && <span>{fps} FPS</span>}
          </div>
        </CardHeader>
        <CardContent>
          <div className="relative aspect-video w-full overflow-hidden rounded-lg bg-black">
            {cameras.length === 0 ? (
              <div className="flex h-full items-center justify-center text-sm text-muted-foreground">No cameras detected. Plug in a USB webcam.</div>
            ) : (
              <img ref={imgRef} alt="Camera feed" className="h-full w-full object-contain" />
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  )
}
