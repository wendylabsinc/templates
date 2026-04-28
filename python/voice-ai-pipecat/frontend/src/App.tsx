import * as React from "react"
import { Mic, MicOff, Settings } from "lucide-react"
import { MicrophoneSelector, type MicrophoneSelection } from "./components/MicrophoneSelector"
import { LifestreamVisualizer } from "./components/LifestreamVisualizer"
import { ErrorAlerts } from "./components/ErrorAlerts"
import { SettingsDrawer } from "./components/SettingsDrawer"
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "./components/ui/tooltip"
import { usePipecatClient, useWendyosMicrophones, useShowTranscripts } from "./audio"

function resolveBotWsUrl(): string {
  const override = (import.meta.env.VITE_BOT_WS_URL as string | undefined) ?? null
  if (override) return override
  if (typeof window === "undefined") return ""
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:"
  return `${proto}//${window.location.host}/bot-audio`
}

type AppState = "initializing" | "listening" | "thinking" | "speaking" | "error"

const STATE_STYLES: Record<
  AppState,
  { label: string; ring: string; dot: string; pulse: boolean }
> = {
  initializing: {
    label: "Initializing…",
    ring: "border-emerald-300/20 text-emerald-300/60 bg-black/50",
    dot: "bg-emerald-300/40",
    pulse: false,
  },
  listening: {
    label: "Listening",
    ring: "border-blue-400/60 text-blue-200 bg-blue-500/10",
    dot: "bg-blue-400",
    pulse: true,
  },
  thinking: {
    label: "Thinking…",
    ring: "border-amber-400/60 text-amber-200 bg-amber-500/10",
    dot: "bg-amber-400",
    pulse: true,
  },
  speaking: {
    label: "Speaking",
    ring: "border-emerald-400/60 text-emerald-200 bg-emerald-500/10",
    dot: "bg-emerald-400",
    pulse: true,
  },
  error: {
    label: "Disconnected",
    ring: "border-red-400/60 text-red-200 bg-red-500/10",
    dot: "bg-red-400",
    pulse: false,
  },
}

function App() {
  const [selection, setSelection] = React.useState<MicrophoneSelection | null>(null)
  const [muted, setMuted] = React.useState(false)
  const [settingsOpen, setSettingsOpen] = React.useState(false)
  const [showTranscripts] = useShowTranscripts()
  // When true the browser pipeline is intentionally disconnected so the
  // server can resume its local mic+speaker. Re-arms automatically whenever
  // the user picks a different mic.
  const [handedOff, setHandedOff] = React.useState(false)

  // Only the browser mic source is wired up today; wendyos-sourced audio lands
  // once the agent client exists (see useWendyosMicrophones).
  const browserDeviceId = selection?.kind === "browser" ? selection.id : null

  React.useEffect(() => {
    setHandedOff(false)
  }, [browserDeviceId])

  const botWsUrl = React.useMemo(resolveBotWsUrl, [])
  const client = usePipecatClient({
    url: browserDeviceId && !handedOff ? botWsUrl : null,
    inputDeviceId: browserDeviceId,
    muted,
  })

  // Surface backend-side audio errors (hot-plug events, pipeline crashes)
  // even when the user is on a browser mic — a USB unplug on the device
  // matters either way.
  const { status: wendyosStatus, error: wendyosFetchError } = useWendyosMicrophones()
  const wendyosNotice = React.useMemo<Error | null>(() => {
    if (wendyosFetchError) return wendyosFetchError
    if (!wendyosStatus) return null
    if (wendyosStatus.deviceMissing) {
      return new Error(
        wendyosStatus.error ?? "Host audio device disconnected. Plug it back in.",
      )
    }
    if (wendyosStatus.error && selection?.kind === "wendyos") {
      return new Error(wendyosStatus.error)
    }
    return null
  }, [wendyosFetchError, wendyosStatus, selection])

  const appState = React.useMemo<AppState>(() => {
    // Hard error states first.
    if (wendyosFetchError) return "error"
    if (wendyosStatus?.deviceMissing) return "error"
    if (client.status === "error") return "error"
    // Browser session takes priority — once the WS is up the user is
    // talking to that pipeline regardless of local-mode status.
    if (browserDeviceId && !handedOff) {
      if (client.botSpeaking) return "speaking"
      if (wendyosStatus?.processing) return "thinking"
      if (client.status === "active") return "listening"
      return "initializing"
    }
    // Local mode.
    if (wendyosStatus?.mode === "local") {
      if (wendyosStatus.processing) return "thinking"
      return "listening"
    }
    return "initializing"
  }, [
    wendyosFetchError,
    wendyosStatus,
    client.status,
    client.botSpeaking,
    browserDeviceId,
    handedOff,
  ])
  const stateStyle = STATE_STYLES[appState]
  const latencyLabel = React.useMemo(() => {
    const ms = wendyosStatus?.lastResponseTimeMs
    if (ms == null) return null
    return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`
  }, [wendyosStatus?.lastResponseTimeMs])

  // Trigger a brief flash whenever the wake-pulse counter increments.
  // Polling interval is ~1.2 s, so the flash lands within that window
  // of the actual wake-word fire.
  const [wakeFlashing, setWakeFlashing] = React.useState(false)
  const seenWakePulseRef = React.useRef<number | null>(null)
  React.useEffect(() => {
    const pulse = wendyosStatus?.wakePulse ?? 0
    if (seenWakePulseRef.current === null) {
      seenWakePulseRef.current = pulse
      return
    }
    if (pulse > seenWakePulseRef.current) {
      seenWakePulseRef.current = pulse
      setWakeFlashing(true)
      const id = window.setTimeout(() => setWakeFlashing(false), 1500)
      return () => window.clearTimeout(id)
    }
  }, [wendyosStatus?.wakePulse])

  return (
    <TooltipProvider>
      <main className="relative h-screen w-screen overflow-hidden bg-black text-white">
        {/* Visualizer Background */}
        <LifestreamVisualizer
          micAnalyser={client.micAnalyser}
          botAnalyser={client.botAnalyser}
          botSpeaking={client.botSpeaking}
          lineCount={40}
        />

        {/* Wake-fired flash — full-screen amber pulse on top of the
            visualizer, fades after 1.5s. Lets the user see the wake
            word landed even if the chime isn't loud enough. */}
        <div
          aria-hidden="true"
          className={
            "pointer-events-none absolute inset-0 z-30 transition-opacity duration-300 " +
            (wakeFlashing
              ? "opacity-100 ring-4 ring-inset ring-amber-300/70"
              : "opacity-0 ring-0")
          }
        />

        {/* Top-center status pill — quick visual cue for what the bot is
            doing right now. Pulses while listening / thinking / speaking,
            and shows the most recent round-trip latency on hover. */}
        <div className="pointer-events-none absolute left-1/2 top-6 z-20 -translate-x-1/2">
          <div
            title={
              latencyLabel ? `Last response in ${latencyLabel}` : undefined
            }
            className={
              "flex items-center gap-2 rounded-full border px-3 py-1 text-xs font-medium backdrop-blur-sm " +
              stateStyle.ring
            }
          >
            <span
              className={
                "h-2 w-2 rounded-full " +
                stateStyle.dot +
                (stateStyle.pulse ? " animate-pulse" : "")
              }
            />
            <span>{stateStyle.label}</span>
            {latencyLabel && (
              <span className="ml-1 font-mono text-[10px] opacity-60">
                · {latencyLabel}
              </span>
            )}
          </div>
        </div>

        {/* UI Layer */}
        <div className="pointer-events-none relative z-10 flex h-full flex-col p-6">
          <header className="flex w-full items-center justify-between">
            <div className="flex flex-col gap-1">
              <h1 className="text-2xl font-bold tracking-tight text-emerald-400 drop-shadow-md">
                Voice AI — Pipecat
              </h1>
              {!selection && (
                <p className="text-emerald-300/60 text-sm italic">
                  Select a Microphone
                </p>
              )}
            </div>

            <div className="pointer-events-auto flex items-center gap-2">
              {client.status === "active" && (
                <Tooltip>
                  <TooltipTrigger asChild>
                    <button
                      type="button"
                      onClick={() => setHandedOff(true)}
                      className="rounded-full border border-emerald-500/40 bg-black/60 px-3 py-1.5 text-sm text-emerald-300 transition-colors hover:bg-emerald-500/10 focus:outline-none focus:ring-2 focus:ring-emerald-500/60"
                    >
                      Hand back to local mic
                    </button>
                  </TooltipTrigger>
                  <TooltipContent side="bottom" sideOffset={8}>
                    Disconnect the browser so the device's local mic+speaker takes over.
                  </TooltipContent>
                </Tooltip>
              )}
              <MicrophoneSelector onDeviceSelect={setSelection} />
              <Tooltip>
                <TooltipTrigger asChild>
                  <button
                    type="button"
                    onClick={() => setSettingsOpen(true)}
                    aria-label="Open settings"
                    className="flex h-9 w-9 items-center justify-center rounded-md border border-emerald-500/30 bg-black/50 text-emerald-300 transition-colors hover:bg-emerald-500/10 focus:outline-none focus:ring-2 focus:ring-emerald-500/60"
                  >
                    <Settings className="h-4 w-4" />
                  </button>
                </TooltipTrigger>
                <TooltipContent side="bottom" sideOffset={8}>
                  Settings
                </TooltipContent>
              </Tooltip>
            </div>
          </header>

          <div className="mt-4">
            <ErrorAlerts
              micError={client.error}
              botError={null}
              wendyosError={wendyosNotice}
            />
          </div>

          {/* Center-bottom mute toggle */}
          {browserDeviceId && (
            <div className="pointer-events-auto absolute bottom-8 left-1/2 -translate-x-1/2">
              <Tooltip>
                <TooltipTrigger asChild>
                  <button
                    type="button"
                    onClick={() => setMuted((m) => !m)}
                    aria-label={muted ? "Unmute microphone" : "Mute microphone"}
                    aria-pressed={muted}
                    className={
                      "flex h-14 w-14 items-center justify-center rounded-full border bg-black/60 backdrop-blur-sm transition-colors focus:outline-none focus:ring-2 focus:ring-emerald-500/60 " +
                      (muted
                        ? "border-red-500/40 text-red-400 hover:bg-red-500/10"
                        : "border-emerald-500/40 text-emerald-300 hover:bg-emerald-500/10")
                    }
                  >
                    {muted ? <MicOff className="h-6 w-6" /> : <Mic className="h-6 w-6" />}
                  </button>
                </TooltipTrigger>
                <TooltipContent side="top" sideOffset={8}>
                  {muted ? "Click to unmute" : "Click to mute."}
                </TooltipContent>
              </Tooltip>
            </div>
          )}

          {showTranscripts && (client.userTranscript || client.botTranscript) && (
            <div className="pointer-events-none mx-auto mt-4 flex w-full max-w-2xl flex-col gap-2 px-4">
              {client.userTranscript && (
                <p className="rounded-md bg-blue-500/10 px-3 py-2 text-sm text-blue-200 backdrop-blur-sm">
                  <span className="mr-2 text-xs uppercase tracking-wide text-blue-400/70">
                    you
                  </span>
                  {client.userTranscript}
                </p>
              )}
              {client.botTranscript && (
                <p className="rounded-md bg-emerald-500/10 px-3 py-2 text-sm text-emerald-200 backdrop-blur-sm">
                  <span className="mr-2 text-xs uppercase tracking-wide text-emerald-400/70">
                    bot
                  </span>
                  {client.botTranscript}
                </p>
              )}
            </div>
          )}

          <footer className="mt-auto flex w-full items-end justify-between">
            <div className="max-w-md">
              <p className="text-emerald-300/40 text-xs italic">
                {selection?.kind === "wendyos"
                  ? `Listening on ${selection.id} — speak to interact`
                  : !selection
                    ? "Please select a microphone (or talk to the device directly)"
                    : handedOff
                      ? "Handed off to local mic — pick a microphone to take over"
                      : muted
                        ? "Microphone is muted"
                        : "Speak to interact"}
              </p>
            </div>

            <div className="pointer-events-auto">
              <a href="https://wendy.sh/docs" target="_blank" rel="noopener noreferrer" className="block transition-opacity hover:opacity-100">
                <img
                  src="/logo_with_text.svg"
                  alt="Wendy Logo"
                  className="h-12 w-auto brightness-0 invert opacity-80"
                />
              </a>
            </div>
          </footer>
        </div>
      </main>
      <SettingsDrawer open={settingsOpen} onOpenChange={setSettingsOpen} />
    </TooltipProvider>
  )
}

export default App
