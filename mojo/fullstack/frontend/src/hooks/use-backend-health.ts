import { useEffect, useState } from "react"

/**
 * Polls the backend health endpoint. Returns true if the backend is reachable.
 * Checks every 5 seconds.
 */
export function useBackendHealth() {
  const [healthy, setHealthy] = useState(true)

  useEffect(() => {
    let mounted = true

    async function check() {
      try {
        const res = await fetch("/api/system", { signal: AbortSignal.timeout(3000) })
        if (mounted) setHealthy(res.ok)
      } catch {
        if (mounted) setHealthy(false)
      }
    }

    check()
    const id = setInterval(check, 5000)
    return () => {
      mounted = false
      clearInterval(id)
    }
  }, [])

  return healthy
}
