import { useSyncExternalStore } from "react"

export type ThemePreference = "light" | "dark" | "system"

export type Settings = {
  chatFullWidth: boolean
  theme: ThemePreference
}

const STORAGE_KEY = "llm-chat:settings"
const defaults: Settings = { chatFullWidth: false, theme: "system" }

function load(): Settings {
  if (typeof localStorage === "undefined") return defaults
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return defaults
    return { ...defaults, ...(JSON.parse(raw) as Partial<Settings>) }
  } catch {
    return defaults
  }
}

let state: Settings = load()
const listeners = new Set<() => void>()

function emit() {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state))
  } catch {
    // ignore quota / unavailable
  }
  for (const l of listeners) l()
}

function subscribe(listener: () => void) {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

function getSnapshot() {
  return state
}

export function useSettings() {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot)
}

export function setSetting<K extends keyof Settings>(key: K, value: Settings[K]) {
  state = { ...state, [key]: value }
  emit()
}
