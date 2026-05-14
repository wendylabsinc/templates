import * as React from "react"
import { useSettings, type ThemePreference } from "@/lib/settings"

function apply(theme: ThemePreference) {
  const root = document.documentElement
  const resolved =
    theme === "system"
      ? window.matchMedia("(prefers-color-scheme: dark)").matches
        ? "dark"
        : "light"
      : theme
  root.classList.toggle("dark", resolved === "dark")
  root.style.colorScheme = resolved
}

export function ThemeSync() {
  const { theme } = useSettings()

  React.useEffect(() => {
    apply(theme)
    if (theme !== "system") return
    const mq = window.matchMedia("(prefers-color-scheme: dark)")
    const onChange = () => apply("system")
    mq.addEventListener("change", onChange)
    return () => mq.removeEventListener("change", onChange)
  }, [theme])

  return null
}
