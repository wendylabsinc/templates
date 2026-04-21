import * as React from "react"
import { Mic, MicOff } from "lucide-react"
import { MicrophoneSelector, type MicrophoneSelection } from "./components/MicrophoneSelector"
import { LifestreamVisualizer } from "./components/LifestreamVisualizer"
import { ErrorAlerts } from "./components/ErrorAlerts"
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "./components/ui/tooltip"
import { useMicrophoneSource, useWebSocketSource } from "./audio"

function resolveBotWsUrl(): string {
  const override = (import.meta.env.VITE_BOT_WS_URL as string | undefined) ?? null
  if (override) return override
  if (typeof window === "undefined") return ""
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:"
  return `${proto}//${window.location.host}/bot-audio`
}

function App() {
  const [selection, setSelection] = React.useState<MicrophoneSelection | null>(null)
  const [muted, setMuted] = React.useState(false)

  // Only the browser mic source is wired up today; wendyos-sourced audio lands
  // once the agent client exists (see useWendyosMicrophones).
  const browserDeviceId = selection?.kind === "browser" ? selection.id : null
  const mic = useMicrophoneSource(browserDeviceId, { muted })

  const botWsUrl = React.useMemo(resolveBotWsUrl, [])
  const bot = useWebSocketSource({ url: botWsUrl, sampleRate: 16000 })

  const wendyosNotice =
    selection?.kind === "wendyos"
      ? new Error(
          "WendyOS-sourced microphones are selected but the agent client isn't wired up yet. " +
            "Pick a Browser mic, or see useWendyosMicrophones for the integration TODO.",
        )
      : null

  return (
    <TooltipProvider>
      <main className="relative h-screen w-screen overflow-hidden bg-black text-white">
        {/* Visualizer Background */}
        <LifestreamVisualizer
          micAnalyser={mic.analyser}
          botAnalyser={bot.analyser}
          lineCount={40}
        />

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

            <div className="pointer-events-auto">
              <MicrophoneSelector onDeviceSelect={setSelection} />
            </div>
          </header>

          <div className="mt-4">
            <ErrorAlerts
              micError={mic.error}
              botError={bot.error}
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

          <footer className="mt-auto flex w-full items-end justify-between">
            <div className="max-w-md">
              <p className="text-emerald-300/40 text-xs italic">
                {!selection
                  ? "Please select a microphone"
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
    </TooltipProvider>
  )
}

export default App
