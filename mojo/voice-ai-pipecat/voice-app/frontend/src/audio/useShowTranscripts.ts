import * as React from "react"

const STORAGE_KEY = "show-transcripts"

/**
 * Browser-local toggle for the on-screen transcript overlay. Persists
 * across reloads via localStorage so the user doesn't have to flip it
 * every session, but stays per-device — the backend doesn't need to
 * know about it.
 */
export function useShowTranscripts(): [boolean, (next: boolean) => void] {
  const [enabled, setEnabled] = React.useState<boolean>(() => {
    if (typeof window === "undefined") return false
    return window.localStorage.getItem(STORAGE_KEY) === "1"
  })

  const update = React.useCallback((next: boolean) => {
    setEnabled(next)
    if (typeof window !== "undefined") {
      window.localStorage.setItem(STORAGE_KEY, next ? "1" : "0")
    }
  }, [])

  return [enabled, update]
}
