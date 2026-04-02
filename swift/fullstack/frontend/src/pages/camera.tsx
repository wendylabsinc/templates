import { useEffect, useRef, useState } from "react"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"

export default function CameraPage() {
  const imgRef = useRef<HTMLImageElement>(null)
  const [status, setStatus] = useState<"connecting" | "live" | "error">("connecting")
  const [fps, setFps] = useState(0)

  useEffect(() => {
    let ws: WebSocket | null = null
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
      ws = new WebSocket(`${proto}//${location.host}/api/camera/stream`)
      ws.binaryType = "blob"

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
      ws?.close()
    }
  }, [])

  return (
    <div className="flex flex-col gap-4 p-4 md:gap-6 md:p-6">
      <h1 className="text-2xl font-bold tracking-tight">Camera</h1>
      <Card>
        <CardHeader className="flex flex-row items-center justify-between pb-2">
          <CardTitle className="text-sm font-medium">Live Feed</CardTitle>
          <div className="flex items-center gap-3 text-xs text-muted-foreground">
            <span className="flex items-center gap-1.5">
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
            {status === "live" && <span>{fps} FPS</span>}
          </div>
        </CardHeader>
        <CardContent>
          <div className="relative aspect-video w-full overflow-hidden rounded-lg bg-black">
            <img
              ref={imgRef}
              alt="Camera feed"
              className="h-full w-full object-contain"
            />
          </div>
        </CardContent>
      </Card>
      <p className="text-sm text-muted-foreground">
        Connect a USB webcam to your device. The feed streams via GStreamer MJPEG over WebSocket.
        Add a <code className="rounded bg-muted px-1 py-0.5">/api/camera/stream</code> WebSocket endpoint to your backend.
      </p>
    </div>
  )
}
