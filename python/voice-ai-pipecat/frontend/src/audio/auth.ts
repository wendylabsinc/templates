/**
 * Bearer-token plumbing for write routes (`/api/settings`,
 * `/api/conversation/reset`, `/api/local-audio/select`).
 *
 * The backend gate (`require_auth`) is no-op when `WENDY_AUTH_TOKEN` is
 * unset, matching the trusted-LAN default. When it IS set, every write
 * route 401s without an `Authorization: Bearer <token>` header.
 *
 * Token sources, in priority order:
 *  1. `#token=<value>` URL fragment — read once on first import, then
 *     stripped from the URL and persisted to localStorage. Fragments
 *     are never sent to the server or logged in Referer headers, so
 *     the secret doesn't leak into HTTP access logs / external referers.
 *  2. `?token=<value>` URL query — same handling, but the token IS
 *     visible in server logs and Referer until replaceState runs.
 *     Supported for back-compat with existing bookmarks; prefer #.
 *  3. `localStorage.wendyAuthToken` — set by step 1/2, by an admin via
 *     DevTools, or by a UI you might add later.
 *
 * When none are present, requests go out without an Authorization
 * header (correct for the trusted-LAN default).
 */

const STORAGE_KEY = "wendyAuthToken"

function readFragmentToken(hash: string): string | null {
  // hash is "#token=...&foo=..." or empty.
  if (!hash || hash.length < 2) return null
  const params = new URLSearchParams(hash.slice(1))
  return params.get("token")
}

function bootstrapTokenFromUrl(): void {
  if (typeof window === "undefined") return
  let url: URL
  try {
    url = new URL(window.location.href)
  } catch (err) {
    console.warn("Wendy auth: could not parse window.location.href:", err)
    return
  }
  const fromFragment = readFragmentToken(url.hash)
  const fromQuery = url.searchParams.get("token")
  const token = fromFragment ?? fromQuery
  if (!token) return
  // Persist BEFORE stripping the URL so a thrown setItem doesn't leave
  // the user with no token at all. If localStorage is unavailable
  // (private mode, sandboxed iframe), keep the URL so a manual reload
  // still has a chance.
  try {
    window.localStorage.setItem(STORAGE_KEY, token)
  } catch (err) {
    console.warn(
      "Wendy auth: failed to persist token from URL — write routes will 401 until you set localStorage.wendyAuthToken manually:",
      err,
    )
    return
  }
  try {
    if (fromFragment !== null) {
      const params = new URLSearchParams(url.hash.slice(1))
      params.delete("token")
      const remaining = params.toString()
      url.hash = remaining ? `#${remaining}` : ""
    }
    if (fromQuery !== null) {
      url.searchParams.delete("token")
    }
    window.history.replaceState({}, "", url.toString())
  } catch (err) {
    console.warn("Wendy auth: failed to strip token from URL after persist:", err)
  }
}

bootstrapTokenFromUrl()

let storageReadFailureLogged = false

export function getAuthToken(): string {
  if (typeof window === "undefined") return ""
  try {
    return window.localStorage.getItem(STORAGE_KEY) ?? ""
  } catch (err) {
    // Without this log, write requests silently 401 with no clue that
    // localStorage is the culprit. Log once per session to avoid
    // flooding the console.
    if (!storageReadFailureLogged) {
      storageReadFailureLogged = true
      console.warn("Wendy auth: localStorage read failed; requests will go out unauthenticated:", err)
    }
    return ""
  }
}

/** Headers to merge into every write request. Returns `{}` when no
 *  token is configured so the backend's no-auth default still works. */
export function authHeaders(): Record<string, string> {
  const token = getAuthToken()
  return token ? { Authorization: `Bearer ${token}` } : {}
}
