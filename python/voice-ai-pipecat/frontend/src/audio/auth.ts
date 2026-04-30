/**
 * Bearer-token plumbing for write routes (`/api/settings`,
 * `/api/conversation/reset`, `/api/local-audio/select`).
 *
 * The backend gate (`require_auth`) is no-op when `WENDY_AUTH_TOKEN` is
 * unset, matching the trusted-LAN default. When it IS set, every write
 * route 401s without an `Authorization: Bearer <token>` header.
 *
 * Token sources, in priority order:
 *  1. `?token=<value>` URL parameter — read once on first import, then
 *     stripped from the URL and persisted to localStorage so the user
 *     can bookmark a clean URL.
 *  2. `localStorage.wendyAuthToken` — set by step 1, by an admin via
 *     DevTools, or by a UI you might add later.
 *
 * When neither is present, requests go out without an Authorization
 * header (correct for the trusted-LAN default).
 */

const STORAGE_KEY = "wendyAuthToken"

function bootstrapTokenFromUrl(): void {
  if (typeof window === "undefined") return
  try {
    const url = new URL(window.location.href)
    const fromQuery = url.searchParams.get("token")
    if (fromQuery) {
      window.localStorage.setItem(STORAGE_KEY, fromQuery)
      url.searchParams.delete("token")
      window.history.replaceState({}, "", url.toString())
    }
  } catch {
    // URL parsing or storage write failed (private mode, file://, etc.) —
    // not fatal, just means the token has to be set manually.
  }
}

bootstrapTokenFromUrl()

export function getAuthToken(): string {
  if (typeof window === "undefined") return ""
  try {
    return window.localStorage.getItem(STORAGE_KEY) ?? ""
  } catch {
    return ""
  }
}

/** Headers to merge into every write request. Returns `{}` when no
 *  token is configured so the backend's no-auth default still works. */
export function authHeaders(): Record<string, string> {
  const token = getAuthToken()
  return token ? { Authorization: `Bearer ${token}` } : {}
}
