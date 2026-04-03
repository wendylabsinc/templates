import { useEffect, useRef, useState, useCallback } from "react"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Label } from "@/components/ui/label"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Empty, EmptyHeader, EmptyMedia, EmptyTitle, EmptyDescription } from "@/components/ui/empty"
import { CameraIcon, CameraOffIcon, AlertCircleIcon, WifiOffIcon } from "lucide-react"

interface Device { id: string; name: string }

export default function CameraPage() {
  const imgRef = useRef<HTMLImageElement>(null)
  const wsRef = useRef<WebSocket | null>(null)
  const [status, setStatus] = useState<"connecting" | "live" | "no-feed" | "error">("connecting")
  const [errorMsg, setErrorMsg] = useState<string | null>(null)
  const [fps, setFps] = useState(0)
  const [cameras, setCameras] = useState<Device[]>([])
  const [camerasLoaded, setCamerasLoaded] = useState(false)
  const [selectedCamera, setSelectedCamera] = useState<string>("")
  const [receivedFrame, setReceivedFrame] = useState(false)

  const fetchCameras = useCallback(() => {
    fetch("/api/cameras")
      .then((r) => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json() })
      .then((list: Device[]) => {
        setCameras(list)
        setCamerasLoaded(true)
        if (!selectedCamera && list.length > 0) setSelectedCamera(list[0].id)
      })
      .catch((e) => {
        setCamerasLoaded(true)
        setErrorMsg(`Failed to list cameras: ${e.message}`)
      })
  }, [selectedCamera])

  useEffect(() => {
    fetchCameras()
    const id = setInterval(fetchCameras, 3000)
    return () => clearInterval(id)
  }, [fetchCameras])

  useEffect(() => {
    if (selectedCamera && wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ switch_camera: selectedCamera }))
    }
  }, [selectedCamera])

  useEffect(() => {
    let frameCount = 0
    let lastTime = performance.now()
    let prevBlob: string | null = null
    let noFrameTimer: ReturnType<typeof setTimeout> | null = null

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
      setReceivedFrame(false)

      ws.onopen = () => {
        setStatus("connecting")
        setErrorMsg(null)
        // If no frame arrives within 5s, mark as no-feed
        noFrameTimer = setTimeout(() => {
          if (!receivedFrame) setStatus("no-feed")
        }, 5000)
      }
      ws.onmessage = (e) => {
        if (e.data instanceof Blob) {
          const url = URL.createObjectURL(e.data)
          if (imgRef.current) imgRef.current.src = url
          if (prevBlob) URL.revokeObjectURL(prevBlob)
          prevBlob = url
          frameCount++
          setReceivedFrame(true)
          setStatus("live")
          if (noFrameTimer) { clearTimeout(noFrameTimer); noFrameTimer = null }
        }
      }
      ws.onclose = (e) => {
        if (noFrameTimer) { clearTimeout(noFrameTimer); noFrameTimer = null }
        if (e.code === 1011) {
          setStatus("error")
          setErrorMsg("Camera pipeline failed to start. Check device logs.")
        } else {
          setStatus("connecting")
        }
        setTimeout(connect, 2000)
      }
      ws.onerror = () => {
        setStatus("error")
        setErrorMsg("WebSocket connection failed")
      }
    }

    connect()
    return () => {
      clearInterval(fpsInterval)
      if (noFrameTimer) clearTimeout(noFrameTimer)
      wsRef.current?.close()
    }
  }, [])

  const showFeed = status === "live" && cameras.length > 0

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

      {errorMsg && (
        <Alert variant="destructive">
          <AlertCircleIcon className="h-4 w-4" />
          <AlertDescription>{errorMsg}</AlertDescription>
        </Alert>
      )}

      <Card>
        <CardHeader className="flex flex-row items-center justify-between pb-2">
          <CardTitle className="text-sm font-medium">Live Feed</CardTitle>
          {showFeed && (
            <div className="flex items-center gap-3 text-xs text-muted-foreground">
              <span className="flex items-center gap-1.5">
                <span className="h-2 w-2 rounded-full bg-green-500" />
                Live
              </span>
              <span>{fps} FPS</span>
            </div>
          )}
        </CardHeader>
        <CardContent>
          {showFeed ? (
            <div className="relative aspect-video w-full overflow-hidden rounded-lg bg-black">
              <img ref={imgRef} alt="Camera feed" className="h-full w-full object-contain" />
            </div>
          ) : camerasLoaded && cameras.length === 0 ? (
            <Empty className="min-h-[300px] border">
              <EmptyHeader>
                <EmptyMedia variant="icon">
                  <CameraOffIcon />
                </EmptyMedia>
                <EmptyTitle>No cameras detected</EmptyTitle>
                <EmptyDescription>
                  Plug in a USB webcam and it will appear here automatically.
                  Make sure the <code className="rounded bg-muted px-1 py-0.5 text-xs">video</code> entitlement is enabled in your wendy.json.
                </EmptyDescription>
              </EmptyHeader>
            </Empty>
          ) : status === "no-feed" && cameras.length > 0 ? (
            <Empty className="min-h-[300px] border">
              <EmptyHeader>
                <EmptyMedia variant="icon">
                  <WifiOffIcon />
                </EmptyMedia>
                <EmptyTitle>No video feed</EmptyTitle>
                <EmptyDescription>
                  A camera was detected but no frames are being received.
                  The GStreamer pipeline may have failed to start — check the device logs.
                </EmptyDescription>
              </EmptyHeader>
            </Empty>
          ) : status === "error" ? (
            <Empty className="min-h-[300px] border">
              <EmptyHeader>
                <EmptyMedia variant="icon">
                  <AlertCircleIcon />
                </EmptyMedia>
                <EmptyTitle>Camera error</EmptyTitle>
                <EmptyDescription>
                  {errorMsg || "Failed to connect to the camera stream. The backend may not be running."}
                </EmptyDescription>
              </EmptyHeader>
            </Empty>
          ) : (
            <Empty className="min-h-[300px] border">
              <EmptyHeader>
                <EmptyMedia variant="icon">
                  <CameraIcon />
                </EmptyMedia>
                <EmptyTitle>Connecting...</EmptyTitle>
                <EmptyDescription>
                  Waiting for camera feed from the device.
                </EmptyDescription>
              </EmptyHeader>
            </Empty>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
