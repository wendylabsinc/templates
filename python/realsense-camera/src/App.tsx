import { useState } from "react"
import { Check, ChevronsUpDown, Play } from "lucide-react"
import logoUrl from "@/assets/logo.svg"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover"
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command"
import { cn } from "@/lib/utils"

type StreamId = "color" | "ir-left" | "ir-right" | "depth"

type StreamDef = {
  id: StreamId
  label: string
  placeholder: string
}

const STREAMS: StreamDef[] = [
  { id: "color", label: "Color Stream", placeholder: "#3b82f6" },
  { id: "ir-left", label: "Left IR Stream", placeholder: "#a855f7" },
  { id: "ir-right", label: "Right IR Stream", placeholder: "#22c55e" },
  { id: "depth", label: "Depth Stream", placeholder: "#f97316" },
]

const PRESETS = [
  { value: "default", label: "Default" },
  { value: "high-accuracy", label: "High Accuracy" },
  { value: "high-density", label: "High Density" },
  { value: "medium-density", label: "Medium Density" },
  { value: "hand", label: "Hand" },
]

const FPS_OPTIONS = [5, 10, 15, 24, 30, 60, 120]

function App() {
  const [enabled, setEnabled] = useState<Record<StreamId, boolean>>({
    color: true,
    "ir-left": true,
    "ir-right": true,
    depth: true,
  })
  const [streamSession, setStreamSession] = useState(0)
  const [mode, setMode] = useState("live")
  const [resolution, setResolution] = useState("1280x720")
  const [preset, setPreset] = useState("default")
  const [presetOpen, setPresetOpen] = useState(false)
  const [fps, setFps] = useState(30)
  const [fpsOpen, setFpsOpen] = useState(false)
  const [streaming, setStreaming] = useState(false)

  const active = STREAMS.filter((s) => enabled[s.id])
  const isFullscreen = active.length === 1

  const gridClasses = (() => {
    if (active.length <= 1) return "grid grid-cols-1 grid-rows-1"
    if (active.length === 2) return "grid grid-cols-2 grid-rows-1"
    return "grid grid-cols-2 grid-rows-2"
  })()

  return (
    <div className="flex h-screen w-screen flex-col bg-black text-foreground">
      <header className="flex items-center justify-between border-b border-border/40 px-6 py-4">
        <a
          href="https://wendy.sh/docs"
          target="_blank"
          rel="noreferrer"
          className="inline-flex"
        >
          <img src={logoUrl} alt="Wendy" className="h-7 w-auto invert" />
        </a>
        <div className="flex flex-wrap items-center gap-5">
          {STREAMS.map((s) => (
            <label
              key={s.id}
              className="flex cursor-pointer items-center gap-2 text-sm font-medium select-none"
            >
              <Checkbox
                checked={enabled[s.id]}
                onCheckedChange={(v) =>
                  setEnabled((prev) => ({ ...prev, [s.id]: Boolean(v) }))
                }
              />
              <span>{s.label}</span>
            </label>
          ))}
        </div>
      </header>

      <div className="flex flex-wrap items-center gap-3 border-b border-border/40 px-6 py-3">
        <Tabs value={mode} onValueChange={setMode}>
          <TabsList>
            <TabsTrigger value="live">Live</TabsTrigger>
            <TabsTrigger value="recorded">Recorded</TabsTrigger>
          </TabsList>
        </Tabs>

        <Select value={resolution} onValueChange={setResolution}>
          <SelectTrigger className="w-[160px]">
            <SelectValue placeholder="Resolution" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="640x480">640 × 480</SelectItem>
            <SelectItem value="1280x720">1280 × 720</SelectItem>
            <SelectItem value="1920x1080">1920 × 1080</SelectItem>
          </SelectContent>
        </Select>

        <Popover open={presetOpen} onOpenChange={setPresetOpen}>
          <PopoverTrigger asChild>
            <Button
              variant="outline"
              role="combobox"
              aria-expanded={presetOpen}
              className="w-[200px] justify-between"
            >
              {PRESETS.find((p) => p.value === preset)?.label ?? "Preset..."}
              <ChevronsUpDown className="opacity-50" />
            </Button>
          </PopoverTrigger>
          <PopoverContent className="w-[200px] p-0">
            <Command>
              <CommandInput placeholder="Search preset..." />
              <CommandList>
                <CommandEmpty>No preset found.</CommandEmpty>
                <CommandGroup>
                  {PRESETS.map((p) => (
                    <CommandItem
                      key={p.value}
                      value={p.value}
                      onSelect={(v) => {
                        setPreset(v)
                        setPresetOpen(false)
                      }}
                    >
                      {p.label}
                      <Check
                        className={cn(
                          "ml-auto",
                          preset === p.value ? "opacity-100" : "opacity-0"
                        )}
                      />
                    </CommandItem>
                  ))}
                </CommandGroup>
              </CommandList>
            </Command>
          </PopoverContent>
        </Popover>

        <Popover open={fpsOpen} onOpenChange={setFpsOpen}>
          <PopoverTrigger asChild>
            <Button
              variant="outline"
              role="combobox"
              aria-expanded={fpsOpen}
              className="w-[160px] justify-between"
            >
              {`${fps} FPS`}
              <ChevronsUpDown className="opacity-50" />
            </Button>
          </PopoverTrigger>
          <PopoverContent className="w-[160px] p-0">
            <Command>
              <CommandInput placeholder="Search FPS..." />
              <CommandList>
                <CommandEmpty>No FPS found.</CommandEmpty>
                <CommandGroup>
                  {FPS_OPTIONS.map((f) => (
                    <CommandItem
                      key={f}
                      value={String(f)}
                      onSelect={(v) => {
                        setFps(Number(v))
                        setFpsOpen(false)
                      }}
                    >
                      {`${f} FPS`}
                      <Check
                        className={cn(
                          "ml-auto",
                          fps === f ? "opacity-100" : "opacity-0"
                        )}
                      />
                    </CommandItem>
                  ))}
                </CommandGroup>
              </CommandList>
            </Command>
          </PopoverContent>
        </Popover>

        <div className="ml-auto">
          <Button
            onClick={async () => {
              if (!streaming) {
                const [width, height] = resolution.split("x")
                try {
                  await fetch(
                    `/config?width=${width}&height=${height}&fps=${fps}`,
                    { method: "POST" }
                  )
                } catch {
                  // server may not be running yet; the <img> will surface the error
                }
                setStreamSession((n) => n + 1)
              }
              setStreaming((s) => !s)
            }}
            variant={streaming ? "secondary" : "default"}
          >
            <Play />
            {streaming ? "Stop" : "Start"}
          </Button>
        </div>
      </div>

      <main className="flex-1 overflow-hidden p-4">
        {active.length === 0 ? (
          <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
            Enable a stream to see camera output.
          </div>
        ) : (
          <div className={cn("h-full w-full gap-4", gridClasses)}>
            {active.map((s) => (
              <Card
                key={s.id}
                className="flex h-full min-h-0 flex-col overflow-hidden border-border/60 bg-zinc-950 py-0 gap-0"
              >
                <CardHeader className="border-b border-border/40 px-4 py-3">
                  <CardTitle className="text-sm font-medium tracking-wide">
                    {s.label}
                  </CardTitle>
                </CardHeader>
                <CardContent className="flex-1 min-h-0 p-0">
                  {streaming ? (
                    <img
                      key={`${s.id}-${streamSession}`}
                      src={`/stream/${s.id}?t=${streamSession}`}
                      alt={s.label}
                      className={cn(
                        "h-full w-full object-contain bg-black",
                        isFullscreen ? "rounded-none" : ""
                      )}
                    />
                  ) : (
                    <div
                      className={cn(
                        "h-full w-full",
                        isFullscreen ? "rounded-none" : ""
                      )}
                      style={{ backgroundColor: s.placeholder }}
                    />
                  )}
                </CardContent>
              </Card>
            ))}
          </div>
        )}
      </main>
    </div>
  )
}

export default App
