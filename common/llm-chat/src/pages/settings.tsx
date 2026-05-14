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

const hostDetails: DetailRow[] = [
  { icon: Server, label: "Hostname", value: "wendyos-zestful-stork.local" },
  { icon: Terminal, label: "OS", value: "WendyOS 0.5.2", hint: "Yocto kirkstone" },
  { icon: Cpu, label: "Architecture", value: "aarch64 (ARM64)" },
  {
    icon: Cpu,
    label: "CPU",
    value: "ARM Cortex-A78AE · 6 cores @ 1.5 GHz",
  },
  { icon: MemoryStick, label: "Total Memory", value: "7.4 GiB" },
  {
    icon: HardDrive,
    label: "Storage",
    value: "234 GiB NVMe · 41% used",
  },
  {
    icon: Sparkles,
    label: "Accelerator",
    value: "NVIDIA Orin (Ampere) · 1024 CUDA cores",
    hint: "CUDA 12.2",
  },
  { icon: Monitor, label: "Display", value: "Headless" },
  { icon: Network, label: "IP Address", value: "192.168.1.42" },
  { icon: Thermometer, label: "Thermal", value: "44°C · Nominal" },
]

export function SettingsPage() {
  const settings = useSettings()

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
              <span className="size-1.5 rounded-full bg-emerald-500" />
              Online
            </Badge>
          </div>
        </CardHeader>
        <CardContent className="px-0">
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
