import * as React from "react"

export interface WendyosMicrophone {
  /** Opaque device id understood by the wendy-agent RPC (e.g., ALSA `hw:1,0`). */
  id: string
  /** Human-readable label surfaced in the selector UI. */
  label: string
}

export interface WendyosMicrophonesState {
  devices: WendyosMicrophone[]
  error: Error | null
}

/**
 * Enumerate microphones reported by the wendy-agent running on the host device
 * (as opposed to the browser's own `navigator.mediaDevices.enumerateDevices`).
 *
 * TODO(WDY-936 follow-up): wire up the wendy-agent RPC client. For now this
 * returns an empty list so the selector falls back to browser mics only. When
 * the agent client lands, fetch the device list here and subscribe to hot-plug
 * events so the combobox refreshes on plug/unplug.
 */
export function useWendyosMicrophones(): WendyosMicrophonesState {
  const [state] = React.useState<WendyosMicrophonesState>({
    devices: [],
    error: null,
  })
  return state
}
