import { useEffect, useState } from "react"
import { Check, ChevronsUpDown, Play, Square } from "lucide-react"
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

// FPS values shown in the dropdown. The D415 firmware only resolves a
// fixed set of FPS per stream profile (6, 15, 30, 60, 90), so 5/10/24 are
// never accepted by `pipeline.start`. 120 stays in the list purely as a
// disabled affordance — useful to communicate "this camera can't do that".
const FPS_OPTIONS = [6, 15, 30, 60, 120]

// FPS values the D415 actually supports with all four streams enabled at
// the same resolution. Color caps the combined set: 1080p / 720p color is
// 30 max; 480p color is 60 max. Depth/IR can do 90 at 480p but color can't,
// so 90 isn't offered. 1920×1080 isn't user-selectable but listed for
// completeness.
const SUPPORTED_FPS_BY_RESOLUTION: Record<string, number[]> = {
  "640x480": [6, 15, 30, 60],
  "1280x720": [6, 15, 30],
  "1920x1080": [6, 15, 30],
}
const FALLBACK_SUPPORTED_FPS = [15, 30]

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
  const supportedFps =
    SUPPORTED_FPS_BY_RESOLUTION[resolution] ?? FALLBACK_SUPPORTED_FPS

  // Poll /health for per-stream FPS while streaming. Counting at the browser
  // via <img onLoad> is unreliable for `multipart/x-mixed-replace` (Chrome
  // versions vary, Safari is hit-or-miss, Firefox doesn't support it at all),
  // so the pump tallies frames as it publishes them and we just read the
  // 1-second rolling snapshot from /health here.
  useEffect(() => {
    if (!streaming) return
    const tick = async () => {
      try {
        const res = await fetch("/health")
        const data = await res.json()
        console.log("[realsense] fps", data.fps)
      } catch {
        // ignore — next tick will retry
      }
    }
    tick()
    const id = window.setInterval(tick, 1000)
    return () => window.clearInterval(id)
  }, [streaming])

  // If the user switches to a resolution whose supported FPS set doesn't
  // include the currently selected value, snap to the highest supported FPS
  // — otherwise we'd POST something the D415 firmware refuses to resolve.
  useEffect(() => {
    setFps((current) =>
      supportedFps.includes(current) ? current : Math.max(...supportedFps)
    )
  }, [supportedFps])

  // Push the current resolution / FPS / preset to the server whenever the
  // user changes a control (and once on mount, so the pump has fresh values
  // before the first <img> connects). The backend hot-applies preset changes
  // and restarts its pipeline thread for resolution/FPS changes — existing
  // MJPEG connections stay open through the restart, so we don't need to
  // remount the <img> tags.
  useEffect(() => {
    const [width = "640", height = "480"] = resolution.split("x")
    console.log("[realsense] config", { resolution, fps, preset })
    const params = new URLSearchParams({
      width,
      height,
      fps: String(fps),
      preset,
    })
    const controller = new AbortController()
    fetch(`/config?${params}`, {
      method: "POST",
      signal: controller.signal,
    }).catch(() => {
      // Server may not be reachable yet; the next change will retry.
    })
    return () => controller.abort()
  }, [resolution, fps, preset])

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
            {/* D415 depth/IR streams cap at 1280×720; 1080p is color-only and
                this template enables all four streams at one resolution. */}
            <SelectItem value="1920x1080" disabled>
              1920 × 1080
            </SelectItem>
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
                  {FPS_OPTIONS.map((f) => {
                    // Disable any FPS not in the D415's supported set for the
                    // current resolution (see SUPPORTED_FPS_BY_RESOLUTION).
                    // Shown greyed out instead of hidden so the user can see
                    // which options exist but aren't available right now.
                    const unsupported = !supportedFps.includes(f)
                    return (
                      <CommandItem
                        key={f}
                        value={String(f)}
                        disabled={unsupported}
                        onSelect={(v) => {
                          if (unsupported) return
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
                    )
                  })}
                </CommandGroup>
              </CommandList>
            </Command>
          </PopoverContent>
        </Popover>

        <div className="ml-auto">
          <Button
            onClick={() => {
              if (!streaming) {
                setStreamSession((n) => n + 1)
              }
              setStreaming((s) => !s)
            }}
            variant={streaming ? "secondary" : "default"}
          >
            {streaming ? <Square fill="currentColor" /> : <Play />}
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
