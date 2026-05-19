import * as React from "react"
import {
  Cpu,
  HardDrive,
  MemoryStick,
  Monitor,
  Network,
  Server,
  Sparkles,
  Terminal,
  Thermometer,
} from "lucide-react"

import { Badge } from "@/components/ui/badge"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Separator } from "@/components/ui/separator"
import { Switch } from "@/components/ui/switch"
import {
  setSetting,
  useSettings,
  type ThemePreference,
} from "@/lib/settings"

type DetailRow = {
  icon: React.ComponentType<{ className?: string }>
  label: string
  value: string
  hint?: string
}

type StatusResponse = {
  backend: string
  appId: string
  model: {
    requested: string
    selected: string
    preset: string
    reason: string
  }
  llama: {
    baseUrl: string
    managed: boolean
    running: boolean
    healthy: boolean
  }
  runtime: {
    contextSize: string
    gpuLayers: string
    threads: string
    cacheTypeK: string
    cacheTypeV: string
  }
  system: {
    hostname: string
    platform: string
    deviceType: string
    architecture: string
    memoryGiB: number | null
  }
}

function buildHostDetails(status: StatusResponse | null): DetailRow[] {
  return [
    {
      icon: Server,
      label: "Hostname",
      value: status?.system.hostname ?? "Loading",
    },
    {
      icon: Terminal,
      label: "Runtime",
      value: status?.system.platform ?? "WendyOS",
      hint: status?.system.deviceType || undefined,
    },
    {
      icon: Cpu,
      label: "Architecture",
      value: status?.system.architecture ?? "Unknown",
    },
    {
      icon: MemoryStick,
      label: "Total Memory",
      value:
        status?.system.memoryGiB != null
          ? `${status.system.memoryGiB} GiB`
          : "Unknown",
    },
    {
      icon: Sparkles,
      label: "Model",
      value: status?.model.selected ?? "Selecting Gemma 4 model",
      hint: status?.model.preset,
    },
    {
      icon: HardDrive,
      label: "Model cache",
      value: "/models",
      hint: "Persisted",
    },
    {
      icon: Network,
      label: "llama.cpp API",
      value: status?.llama.baseUrl ?? "Starting",
      hint: status?.llama.managed ? "Managed" : "External",
    },
    {
      icon: Monitor,
      label: "Context",
      value: status ? `${status.runtime.contextSize} tokens` : "Loading",
      hint: status ? `${status.runtime.gpuLayers} GPU layers` : undefined,
    },
    {
      icon: Thermometer,
      label: "KV cache",
      value: status
        ? `${status.runtime.cacheTypeK}/${status.runtime.cacheTypeV}`
        : "Loading",
    },
  ]
}

export function SettingsPage() {
  const settings = useSettings()
  const [status, setStatus] = React.useState<StatusResponse | null>(null)
  const [statusError, setStatusError] = React.useState("")

  React.useEffect(() => {
    let cancelled = false

    async function loadStatus() {
      try {
        const response = await fetch("/api/status")
        if (!response.ok) throw new Error(`HTTP ${response.status}`)
        const nextStatus = (await response.json()) as StatusResponse
        if (!cancelled) {
          setStatus(nextStatus)
          setStatusError("")
        }
      } catch (error) {
        if (!cancelled) {
          setStatusError(error instanceof Error ? error.message : String(error))
        }
      }
    }

    void loadStatus()
    const id = window.setInterval(loadStatus, 5000)
    return () => {
      cancelled = true
      window.clearInterval(id)
    }
  }, [])

  const hostDetails = buildHostDetails(status)
  const llamaReady = status?.llama.healthy ?? false

  return (
    <div className="mx-auto w-full max-w-3xl px-6 py-10 space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Settings</h1>
        <p className="text-sm text-muted-foreground">
          Configure your chat experience.
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Appearance</CardTitle>
          <CardDescription>
            Preferences saved to this device.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-6">
          <div className="flex items-center justify-between gap-4">
            <div className="space-y-0.5">
              <Label htmlFor="theme">Theme</Label>
              <p className="text-sm text-muted-foreground">
                Browser Default follows your OS preference.
              </p>
            </div>
            <Select
              value={settings.theme}
              onValueChange={(v) => setSetting("theme", v as ThemePreference)}
            >
              <SelectTrigger id="theme" className="w-44">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="system">Browser Default</SelectItem>
                <SelectItem value="light">Light Mode</SelectItem>
                <SelectItem value="dark">Dark Mode</SelectItem>
              </SelectContent>
            </Select>
          </div>

          <Separator />

          <div className="flex items-center justify-between gap-4">
            <div className="space-y-0.5">
              <Label htmlFor="chat-full-width">Full-width chat</Label>
              <p className="text-sm text-muted-foreground">
                Stretch messages and the input across the entire window.
              </p>
            </div>
            <Switch
              id="chat-full-width"
              checked={settings.chatFullWidth}
              onCheckedChange={(checked) =>
                setSetting("chatFullWidth", checked)
              }
            />
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <div className="flex items-center justify-between gap-2">
            <div>
              <CardTitle>Host machine</CardTitle>
              <CardDescription>
                Details about the device running this chat.
              </CardDescription>
            </div>
            <Badge variant="secondary" className="gap-1">
              <span
                className={
                  llamaReady
                    ? "size-1.5 rounded-full bg-emerald-500"
                    : "size-1.5 rounded-full bg-amber-500"
                }
              />
              {statusError ? "Status error" : llamaReady ? "Ready" : "Starting"}
            </Badge>
          </div>
        </CardHeader>
        <CardContent className="px-0">
          {statusError && (
            <p className="px-4 pb-3 text-sm text-destructive">
              Could not read backend status: {statusError}
            </p>
          )}
          {status?.model.reason && (
            <p className="px-4 pb-3 text-sm text-muted-foreground">
              {status.model.reason}
            </p>
          )}
          <Separator />
          <dl className="divide-y">
            {hostDetails.map(({ icon: Icon, label, value, hint }) => (
              <div
                key={label}
                className="grid grid-cols-[1fr_auto] items-center gap-4 px-4 py-3"
              >
                <dt className="flex items-center gap-2 text-sm text-muted-foreground">
                  <Icon className="size-4" />
                  {label}
                </dt>
                <dd className="flex items-center gap-2 text-sm font-medium">
                  <span>{value}</span>
                  {hint && (
                    <Badge variant="outline" className="font-normal">
                      {hint}
                    </Badge>
                  )}
                </dd>
              </div>
            ))}
          </dl>
        </CardContent>
      </Card>
    </div>
  )
}
