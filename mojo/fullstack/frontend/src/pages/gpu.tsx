import { useEffect, useState } from "react"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"

interface GpuInfo {
  available: boolean
  name?: string
  memory?: string
  driver?: string
  temperature?: string
}

export default function GpuPage() {
  const [gpu, setGpu] = useState<GpuInfo | null>(null)

  useEffect(() => {
    fetch("/api/gpu")
      .then((r) => r.json())
      .then(setGpu)
      .catch(() => setGpu({ available: false }))
  }, [])

  return (
    <div className="flex flex-col gap-4 p-4 md:gap-6 md:p-6">
      <h1 className="text-2xl font-bold tracking-tight">GPU</h1>
      <div className="grid gap-4 md:grid-cols-2">
        <Card>
          <CardHeader>
            <div className="flex items-center justify-between">
              <CardTitle className="text-sm font-medium">GPU Status</CardTitle>
              <Badge variant={gpu?.available ? "default" : "secondary"}>
                {gpu?.available ? "Available" : "Not detected"}
              </Badge>
            </div>
            <CardDescription>
              Hardware acceleration for AI inference and compute workloads.
            </CardDescription>
          </CardHeader>
          <CardContent>
            {gpu?.available ? (
              <div className="space-y-3 text-sm">
                {gpu.name && (
                  <div className="flex justify-between">
                    <span className="text-muted-foreground">Device</span>
                    <span className="font-medium">{gpu.name}</span>
                  </div>
                )}
                {gpu.memory && (
                  <div className="flex justify-between">
                    <span className="text-muted-foreground">Memory</span>
                    <span className="font-medium">{gpu.memory}</span>
                  </div>
                )}
                {gpu.driver && (
                  <div className="flex justify-between">
                    <span className="text-muted-foreground">Driver</span>
                    <span className="font-medium">{gpu.driver}</span>
                  </div>
                )}
                {gpu.temperature && (
                  <div className="flex justify-between">
                    <span className="text-muted-foreground">Temperature</span>
                    <span className="font-medium">{gpu.temperature}</span>
                  </div>
                )}
              </div>
            ) : (
              <p className="text-sm text-muted-foreground">
                No GPU detected. Ensure the <code className="rounded bg-muted px-1 py-0.5">gpu</code> entitlement
                is enabled in your wendy.json.
              </p>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="text-sm font-medium">Entitlement</CardTitle>
            <CardDescription>Add GPU access to your app</CardDescription>
          </CardHeader>
          <CardContent>
            <pre className="overflow-x-auto rounded-lg bg-muted p-4 text-xs">
{`{
  "entitlements": [
    { "type": "gpu" }
  ]
}`}
            </pre>
          </CardContent>
        </Card>
      </div>
      <p className="text-sm text-muted-foreground">
        Add a <code className="rounded bg-muted px-1 py-0.5">/api/gpu</code> endpoint to your backend
        that returns GPU info (e.g. via <code className="rounded bg-muted px-1 py-0.5">nvidia-smi</code> on Jetson).
      </p>
    </div>
  )
}
