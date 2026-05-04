import * as React from "react"
import { authHeaders } from "./auth"

export interface WendyosMicrophone {
  /** Stable handle for the device-side selector (the device's `name`). */
  id: string
  /** Human-readable label surfaced in the selector UI. */
  label: string
  /** Whether the device can capture audio (input_channels > 0). */
  hasInput: boolean
  /** Whether the device can play audio (output_channels > 0). */
  hasOutput: boolean
}

export interface WendyosStatus {
  mode: string
  inputName: string | null
  outputName: string | null
  deviceMissing: boolean
  error: string | null
  /** True between user-stopped-speaking and bot-started-speaking — i.e.
   *  while STT/LLM/TTS-startup is running. Drives the "Thinking" pill. */
  processing: boolean
  /** Round-trip ms from end-of-user-speech to bot's first audio.
   *  Cleared until the first turn lands. */
  lastResponseTimeMs: number | null
  /** Epoch seconds when the wake word last fired, or null. */
  lastWakeAt: number | null
  /** Monotonically-increasing counter — frontend triggers a flash on
   *  every increment. More reliable than diffing a timestamp because
   *  it survives across polling intervals where timestamp may not yet
   *  have updated server-side. */
  wakePulse: number
}

export interface WendyosMicrophonesState {
  /** Input-capable host devices reported by /api/audio-devices. */
  devices: WendyosMicrophone[]
  /** Backend session status (mode, current device, last error). */
  status: WendyosStatus | null
  /** Last fetch error from /api/audio-devices or /api/status. */
  error: Error | null
  /** POSTs /api/local-audio/select. Returns when the backend has switched. */
  selectInput: (id: string) => Promise<void>
}

// 1.2s gives a wake-flash latency the user actually notices; the
// payloads are small (a couple hundred bytes each) and on localhost
// so cost is negligible.
const POLL_MS = 1_200

interface BackendDevice {
  id: number
  name: string
  input_channels: number
  output_channels: number
  default_sample_rate: number
}

interface BackendStatus {
  mode: string
  input_name: string | null
  output_name: string | null
  device_missing: boolean
  error: string | null
  processing: boolean
  last_response_time_ms: number | null
  last_wake_at: number | null
  wake_pulse: number
}

/**
 * Polls the Pipecat backend for host-side audio devices (PyAudio enumeration)
 * and exposes a `selectInput` action that asks the backend to restart its
 * local pipeline against the chosen device. Polling at POLL_MS doubles as
 * hot-plug detection — when the user plugs/unplugs a USB mic the next tick
 * picks up the change.
 */
export function useWendyosMicrophones(): WendyosMicrophonesState {
  const [devices, setDevices] = React.useState<WendyosMicrophone[]>([])
  const [status, setStatus] = React.useState<WendyosStatus | null>(null)
  const [error, setError] = React.useState<Error | null>(null)

  // Dedup state updates by content. tick() runs every 1.2s; without
  // these refs each poll produces fresh-identity arrays/objects that
  // re-fire downstream effects (notably MicrophoneSelector's
  // auto-fallback, which races the backend hot-plug recovery).
  const lastDevicesJson = React.useRef<string>("")
  const lastStatusJson = React.useRef<string>("")

  const tick = React.useCallback(async () => {
    try {
      const [devicesRes, statusRes] = await Promise.all([
        fetch("/api/audio-devices"),
        fetch("/api/status"),
      ])
      // A 5xx from a polling endpoint silently parses to `{}` if we
      // skip the .ok check, leaving devices=[]/status=null/error=null
      // — the UI shows "no devices" with nothing to act on. Surface
      // the failure so ErrorAlerts displays the status code instead.
      if (!devicesRes.ok) throw new Error(`/api/audio-devices ${devicesRes.status}`)
      if (!statusRes.ok) throw new Error(`/api/status ${statusRes.status}`)
      const devicesData = (await devicesRes.json()) as { devices?: BackendDevice[] }
      const statusData = (await statusRes.json()) as BackendStatus
      const raw: BackendDevice[] = devicesData.devices ?? []
      const inputs: WendyosMicrophone[] = raw
        .filter((d) => d.input_channels > 0)
        .map((d) => ({
          id: d.name,
          // Relabel the ALSA "default" alias so it's obvious this is the
          // recommended pick — it routes through asound.conf's plug to the
          // USB mic with rate conversion. Raw `(hw:N,M)` entries stay
          // visible for power users but show a "raw" hint.
          label:
            d.name === "default"
              ? "Default (auto-routed to USB mic)"
              : /\(hw:\d+,\d+\)/.test(d.name)
                ? `${d.name} — raw, may not work at 16 kHz`
                : d.name,
          hasInput: true,
          hasOutput: d.output_channels > 0,
        }))
      const inputsJson = JSON.stringify(inputs)
      if (inputsJson !== lastDevicesJson.current) {
        lastDevicesJson.current = inputsJson
        setDevices(inputs)
      }
      const nextStatus: WendyosStatus = {
        mode: statusData.mode,
        inputName: statusData.input_name,
        outputName: statusData.output_name,
        deviceMissing: statusData.device_missing,
        error: statusData.error,
        processing: statusData.processing,
        lastResponseTimeMs: statusData.last_response_time_ms,
        lastWakeAt: statusData.last_wake_at,
        wakePulse: statusData.wake_pulse ?? 0,
      }
      const statusJson = JSON.stringify(nextStatus)
      if (statusJson !== lastStatusJson.current) {
        lastStatusJson.current = statusJson
        setStatus(nextStatus)
      }
      setError(null)
    } catch (err) {
      setError(err as Error)
    }
  }, [])

  React.useEffect(() => {
    void tick()
    const id = window.setInterval(() => void tick(), POLL_MS)
    return () => window.clearInterval(id)
  }, [tick])

  const selectInput = React.useCallback(
    async (id: string) => {
      const res = await fetch("/api/local-audio/select", {
        method: "POST",
        headers: { "content-type": "application/json", ...authHeaders() },
        // Use the device name for both input & output; for USB speakerphones
        // (e.g. PowerConf) the same hardware does mic + speaker. Users with
        // separate mic/speaker devices can still drive this via env vars at
        // boot — we keep the API symmetric for now.
        body: JSON.stringify({ input_id: id, output_id: id }),
      })
      if (!res.ok) {
        // Prefer the FastAPI HTTPException detail over a bare status so
        // the ErrorAlerts banner says e.g. "Audio device 'foo' not found"
        // instead of "Failed to select input: 400".
        let detail = `Failed to select input: ${res.status}`
        try {
          const body = (await res.json()) as { detail?: unknown }
          if (typeof body?.detail === "string" && body.detail) detail = body.detail
        } catch {
          // non-JSON body — keep status-based detail
        }
        const err = new Error(detail)
        // Surface to the same error state ErrorAlerts watches; the next
        // successful tick() clears it.
        setError(err)
        throw err
      }
      await tick()
    },
    [tick],
  )

  return { devices, status, error, selectInput }
}
