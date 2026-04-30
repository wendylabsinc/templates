import * as React from "react"
import { authHeaders } from "./auth"

export interface AppSettings {
  systemPrompt: string
  ttsVoice: string
  allowInterruptions: boolean
  wakeWordModels: string[]
  wakeWordDisabled: boolean
  continuousConversation: boolean
  continuousWindowSecs: number
  sttLanguage: string
  vadConfidence: number
  vadMinVolume: number
  vadStopSecs: number
  vadStartSecs: number
  googleSearchEnabled: boolean
  greetingEnabled: boolean
  greetingMessage: string
  persistConversation: boolean
  llmProvider: string
  llmModel: string
  sttProvider: string
  sttModel: string
  apiKeysConfigured: Record<string, boolean>
  searchApiKeyConfigured: boolean
}

export interface AppSettingsState {
  settings: AppSettings | null
  defaultSystemPrompt: string
  availableTtsVoices: string[]
  availableWakeWords: string[]
  availableSttLanguages: string[]
  availableLlmProviders: Record<string, string[]>
  availableSttProviders: Record<string, string[]>
  promptPresets: Record<string, string>
  loading: boolean
  error: Error | null
  /** Update one or more API keys without exposing them on read. */
  saveApiKeys: (keys: Record<string, string>) => Promise<void>
  /** Clear stored keys for the listed providers (falls back to env vars). */
  clearApiKeys: (providers: string[]) => Promise<void>
  /** Set or clear the Brave search key. */
  saveBraveKey: (key: string) => Promise<void>
  /** POST settings to the backend. Returns when the change has been
   *  persisted to disk (and the active local pipeline restarted, if one
   *  was running). Rejects with the server's error detail on save
   *  failure so the drawer can show a real error instead of a stale
   *  "Saved" toast. */
  save: (next: Partial<AppSettings>) => Promise<void>
  /** Restore the prompt to the template's built-in default. */
  resetToDefault: () => Promise<void>
  /** Drop the persisted conversation history and restart the local
   *  pipeline so the bot forgets everything. */
  resetConversation: () => Promise<void>
}

interface BackendSettings {
  system_prompt: string
  tts_voice: string
  allow_interruptions: boolean
  wake_word_models: string[]
  wake_word_disabled: boolean
  continuous_conversation: boolean
  continuous_window_secs: number
  stt_language: string
  vad_confidence: number
  vad_min_volume: number
  vad_stop_secs: number
  vad_start_secs: number
  google_search_enabled: boolean
  greeting_enabled: boolean
  greeting_message: string
  persist_conversation: boolean
  llm_provider: string
  llm_model: string
  stt_provider: string
  stt_model: string
  api_keys_configured: Record<string, boolean>
  search_api_key_configured: boolean
}

interface BackendResponse {
  settings: BackendSettings
  default_system_prompt: string
  available_tts_voices: string[]
  available_wake_words: string[]
  available_stt_languages: string[]
  prompt_presets: Record<string, string>
  available_llm_providers: Record<string, string[]>
  available_stt_providers: Record<string, string[]>
}

function fromBackend(s: BackendSettings): AppSettings {
  return {
    systemPrompt: s.system_prompt,
    ttsVoice: s.tts_voice,
    allowInterruptions: s.allow_interruptions,
    wakeWordModels: s.wake_word_models,
    wakeWordDisabled: s.wake_word_disabled,
    continuousConversation: s.continuous_conversation,
    continuousWindowSecs: s.continuous_window_secs,
    sttLanguage: s.stt_language,
    vadConfidence: s.vad_confidence,
    vadMinVolume: s.vad_min_volume,
    vadStopSecs: s.vad_stop_secs,
    vadStartSecs: s.vad_start_secs,
    googleSearchEnabled: s.google_search_enabled,
    greetingEnabled: s.greeting_enabled,
    greetingMessage: s.greeting_message,
    persistConversation: s.persist_conversation,
    llmProvider: s.llm_provider,
    llmModel: s.llm_model,
    sttProvider: s.stt_provider,
    sttModel: s.stt_model,
    apiKeysConfigured: s.api_keys_configured ?? {},
    searchApiKeyConfigured: s.search_api_key_configured ?? false,
  }
}

function toBackendPayload(next: Partial<AppSettings>): Record<string, unknown> {
  const out: Record<string, unknown> = {}
  if (next.systemPrompt !== undefined) out.system_prompt = next.systemPrompt
  if (next.ttsVoice !== undefined) out.tts_voice = next.ttsVoice
  if (next.allowInterruptions !== undefined) out.allow_interruptions = next.allowInterruptions
  if (next.wakeWordModels !== undefined) out.wake_word_models = next.wakeWordModels
  if (next.wakeWordDisabled !== undefined) out.wake_word_disabled = next.wakeWordDisabled
  if (next.continuousConversation !== undefined)
    out.continuous_conversation = next.continuousConversation
  if (next.continuousWindowSecs !== undefined)
    out.continuous_window_secs = next.continuousWindowSecs
  if (next.sttLanguage !== undefined) out.stt_language = next.sttLanguage
  if (next.vadConfidence !== undefined) out.vad_confidence = next.vadConfidence
  if (next.vadMinVolume !== undefined) out.vad_min_volume = next.vadMinVolume
  if (next.vadStopSecs !== undefined) out.vad_stop_secs = next.vadStopSecs
  if (next.vadStartSecs !== undefined) out.vad_start_secs = next.vadStartSecs
  if (next.googleSearchEnabled !== undefined) out.google_search_enabled = next.googleSearchEnabled
  if (next.greetingEnabled !== undefined) out.greeting_enabled = next.greetingEnabled
  if (next.greetingMessage !== undefined) out.greeting_message = next.greetingMessage
  if (next.persistConversation !== undefined) out.persist_conversation = next.persistConversation
  if (next.llmProvider !== undefined) out.llm_provider = next.llmProvider
  if (next.llmModel !== undefined) out.llm_model = next.llmModel
  if (next.sttProvider !== undefined) out.stt_provider = next.sttProvider
  if (next.sttModel !== undefined) out.stt_model = next.sttModel
  return out
}

export function useAppSettings(): AppSettingsState {
  const [settings, setSettings] = React.useState<AppSettings | null>(null)
  const [defaultSystemPrompt, setDefaultSystemPrompt] = React.useState("")
  const [availableTtsVoices, setAvailableTtsVoices] = React.useState<string[]>([])
  const [availableWakeWords, setAvailableWakeWords] = React.useState<string[]>([])
  const [availableSttLanguages, setAvailableSttLanguages] = React.useState<string[]>([])
  const [availableLlmProviders, setAvailableLlmProviders] = React.useState<
    Record<string, string[]>
  >({})
  const [availableSttProviders, setAvailableSttProviders] = React.useState<
    Record<string, string[]>
  >({})
  const [promptPresets, setPromptPresets] = React.useState<Record<string, string>>({})
  const [loading, setLoading] = React.useState(true)
  const [error, setError] = React.useState<Error | null>(null)

  const refresh = React.useCallback(async () => {
    try {
      const res = await fetch("/api/settings")
      if (!res.ok) throw new Error(`/api/settings ${res.status}`)
      const data = (await res.json()) as BackendResponse
      setSettings(fromBackend(data.settings))
      setDefaultSystemPrompt(data.default_system_prompt)
      setAvailableTtsVoices(data.available_tts_voices)
      setAvailableWakeWords(data.available_wake_words)
      setAvailableSttLanguages(data.available_stt_languages)
      setAvailableLlmProviders(data.available_llm_providers ?? {})
      setAvailableSttProviders(data.available_stt_providers ?? {})
      setPromptPresets(data.prompt_presets)
      setError(null)
    } catch (err) {
      setError(err as Error)
    } finally {
      setLoading(false)
    }
  }, [])

  React.useEffect(() => {
    void refresh()
  }, [refresh])

  // Centralize the POST-and-handle-response path so every mutator
  // (save, resetToDefault, saveApiKeys, ...) surfaces server errors
  // identically and clears any stale load-time error on success.
  const postSettings = React.useCallback(
    async (path: string, payload: Record<string, unknown>) => {
      const res = await fetch(path, {
        method: "POST",
        headers: { "content-type": "application/json", ...authHeaders() },
        body: JSON.stringify(payload),
      })
      if (!res.ok) {
        // Backend returns FastAPI HTTPException JSON ({detail: "..."})
        // for known failure modes (disk full, validation). Prefer that
        // over the generic status code so users see "Could not persist
        // settings: Read-only file system" instead of "500".
        let detail = `${path} ${res.status}`
        try {
          const body = (await res.json()) as { detail?: unknown }
          if (typeof body?.detail === "string" && body.detail) detail = body.detail
        } catch {
          // non-JSON body — keep status-based detail
        }
        throw new Error(detail)
      }
      const data = (await res.json()) as BackendResponse
      setSettings(fromBackend(data.settings))
      setError(null)
    },
    [],
  )

  const save = React.useCallback(
    async (next: Partial<AppSettings>) => {
      await postSettings("/api/settings", toBackendPayload(next))
    },
    [postSettings],
  )

  const resetToDefault = React.useCallback(async () => {
    await postSettings("/api/settings", { reset_to_default: true })
  }, [postSettings])

  const resetConversation = React.useCallback(async () => {
    const res = await fetch("/api/conversation/reset", {
      method: "POST",
      headers: authHeaders(),
    })
    if (!res.ok) {
      let detail = `/api/conversation/reset ${res.status}`
      try {
        const body = (await res.json()) as { detail?: unknown }
        if (typeof body?.detail === "string" && body.detail) detail = body.detail
      } catch {
        // non-JSON body
      }
      throw new Error(detail)
    }
  }, [])

  const saveApiKeys = React.useCallback(
    async (keys: Record<string, string>) => {
      await postSettings("/api/settings", { api_keys: keys })
    },
    [postSettings],
  )

  const clearApiKeys = React.useCallback(
    async (providers: string[]) => {
      await postSettings("/api/settings", { api_keys_clear: providers })
    },
    [postSettings],
  )

  const saveBraveKey = React.useCallback(
    async (key: string) => {
      await postSettings("/api/settings", { brave_api_key: key })
    },
    [postSettings],
  )

  return {
    settings,
    defaultSystemPrompt,
    availableTtsVoices,
    availableWakeWords,
    availableSttLanguages,
    availableLlmProviders,
    availableSttProviders,
    promptPresets,
    loading,
    error,
    save,
    resetToDefault,
    resetConversation,
    saveApiKeys,
    clearApiKeys,
    saveBraveKey,
  }
}
