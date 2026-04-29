"use client"

import * as React from "react"
import {
  Combobox,
  ComboboxTrigger,
  ComboboxContent,
  ComboboxList,
  ComboboxGroup,
  ComboboxLabel,
  ComboboxItem,
  ComboboxEmpty,
} from "@/components/ui/combobox"
import { Mic } from "lucide-react"
import { useWendyosMicrophones } from "@/audio"

export type MicrophoneKind = "wendyos" | "browser"

export interface MicrophoneSelection {
  kind: MicrophoneKind
  id: string
}

interface MicrophoneSelectorProps {
  onDeviceSelect: (selection: MicrophoneSelection | null) => void
}

const STORAGE_KEY = "selected-mic-id"

function encodeValue(sel: MicrophoneSelection): string {
  return `${sel.kind}:${sel.id}`
}

function decodeValue(val: string | null | undefined): MicrophoneSelection | null {
  if (!val) return null
  const idx = val.indexOf(":")
  if (idx <= 0) {
    // Legacy value without prefix — treat as a browser device id.
    return { kind: "browser", id: val }
  }
  const kind = val.slice(0, idx)
  if (kind !== "wendyos" && kind !== "browser") return null
  return { kind, id: val.slice(idx + 1) }
}

export function MicrophoneSelector({ onDeviceSelect }: MicrophoneSelectorProps) {
  const [browserDevices, setBrowserDevices] = React.useState<MediaDeviceInfo[]>([])
  const { devices: wendyosDevices, selectInput } = useWendyosMicrophones()
  const [selected, setSelected] = React.useState<MicrophoneSelection | null>(() => {
    if (typeof window === "undefined") return null
    return decodeValue(localStorage.getItem(STORAGE_KEY))
  })

  const emit = React.useCallback(
    (sel: MicrophoneSelection | null) => {
      setSelected(sel)
      onDeviceSelect(sel)
      if (typeof window !== "undefined") {
        if (sel) localStorage.setItem(STORAGE_KEY, encodeValue(sel))
        else localStorage.removeItem(STORAGE_KEY)
      }
      // When the user picks a wendyos (host-side) mic, ask the backend
      // to switch its local pipeline to that device. Browser mics are
      // handled by the Pipecat WS client in App.tsx instead. The hook
      // already setError()s on failure so ErrorAlerts surfaces it; this
      // catch is just to keep the unhandled-rejection warning quiet.
      if (sel?.kind === "wendyos") {
        void selectInput(sel.id).catch(() => {})
      }
    },
    [onDeviceSelect, selectInput],
  )

  const getDevices = React.useCallback(async () => {
    try {
      const allDevices = await navigator.mediaDevices.enumerateDevices()
      const audioInputs = allDevices.filter((device) => device.kind === "audioinput")
      setBrowserDevices(audioInputs)
    } catch (err) {
      console.error("Error enumerating devices:", err)
    }
  }, [])

  const requestPermission = React.useCallback(async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      stream.getTracks().forEach((track) => track.stop())
      await getDevices()
    } catch (err) {
      console.error("Permission denied:", err)
    }
  }, [getDevices])

  React.useEffect(() => {
    requestPermission()
    navigator.mediaDevices.addEventListener("devicechange", getDevices)
    return () => {
      navigator.mediaDevices.removeEventListener("devicechange", getDevices)
    }
  }, [getDevices, requestPermission])

  // Once we know the available devices, validate the cached selection or
  // fall back to the first available option (preferring wendyos > browser).
  React.useEffect(() => {
    if (selected) {
      const stillExists =
        selected.kind === "wendyos"
          ? wendyosDevices.some((d) => d.id === selected.id)
          : browserDevices.some((d) => d.deviceId === selected.id)
      if (stillExists) {
        onDeviceSelect(selected)
        return
      }
    }

    if (wendyosDevices.length > 0) {
      emit({ kind: "wendyos", id: wendyosDevices[0].id })
    } else if (browserDevices.length > 0) {
      emit({ kind: "browser", id: browserDevices[0].deviceId })
    }
    // Intentionally only re-run when the device lists change; `selected`
    // changes are driven by user action and already emit from `emit`.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wendyosDevices, browserDevices])

  const selectedLabel = React.useMemo(() => {
    if (!selected) return null
    if (selected.kind === "wendyos") {
      return wendyosDevices.find((d) => d.id === selected.id)?.label ?? null
    }
    return browserDevices.find((d) => d.deviceId === selected.id)?.label ?? null
  }, [selected, wendyosDevices, browserDevices])

  const hasAny = wendyosDevices.length + browserDevices.length > 0

  return (
    <div className="flex items-center gap-2">
      <Combobox
        value={selected ? encodeValue(selected) : ""}
        onValueChange={(val) => {
          const next = decodeValue(val as string)
          if (next) emit(next)
        }}
      >
        <ComboboxTrigger className="w-[250px] flex items-center justify-between rounded-md border border-emerald-500/30 bg-black/50 px-3 py-2 text-sm text-emerald-100 hover:bg-emerald-500/10 focus:outline-none focus:ring-1 focus:ring-emerald-500">
          {selectedLabel
            ? selectedLabel
            : selected
              ? `Microphone ${selected.id.slice(0, 5)}`
              : "Select Microphone..."}
        </ComboboxTrigger>
        <ComboboxContent>
          <ComboboxList>
            {wendyosDevices.length > 0 && (
              <ComboboxGroup>
                <ComboboxLabel>WendyOS devices</ComboboxLabel>
                {wendyosDevices.map((device) => (
                  <ComboboxItem
                    key={`wendyos:${device.id}`}
                    value={encodeValue({ kind: "wendyos", id: device.id })}
                  >
                    <Mic className="mr-2 h-4 w-4" />
                    {device.label}
                  </ComboboxItem>
                ))}
              </ComboboxGroup>
            )}
            <ComboboxGroup>
              <ComboboxLabel>Browser</ComboboxLabel>
              {browserDevices.map((device) => (
                <ComboboxItem
                  key={`browser:${device.deviceId || device.label}`}
                  value={encodeValue({ kind: "browser", id: device.deviceId })}
                >
                  <Mic className="mr-2 h-4 w-4" />
                  {device.label || `Microphone ${device.deviceId.slice(0, 5)}`}
                </ComboboxItem>
              ))}
            </ComboboxGroup>
          </ComboboxList>
          {!hasAny && <ComboboxEmpty>No microphone found.</ComboboxEmpty>}
        </ComboboxContent>
      </Combobox>
    </div>
  )
}
