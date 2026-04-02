import { useEffect, useState } from "react"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"

interface SystemInfo {
  hostname?: string
  platform?: string
  architecture?: string
  uptime?: string
  memory?: { total: string; used: string; free: string }
  disk?: { total: string; used: string; free: string }
  cpu?: { model: string; cores: number }
}

export default function SystemPage() {
  const [info, setInfo] = useState<SystemInfo | null>(null)

  useEffect(() => {
    fetch("/api/system")
      .then((r) => r.json())
      .then(setInfo)
      .catch(() => setInfo(null))
  }, [])

  return (
    <div className="flex flex-col gap-4 p-4 md:gap-6 md:p-6">
      <h1 className="text-2xl font-bold tracking-tight">System Information</h1>
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
        <Card>
          <CardHeader>
            <CardTitle className="text-sm font-medium">Device</CardTitle>
            <CardDescription>Hardware and OS information</CardDescription>
          </CardHeader>
          <CardContent>
            <div className="space-y-3 text-sm">
              <Row label="Hostname" value={info?.hostname} />
              <Row label="Platform" value={info?.platform} />
              <Row label="Architecture" value={info?.architecture} />
              <Row label="Uptime" value={info?.uptime} />
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="text-sm font-medium">CPU</CardTitle>
            <CardDescription>Processor details</CardDescription>
          </CardHeader>
          <CardContent>
            <div className="space-y-3 text-sm">
              <Row label="Model" value={info?.cpu?.model} />
              <Row label="Cores" value={info?.cpu?.cores?.toString()} />
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="text-sm font-medium">Memory</CardTitle>
            <CardDescription>RAM usage</CardDescription>
          </CardHeader>
          <CardContent>
            <div className="space-y-3 text-sm">
              <Row label="Total" value={info?.memory?.total} />
              <Row label="Used" value={info?.memory?.used} />
              <Row label="Free" value={info?.memory?.free} />
            </div>
          </CardContent>
        </Card>
      </div>
      <p className="text-sm text-muted-foreground">
        Add a <code className="rounded bg-muted px-1 py-0.5">/api/system</code> endpoint to your backend
        that returns system information (hostname, memory, disk, CPU, uptime).
      </p>
    </div>
  )
}

function Row({ label, value }: { label: string; value?: string }) {
  return (
    <div className="flex justify-between">
      <span className="text-muted-foreground">{label}</span>
      <span className="font-medium">{value || "—"}</span>
    </div>
  )
}
