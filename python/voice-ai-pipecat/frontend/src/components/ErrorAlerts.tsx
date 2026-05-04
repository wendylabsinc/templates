"use client"

import { AlertCircle } from "lucide-react"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"

export interface ErrorAlertsProps {
  /** Error from the browser microphone source (permission denied, device gone, ...). */
  micError: Error | null
  /** Error from the bot audio WebSocket (connection closed, server down, ...). */
  botError: Error | null
  /**
   * Error from the wendy-agent device RPC. Currently always null; wired up when
   * the agent client lands (see useWendyosMicrophones).
   */
  wendyosError: Error | null
}

interface ErrorEntry {
  key: string
  title: string
  message: string
}

function buildEntries(props: ErrorAlertsProps): ErrorEntry[] {
  const entries: ErrorEntry[] = []
  if (props.micError) {
    entries.push({
      key: "mic",
      title: "Microphone error",
      message: props.micError.message,
    })
  }
  if (props.botError) {
    entries.push({
      key: "bot",
      title: "Bot audio connection error",
      message: props.botError.message,
    })
  }
  if (props.wendyosError) {
    entries.push({
      key: "wendyos",
      title: "WendyOS device error",
      message: props.wendyosError.message,
    })
  }
  return entries
}

export function ErrorAlerts(props: ErrorAlertsProps) {
  const entries = buildEntries(props)
  if (entries.length === 0) return null

  return (
    <div className="pointer-events-auto flex w-full max-w-md flex-col gap-2">
      {entries.map((entry) => (
        <Alert key={entry.key} variant="destructive">
          <AlertCircle />
          <AlertTitle>{entry.title}</AlertTitle>
          <AlertDescription>{entry.message}</AlertDescription>
        </Alert>
      ))}
    </div>
  )
}
