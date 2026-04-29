"use client"

import * as React from "react"
import { X, Loader2 } from "lucide-react"
import { Textarea } from "@/components/ui/textarea"
import { Button } from "@/components/ui/button"
import { useAppSettings, type AppSettings } from "@/audio"
import { useShowTranscripts } from "@/audio/useShowTranscripts"

interface SettingsDrawerProps {
  open: boolean
  onOpenChange: (open: boolean) => void
}

const LANGUAGE_LABELS: Record<string, string> = {
  auto: "Auto-detect",
  en: "English",
  es: "Spanish",
  fr: "French",
  de: "German",
  it: "Italian",
  pt: "Portuguese",
  nl: "Dutch",
  ru: "Russian",
  zh: "Chinese",
  ja: "Japanese",
  ko: "Korean",
  ar: "Arabic",
  hi: "Hindi",
}

const PRESET_LABELS: Record<string, string> = {
  concise: "Concise",
  conversational: "Conversational",
  playful: "Playful",
}

interface ToggleProps {
  id: string
  checked: boolean
  onChange: (next: boolean) => void
  label: string
  hint?: string
  disabled?: boolean
}

function Toggle({ id, checked, onChange, label, hint, disabled }: ToggleProps) {
  return (
    <label htmlFor={id} className="flex cursor-pointer items-start gap-3">
      <input
        id={id}
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
        className="mt-1 h-4 w-4 rounded border-emerald-500/40 bg-black/60 text-emerald-400 focus:ring-emerald-500/60"
      />
      <span className="flex flex-col gap-1">
        <span className="text-sm font-medium text-emerald-200">{label}</span>
        {hint && <span className="text-xs text-emerald-300/50">{hint}</span>}
      </span>
    </label>
  )
}

/**
 * Right-side slide-in panel for editing user settings. Keeps a local
 * `draft` state for everything; "Save" pushes the whole draft to
 * /api/settings (which restarts the local pipeline). UI-only toggles
 * (transcripts overlay) write to localStorage independently the moment
 * the checkbox flips, so they don't need to wait for "Save" to apply.
 */
export function SettingsDrawer({ open, onOpenChange }: SettingsDrawerProps) {
  const {
    settings,
    defaultSystemPrompt,
    availableTtsVoices,
    availableWakeWords,
    availableSttLanguages,
    promptPresets,
    loading,
    error,
    save,
    resetToDefault,
    resetConversation,
    availableLlmProviders,
    availableSttProviders,
    saveApiKeys,
    clearApiKeys,
    saveBraveKey,
  } = useAppSettings()
  // Per-provider draft API key entries. Held locally so saving the
  // settings panel doesn't push key text alongside everything else.
  const [keyDrafts, setKeyDrafts] = React.useState<Record<string, string>>({})
  const [braveDraft, setBraveDraft] = React.useState<string>("")
  const [showTranscripts, setShowTranscripts] = useShowTranscripts()
  const [draft, setDraft] = React.useState<AppSettings | null>(null)
  const [saving, setSaving] = React.useState(false)
  const [saveError, setSaveError] = React.useState<Error | null>(null)
  const [savedAt, setSavedAt] = React.useState<number | null>(null)

  React.useEffect(() => {
    if (open && settings) {
      setDraft(settings)
      setSaveError(null)
    }
  }, [open, settings])

  React.useEffect(() => {
    if (!open) return
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") onOpenChange(false)
    }
    window.addEventListener("keydown", handler)
    return () => window.removeEventListener("keydown", handler)
  }, [open, onOpenChange])

  const dirty = settings != null && draft != null && JSON.stringify(draft) !== JSON.stringify(settings)

  const handleSave = async () => {
    if (!draft) return
    setSaving(true)
    setSaveError(null)
    try {
      await save(draft)
      setSavedAt(Date.now())
    } catch (err) {
      setSaveError(err as Error)
    } finally {
      setSaving(false)
    }
  }

  const handleReset = async () => {
    setSaving(true)
    setSaveError(null)
    try {
      await resetToDefault()
      setDraft((prev) =>
        prev ? { ...prev, systemPrompt: defaultSystemPrompt } : prev,
      )
      setSavedAt(Date.now())
    } catch (err) {
      setSaveError(err as Error)
    } finally {
      setSaving(false)
    }
  }

  const updateDraft = (patch: Partial<AppSettings>) => {
    setDraft((prev) => (prev ? { ...prev, ...patch } : prev))
  }

  const toggleWakeModel = (model: string, checked: boolean) => {
    if (!draft) return
    const next = checked
      ? Array.from(new Set([...draft.wakeWordModels, model]))
      : draft.wakeWordModels.filter((m) => m !== model)
    // Don't allow empty list — keep at least one model.
    if (next.length === 0) return
    updateDraft({ wakeWordModels: next })
  }

  return (
    <>
      <div
        className={
          "fixed inset-0 z-40 bg-black/60 backdrop-blur-sm transition-opacity " +
          (open ? "pointer-events-auto opacity-100" : "pointer-events-none opacity-0")
        }
        onClick={() => onOpenChange(false)}
      />

      <aside
        className={
          "fixed right-0 top-0 z-50 flex h-full w-full max-w-md flex-col border-l border-emerald-500/20 bg-black/95 text-emerald-50 shadow-2xl transition-transform " +
          (open ? "translate-x-0" : "translate-x-full")
        }
        aria-hidden={!open}
      >
        <header className="flex items-center justify-between border-b border-emerald-500/20 px-6 py-4">
          <h2 className="text-lg font-semibold tracking-tight text-emerald-300">
            Settings
          </h2>
          <button
            type="button"
            onClick={() => onOpenChange(false)}
            aria-label="Close settings"
            className="rounded-md p-1 text-emerald-300/70 hover:bg-emerald-500/10 hover:text-emerald-200 focus:outline-none focus:ring-2 focus:ring-emerald-500/60"
          >
            <X className="h-5 w-5" />
          </button>
        </header>

        <div className="flex-1 space-y-6 overflow-y-auto px-6 py-5">
          {loading || !draft ? (
            <div className="flex items-center gap-2 text-emerald-300/60">
              <Loader2 className="h-4 w-4 animate-spin" />
              Loading settings…
            </div>
          ) : error ? (
            <p className="text-sm text-red-400">
              Couldn't load settings: {error.message}
            </p>
          ) : (
            <>
              <section className="flex flex-col gap-2">
                <label
                  htmlFor="tts-voice"
                  className="text-sm font-medium text-emerald-200"
                >
                  TTS voice
                </label>
                <p className="text-xs text-emerald-300/50">
                  All voices are pre-downloaded; switching is instant on the
                  next reply.
                </p>
                <select
                  id="tts-voice"
                  value={draft.ttsVoice}
                  onChange={(e) => updateDraft({ ttsVoice: e.target.value })}
                  className="rounded-md border border-emerald-500/30 bg-black/60 px-3 py-2 text-sm text-emerald-100 focus:outline-none focus:ring-2 focus:ring-emerald-500/60"
                >
                  {availableTtsVoices.map((v) => (
                    <option key={v} value={v}>
                      {v}
                    </option>
                  ))}
                </select>
              </section>

              <section className="flex flex-col gap-3">
                <h3 className="text-sm font-medium text-emerald-200">
                  Wake word
                </h3>
                <Toggle
                  id="wake-word-disabled"
                  checked={draft.wakeWordDisabled}
                  onChange={(v) => updateDraft({ wakeWordDisabled: v })}
                  label="Always listening (disable wake word)"
                  hint="Skip the wake-word stage entirely. Bot replies to anything spoken."
                />
                <div
                  className={
                    "ml-7 flex flex-col gap-2 " +
                    (draft.wakeWordDisabled ? "opacity-40" : "")
                  }
                >
                  <p className="text-xs text-emerald-300/50">
                    Active phrases (pick one or more):
                  </p>
                  <div className="grid grid-cols-2 gap-1">
                    {availableWakeWords.map((m) => (
                      <Toggle
                        key={m}
                        id={`wake-${m}`}
                        checked={draft.wakeWordModels.includes(m)}
                        onChange={(v) => toggleWakeModel(m, v)}
                        label={m.replace(/_/g, " ")}
                        disabled={draft.wakeWordDisabled}
                      />
                    ))}
                  </div>
                </div>
              </section>

              <section className="flex flex-col gap-3">
                <h3 className="text-sm font-medium text-emerald-200">
                  Conversation
                </h3>
                <Toggle
                  id="allow-interruptions"
                  checked={draft.allowInterruptions}
                  onChange={(v) => updateDraft({ allowInterruptions: v })}
                  label="Allow interruptions"
                  hint="Bot stops mid-sentence when you start speaking. Off works better for near-field mics like the PowerConf."
                />
                <Toggle
                  id="show-transcripts"
                  checked={showTranscripts}
                  onChange={setShowTranscripts}
                  label="Show transcripts on screen"
                  hint="Overlay your speech and the bot's reply on the visualizer. Saved locally to this browser."
                />
                <Toggle
                  id="google-search-enabled"
                  checked={draft.googleSearchEnabled}
                  onChange={(v) => updateDraft({ googleSearchEnabled: v })}
                  label="Google Search grounding"
                  hint="Lets Gemini fetch live data (weather, news, scores). Turn off for pure offline / privacy mode."
                />
              </section>

              <section className="flex flex-col gap-3">
                <h3 className="text-sm font-medium text-emerald-200">
                  Speech recognition
                </h3>
                <p className="text-xs text-emerald-300/50">
                  Whisper runs locally on CPU; Deepgram streams over
                  the cloud and is roughly 10× faster (200 ms vs 1–3 s
                  TTFB).
                </p>
                <div className="grid grid-cols-2 gap-1">
                  {Object.keys(availableSttProviders).map((p) => {
                    const needsKey = p !== "whisper"
                    const configured = !needsKey || draft.apiKeysConfigured[p]
                    const active = draft.sttProvider === p
                    return (
                      <button
                        key={p}
                        type="button"
                        onClick={() => {
                          const models = availableSttProviders[p] ?? []
                          updateDraft({
                            sttProvider: p,
                            sttModel: models[0] ?? draft.sttModel,
                          })
                        }}
                        className={
                          "flex items-center justify-between rounded-md border px-3 py-2 text-sm capitalize transition-colors " +
                          (active
                            ? "border-emerald-400/60 bg-emerald-500/15 text-emerald-100"
                            : "border-emerald-500/20 bg-black/40 text-emerald-300/70 hover:bg-emerald-500/10")
                        }
                      >
                        <span>{p}</span>
                        {needsKey && (
                          <span
                            className={
                              "text-[10px] " +
                              (configured
                                ? "text-emerald-400/80"
                                : "text-amber-400/80")
                            }
                          >
                            {configured ? "● key" : "○ no key"}
                          </span>
                        )}
                      </button>
                    )
                  })}
                </div>

                <label
                  htmlFor="stt-model"
                  className="text-xs text-emerald-200"
                >
                  Model
                </label>
                <select
                  id="stt-model"
                  value={draft.sttModel}
                  onChange={(e) => updateDraft({ sttModel: e.target.value })}
                  className="rounded-md border border-emerald-500/30 bg-black/60 px-3 py-2 text-sm text-emerald-100 focus:outline-none focus:ring-2 focus:ring-emerald-500/60"
                >
                  {(availableSttProviders[draft.sttProvider] ?? []).map((m) => (
                    <option key={m} value={m}>
                      {m}
                    </option>
                  ))}
                </select>

                <label
                  htmlFor="stt-language"
                  className="text-xs text-emerald-200"
                >
                  Language
                </label>
                <select
                  id="stt-language"
                  value={draft.sttLanguage}
                  onChange={(e) => updateDraft({ sttLanguage: e.target.value })}
                  className="rounded-md border border-emerald-500/30 bg-black/60 px-3 py-2 text-sm text-emerald-100 focus:outline-none focus:ring-2 focus:ring-emerald-500/60"
                >
                  {availableSttLanguages.map((code) => (
                    <option key={code} value={code}>
                      {LANGUAGE_LABELS[code] ?? code}
                    </option>
                  ))}
                </select>
              </section>

              <section className="flex flex-col gap-3">
                <h3 className="text-sm font-medium text-emerald-200">
                  LLM provider
                </h3>
                <p className="text-xs text-emerald-300/50">
                  Pick which company's model the bot routes through.
                  Google supports native Search grounding; the other
                  providers use a Brave-backed `web_search` function
                  instead (set the Brave API key below).
                </p>
                <div className="grid grid-cols-2 gap-1">
                  {Object.keys(availableLlmProviders).map((p) => {
                    const configured = draft.apiKeysConfigured[p]
                    const active = draft.llmProvider === p
                    return (
                      <button
                        key={p}
                        type="button"
                        onClick={() => {
                          const models = availableLlmProviders[p] ?? []
                          updateDraft({
                            llmProvider: p,
                            llmModel: models[0] ?? draft.llmModel,
                          })
                        }}
                        className={
                          "flex items-center justify-between rounded-md border px-3 py-2 text-sm capitalize transition-colors " +
                          (active
                            ? "border-emerald-400/60 bg-emerald-500/15 text-emerald-100"
                            : "border-emerald-500/20 bg-black/40 text-emerald-300/70 hover:bg-emerald-500/10")
                        }
                      >
                        <span>{p}</span>
                        <span
                          className={
                            "text-[10px] " +
                            (configured
                              ? "text-emerald-400/80"
                              : "text-amber-400/80")
                          }
                        >
                          {configured ? "● key" : "○ no key"}
                        </span>
                      </button>
                    )
                  })}
                </div>

                <label
                  htmlFor="llm-model"
                  className="text-xs text-emerald-200"
                >
                  Model
                </label>
                <select
                  id="llm-model"
                  value={draft.llmModel}
                  onChange={(e) => updateDraft({ llmModel: e.target.value })}
                  className="rounded-md border border-emerald-500/30 bg-black/60 px-3 py-2 text-sm text-emerald-100 focus:outline-none focus:ring-2 focus:ring-emerald-500/60"
                >
                  {(availableLlmProviders[draft.llmProvider] ?? []).map((m) => (
                    <option key={m} value={m}>
                      {m}
                    </option>
                  ))}
                  {/* Custom model in case the user wants something not in the list */}
                  {!(availableLlmProviders[draft.llmProvider] ?? []).includes(
                    draft.llmModel,
                  ) &&
                    draft.llmModel && (
                      <option value={draft.llmModel}>{draft.llmModel}</option>
                    )}
                </select>

                <details className="rounded-md border border-emerald-500/20 bg-black/40">
                  <summary className="cursor-pointer px-3 py-2 text-xs text-emerald-200 hover:bg-emerald-500/10">
                    API keys
                  </summary>
                  <div className="flex flex-col gap-2 px-3 py-3">
                    {Object.keys(availableLlmProviders).map((p) => {
                      const configured = draft.apiKeysConfigured[p]
                      return (
                        <div key={p} className="flex flex-col gap-1">
                          <label
                            htmlFor={`api-key-${p}`}
                            className="flex items-center justify-between text-xs capitalize text-emerald-300/70"
                          >
                            <span>{p}</span>
                            <span
                              className={
                                "text-[10px] " +
                                (configured
                                  ? "text-emerald-400/80"
                                  : "text-amber-400/80")
                              }
                            >
                              {configured ? "configured" : "not configured"}
                            </span>
                          </label>
                          <div className="flex items-center gap-1">
                            <input
                              id={`api-key-${p}`}
                              type="password"
                              autoComplete="off"
                              spellCheck={false}
                              placeholder={
                                configured ? "•••••• (saved)" : "Paste API key"
                              }
                              value={keyDrafts[p] ?? ""}
                              onChange={(e) =>
                                setKeyDrafts((prev) => ({
                                  ...prev,
                                  [p]: e.target.value,
                                }))
                              }
                              className="flex-1 rounded-md border border-emerald-500/30 bg-black/60 px-2 py-1 text-xs text-emerald-100 placeholder:text-emerald-300/30 focus:outline-none focus:ring-2 focus:ring-emerald-500/60"
                            />
                            <Button
                              type="button"
                              variant="ghost"
                              onClick={async () => {
                                const k = keyDrafts[p]
                                if (!k) return
                                try {
                                  await saveApiKeys({ [p]: k })
                                  setKeyDrafts((prev) => ({ ...prev, [p]: "" }))
                                  setSavedAt(Date.now())
                                } catch (err) {
                                  setSaveError(err as Error)
                                }
                              }}
                              disabled={!keyDrafts[p]}
                              className="h-7 px-2 text-xs text-emerald-300/80 hover:bg-emerald-500/10 hover:text-emerald-100"
                            >
                              Save
                            </Button>
                            {configured && (
                              <Button
                                type="button"
                                variant="ghost"
                                onClick={async () => {
                                  try {
                                    await clearApiKeys([p])
                                    setSavedAt(Date.now())
                                  } catch (err) {
                                    setSaveError(err as Error)
                                  }
                                }}
                                className="h-7 px-2 text-xs text-red-300/70 hover:bg-red-500/10 hover:text-red-200"
                              >
                                Clear
                              </Button>
                            )}
                          </div>
                        </div>
                      )
                    })}

                    <div className="mt-3 flex flex-col gap-1 border-t border-emerald-500/10 pt-3">
                      <label className="flex items-center justify-between text-xs text-emerald-300/70">
                        <span>Brave Search (web_search tool)</span>
                        <span
                          className={
                            "text-[10px] " +
                            (draft.searchApiKeyConfigured
                              ? "text-emerald-400/80"
                              : "text-amber-400/80")
                          }
                        >
                          {draft.searchApiKeyConfigured
                            ? "configured"
                            : "not configured"}
                        </span>
                      </label>
                      <div className="flex items-center gap-1">
                        <input
                          type="password"
                          autoComplete="off"
                          spellCheck={false}
                          placeholder={
                            draft.searchApiKeyConfigured
                              ? "•••••• (saved)"
                              : "Paste Brave API key"
                          }
                          value={braveDraft}
                          onChange={(e) => setBraveDraft(e.target.value)}
                          className="flex-1 rounded-md border border-emerald-500/30 bg-black/60 px-2 py-1 text-xs text-emerald-100 placeholder:text-emerald-300/30 focus:outline-none focus:ring-2 focus:ring-emerald-500/60"
                        />
                        <Button
                          type="button"
                          variant="ghost"
                          onClick={async () => {
                            if (!braveDraft) return
                            try {
                              await saveBraveKey(braveDraft)
                              setBraveDraft("")
                              setSavedAt(Date.now())
                            } catch (err) {
                              setSaveError(err as Error)
                            }
                          }}
                          disabled={!braveDraft}
                          className="h-7 px-2 text-xs text-emerald-300/80 hover:bg-emerald-500/10 hover:text-emerald-100"
                        >
                          Save
                        </Button>
                        {draft.searchApiKeyConfigured && (
                          <Button
                            type="button"
                            variant="ghost"
                            onClick={async () => {
                              try {
                                await saveBraveKey("")
                                setSavedAt(Date.now())
                              } catch (err) {
                                setSaveError(err as Error)
                              }
                            }}
                            className="h-7 px-2 text-xs text-red-300/70 hover:bg-red-500/10 hover:text-red-200"
                          >
                            Clear
                          </Button>
                        )}
                      </div>
                      <p className="text-[10px] text-emerald-300/40">
                        Get a free key at https://api.search.brave.com.
                        Only used when an OpenAI/Anthropic/Groq provider
                        is selected with search enabled.
                      </p>
                    </div>
                  </div>
                </details>
              </section>

              <section className="flex flex-col gap-3">
                <h3 className="text-sm font-medium text-emerald-200">
                  Voice activity detection
                </h3>
                <p className="text-xs text-emerald-300/50">
                  Tune how aggressively the bot decides you're talking.
                  Bump these up in noisy rooms; drop them if it misses
                  quiet speech.
                </p>
                <label
                  htmlFor="vad-confidence"
                  className="flex items-center justify-between text-xs text-emerald-200"
                >
                  <span>Confidence</span>
                  <span className="font-mono text-emerald-300/70">
                    {draft.vadConfidence.toFixed(2)}
                  </span>
                </label>
                <input
                  id="vad-confidence"
                  type="range"
                  min={0}
                  max={1}
                  step={0.05}
                  value={draft.vadConfidence}
                  onChange={(e) =>
                    updateDraft({ vadConfidence: parseFloat(e.target.value) })
                  }
                  className="accent-emerald-500"
                />
                <label
                  htmlFor="vad-min-volume"
                  className="flex items-center justify-between text-xs text-emerald-200"
                >
                  <span>Min volume</span>
                  <span className="font-mono text-emerald-300/70">
                    {draft.vadMinVolume.toFixed(2)}
                  </span>
                </label>
                <input
                  id="vad-min-volume"
                  type="range"
                  min={0}
                  max={1}
                  step={0.05}
                  value={draft.vadMinVolume}
                  onChange={(e) =>
                    updateDraft({ vadMinVolume: parseFloat(e.target.value) })
                  }
                  className="accent-emerald-500"
                />
                <label
                  htmlFor="vad-stop-secs"
                  className="flex items-center justify-between text-xs text-emerald-200"
                >
                  <span>End-of-speech silence</span>
                  <span className="font-mono text-emerald-300/70">
                    {draft.vadStopSecs.toFixed(2)} s
                  </span>
                </label>
                <p className="text-[10px] text-emerald-300/40 -mt-2">
                  How long to wait after you stop talking before sending.
                  Bump this up if the bot interrupts mid-sentence while
                  you're thinking.
                </p>
                <input
                  id="vad-stop-secs"
                  type="range"
                  min={0.1}
                  max={3.0}
                  step={0.1}
                  value={draft.vadStopSecs}
                  onChange={(e) =>
                    updateDraft({ vadStopSecs: parseFloat(e.target.value) })
                  }
                  className="accent-emerald-500"
                />
                <label
                  htmlFor="vad-start-secs"
                  className="flex items-center justify-between text-xs text-emerald-200"
                >
                  <span>Speech-onset hold</span>
                  <span className="font-mono text-emerald-300/70">
                    {draft.vadStartSecs.toFixed(2)} s
                  </span>
                </label>
                <input
                  id="vad-start-secs"
                  type="range"
                  min={0.1}
                  max={1.0}
                  step={0.05}
                  value={draft.vadStartSecs}
                  onChange={(e) =>
                    updateDraft({ vadStartSecs: parseFloat(e.target.value) })
                  }
                  className="accent-emerald-500"
                />
              </section>

              <section className="flex flex-col gap-3">
                <h3 className="text-sm font-medium text-emerald-200">
                  Conversation memory
                </h3>
                <Toggle
                  id="persist-conversation"
                  checked={draft.persistConversation}
                  onChange={(v) => updateDraft({ persistConversation: v })}
                  label="Remember conversation across restarts"
                  hint="Saves user/bot turns to disk so the bot keeps context after a redeploy or hot-plug recovery."
                />
                <div>
                  <Button
                    type="button"
                    variant="ghost"
                    onClick={async () => {
                      try {
                        await resetConversation()
                        setSavedAt(Date.now())
                      } catch (err) {
                        setSaveError(err as Error)
                      }
                    }}
                    className="text-emerald-300/70 hover:bg-emerald-500/10 hover:text-emerald-200"
                  >
                    Reset conversation
                  </Button>
                </div>
              </section>

              <section className="flex flex-col gap-3">
                <h3 className="text-sm font-medium text-emerald-200">
                  Startup greeting
                </h3>
                <p className="text-xs text-emerald-300/50">
                  Spoken when the pipeline becomes ready, so the user
                  knows when to start talking.
                </p>
                <Toggle
                  id="greeting-enabled"
                  checked={draft.greetingEnabled}
                  onChange={(v) => updateDraft({ greetingEnabled: v })}
                  label="Speak greeting on startup"
                />
                <input
                  id="greeting-message"
                  type="text"
                  value={draft.greetingMessage}
                  onChange={(e) =>
                    updateDraft({ greetingMessage: e.target.value })
                  }
                  placeholder="Hi, I'm your voice assistant…"
                  disabled={!draft.greetingEnabled}
                  className="rounded-md border border-emerald-500/30 bg-black/60 px-3 py-2 text-sm text-emerald-100 placeholder:text-emerald-300/30 focus:outline-none focus:ring-2 focus:ring-emerald-500/60 disabled:opacity-40"
                />
              </section>

              <section className="flex flex-col gap-2">
                <div className="flex items-center justify-between">
                  <label
                    htmlFor="system-prompt"
                    className="text-sm font-medium text-emerald-200"
                  >
                    System prompt
                  </label>
                  <div className="flex items-center gap-1">
                    {Object.entries(promptPresets).map(([key, text]) => (
                      <button
                        key={key}
                        type="button"
                        onClick={() => updateDraft({ systemPrompt: text })}
                        className="rounded-md border border-emerald-500/30 bg-black/60 px-2 py-1 text-xs text-emerald-300 hover:bg-emerald-500/10 focus:outline-none focus:ring-2 focus:ring-emerald-500/60"
                      >
                        {PRESET_LABELS[key] ?? key}
                      </button>
                    ))}
                  </div>
                </div>
                <p className="text-xs text-emerald-300/50">
                  Defines the assistant's persona, response style, and tool
                  guidance. The preset buttons load a starting point you
                  can edit.
                </p>
                <Textarea
                  id="system-prompt"
                  value={draft.systemPrompt}
                  onChange={(e) =>
                    updateDraft({ systemPrompt: e.target.value })
                  }
                  rows={14}
                  spellCheck={false}
                  className="min-h-[260px] border-emerald-500/30 bg-black/60 font-mono text-xs text-emerald-100 focus-visible:ring-emerald-500/60"
                />
              </section>
            </>
          )}
        </div>

        <footer className="flex flex-col gap-3 border-t border-emerald-500/20 px-6 py-4">
          {saveError && (
            <p className="text-xs text-red-400">
              Save failed: {saveError.message}
            </p>
          )}
          {savedAt && !saveError && !saving && (
            <p className="text-xs text-emerald-300/60">
              Saved {new Date(savedAt).toLocaleTimeString()}.
            </p>
          )}
          <div className="flex items-center justify-between gap-2">
            <Button
              type="button"
              variant="ghost"
              onClick={handleReset}
              disabled={saving || loading}
              className="text-emerald-300/70 hover:bg-emerald-500/10 hover:text-emerald-200"
            >
              Restore prompt default
            </Button>
            <div className="flex items-center gap-2">
              <Button
                type="button"
                variant="ghost"
                onClick={() => onOpenChange(false)}
                disabled={saving}
                className="text-emerald-300/70 hover:bg-emerald-500/10 hover:text-emerald-200"
              >
                Close
              </Button>
              <Button
                type="button"
                onClick={handleSave}
                disabled={!dirty || saving || loading}
                className="bg-emerald-500/80 text-black hover:bg-emerald-400"
              >
                {saving ? (
                  <>
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                    Saving…
                  </>
                ) : (
                  "Save"
                )}
              </Button>
            </div>
          </div>
        </footer>
      </aside>
    </>
  )
}
