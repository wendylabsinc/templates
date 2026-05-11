"use client"

import { useState, useEffect, useRef } from "react"

interface Detection {
  label: string
  confidence: number
  timestamp: string
  vlm_answers?: { question: string; answer: string }[]
  periodic?: boolean
}

interface Profile {
  id: string
  name: string
  description: string
  active: boolean
  available: boolean
}

interface VlmQuestion {
  id: string
  question: string
}

interface NetIface {
  iface: string
  type: "wifi" | "ethernet"
  ip?: string
  signal_dbm?: number
  quality_pct?: number
  speed_mbps?: number
}

interface HwStats {
  cpu_pct: number
  ram_used_gb: number
  ram_total_gb: number
  ram_pct: number
  gpu_pct: number | null
  temps: Record<string, number>
  video_fps: number
  inference_fps: number
  network: NetIface[]
}

const modelLabel = (path: string) => path.split("/").pop()?.replace(/\.pt$/, "") ?? path

export default function DetectionPage() {
  const [detections, setDetections] = useState<Detection[]>([])
  const [showControls, setShowControls] = useState(false)
  const logRef = useRef<HTMLDivElement>(null)
  const [apiStatus, setApiStatus] = useState<"connected" | "reconnecting">("reconnecting")
  const [apiError, setApiError] = useState<string | null>(null)
  const [apiRetryMs, setApiRetryMs] = useState<number | null>(null)
  const [videoStatus, setVideoStatus] = useState<"connected" | "reconnecting">("reconnecting")
  const [videoError, setVideoError] = useState<string | null>(null)
  const [videoRetryMs, setVideoRetryMs] = useState<number | null>(null)
  const [videoSrc, setVideoSrc] = useState(`/api/video-feed?ts=${Date.now()}`)
  const isMountedRef = useRef(true)
  const videoRetryRef = useRef(500)
  const videoTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const [availableModels, setAvailableModels] = useState<string[]>([])
  const [activeModel, setActiveModel] = useState<string>("")
  const [switching, setSwitching] = useState(false)
  const [trtStatus, setTrtStatus] = useState<Record<string, string>>({})
  const [hw, setHw] = useState<HwStats | null>(null)
  const [downloadUrl, setDownloadUrl] = useState("")
  const [downloadName, setDownloadName] = useState("")
  const [downloadStatus, setDownloadStatus] = useState<string | null>(null)
  const [showDownload, setShowDownload] = useState(false)
  const [inferResults, setInferResults] = useState<{ filename: string; detections: { label: string; confidence: number; bbox: number[] }[]; image: string; error?: string }[]>([])
  const [inferring, setInferring] = useState(false)
  const [classStats, setClassStats] = useState<Record<string, { count: number; last_seen: string | null; seconds_visible: number | null }>>({})
  const [paused, setPaused] = useState(false)
  const [targetFps, setTargetFps] = useState(15)
  const [fpsInput, setFpsInput] = useState("15")
  const [activeCam, setActiveCam] = useState(0)
  const [availableCams, setAvailableCams] = useState<{ index: number; name: string; resolution: string }[]>([])
  const [switchingCam, setSwitchingCam] = useState(false)
  const [zoom, setZoom] = useState(1.0)
  const [pan, setPan] = useState(0.0)
  const [tilt, setTilt] = useState(0.0)
  const [vlmConnected, setVlmConnected] = useState(false)
  const [vlmLastError, setVlmLastError] = useState<string | null>(null)
  const [vlmQuestion, setVlmQuestion] = useState("")
  const [vlmAsking, setVlmAsking] = useState(false)
  const [vlmOneTimeResult, setVlmOneTimeResult] = useState<{ question: string; answer: string } | null>(null)
  const [vlmQuestions, setVlmQuestions] = useState<VlmQuestion[]>([])
  const [showVlmPanel, setShowVlmPanel] = useState(false)
  const [vlmIntervalInput, setVlmIntervalInput] = useState("0")
  const [vlmIntervalActive, setVlmIntervalActive] = useState(0)
  const [vlmConfInput, setVlmConfInput] = useState("70")
  const [vlmConfActive, setVlmConfActive] = useState(70)
  const [vlmClasses, setVlmClasses] = useState<string[]>([])
  const [vlmClassInput, setVlmClassInput] = useState("")
  const [searchQuery, setSearchQuery] = useState("")
  const [searchStatus, setSearchStatus] = useState<"idle" | "searching" | "found" | "not_found" | "cancelled" | "error">("idle")
  const [searchQuestion, setSearchQuestion] = useState("")
  const [searchLog, setSearchLog] = useState<{ timestamp: string; step: string; detail: string; zoom: number; pan: number; tilt: number }[]>([])
  const [showSearchPanel, setShowSearchPanel] = useState(false)
  const [profiles, setProfiles] = useState<Profile[]>([])
  const [switchingProfile, setSwitchingProfile] = useState(false)

  const pollIntervalMs = 500
  const pollMaxBackoffMs = 8000
  const videoMaxBackoffMs = 8000

  useEffect(() => {
    isMountedRef.current = true
    return () => {
      isMountedRef.current = false
      if (videoTimeoutRef.current) clearTimeout(videoTimeoutRef.current)
    }
  }, [])

  // Poll detections
  useEffect(() => {
    let isCancelled = false
    let timeoutId: ReturnType<typeof setTimeout> | null = null
    let retryDelay = pollIntervalMs

    const fetchDetections = async () => {
      try {
        const res = await fetch("/api/detections", { cache: "no-store" })
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        const data: Detection[] = await res.json()
        if (isCancelled) return
        setDetections(data)
        setApiStatus("connected")
        setApiError(null)
        setApiRetryMs(null)
        retryDelay = pollIntervalMs
        timeoutId = setTimeout(fetchDetections, pollIntervalMs)
      } catch (error) {
        if (isCancelled) return
        setApiStatus("reconnecting")
        setApiError(error instanceof Error ? error.message : String(error))
        setApiRetryMs(retryDelay)
        timeoutId = setTimeout(fetchDetections, retryDelay)
        retryDelay = Math.min(retryDelay * 2, pollMaxBackoffMs)
      }
    }

    fetchDetections()
    return () => {
      isCancelled = true
      if (timeoutId) clearTimeout(timeoutId)
    }
  }, [])

  // Auto-scroll log
  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight
  }, [detections])

  // Poll models
  useEffect(() => {
    const fetchModels = () =>
      fetch("/api/models").then(r => r.json()).then(d => {
        setAvailableModels(d.models ?? [])
        setActiveModel(d.active ?? "")
        setTrtStatus(d.trt_status ?? {})
      }).catch(() => {})
    fetchModels()
    const id = setInterval(fetchModels, 5000)
    return () => clearInterval(id)
  }, [])

  // Poll HW stats
  useEffect(() => {
    const fetchHw = () => fetch("/api/hw").then(r => r.json()).then(setHw).catch(() => {})
    fetchHw()
    const id = setInterval(fetchHw, 2000)
    return () => clearInterval(id)
  }, [])

  // Poll class stats
  useEffect(() => {
    const fetchClasses = () => fetch("/api/classes").then(r => r.json()).then(setClassStats).catch(() => {})
    fetchClasses()
    const id = setInterval(fetchClasses, 1000)
    return () => clearInterval(id)
  }, [])

  // Load camera config on mount
  useEffect(() => {
    fetch("/api/camera").then(r => r.json()).then(d => {
      setActiveCam(d.active ?? 0)
      setAvailableCams(d.available ?? [])
    }).catch(() => {})
  }, [])

  // Load zoom config on mount
  useEffect(() => {
    fetch("/api/zoom").then(r => r.json()).then(d => {
      setZoom(d.zoom ?? 1.0)
      setPan(d.pan ?? 0.0)
      setTilt(d.tilt ?? 0.0)
    }).catch(() => {})
  }, [])

  // Load FPS config on mount
  useEffect(() => {
    fetch("/api/fps").then(r => r.json()).then(d => {
      setTargetFps(d.target_fps ?? 15)
      setFpsInput(String(d.target_fps ?? 15))
    }).catch(() => {})
  }, [])

  // Load VLM config on mount + poll connection status
  useEffect(() => {
    fetch("/api/vlm/questions").then(r => r.json()).then(setVlmQuestions).catch(() => {})
    const fetchVlmConfig = () =>
      fetch("/api/vlm/config").then(r => r.json()).then(d => {
        setVlmIntervalActive(d.interval ?? 0)
        setVlmIntervalInput(String(d.interval ?? 0))
        const confPct = Math.round((d.conf_threshold ?? 0.7) * 100)
        setVlmConfActive(confPct)
        setVlmConfInput(String(confPct))
        setVlmConnected(d.connected ?? false)
        setVlmLastError(d.last_error ?? null)
        setVlmClasses(d.classes ?? [])
      }).catch(() => {})
    fetchVlmConfig()
    const id = setInterval(fetchVlmConfig, 5000)
    return () => clearInterval(id)
  }, [])

  const scheduleVideoReconnect = (message: string) => {
    if (!isMountedRef.current) return
    if (videoTimeoutRef.current) clearTimeout(videoTimeoutRef.current)
    setVideoStatus("reconnecting")
    setVideoError(message)
    const delay = videoRetryRef.current
    setVideoRetryMs(delay)
    videoTimeoutRef.current = setTimeout(() => {
      if (!isMountedRef.current) return
      setVideoSrc(`/api/video-feed?ts=${Date.now()}`)
    }, delay)
    videoRetryRef.current = Math.min(delay * 2, videoMaxBackoffMs)
  }

  const handleVideoLoad = () => {
    if (!isMountedRef.current) return
    setVideoStatus("connected")
    setVideoError(null)
    setVideoRetryMs(null)
    videoRetryRef.current = 500
  }

  const handlePauseToggle = async () => {
    const res = await fetch("/api/pause", { method: "POST" })
    const data = await res.json()
    setPaused(data.paused)
  }

  const handleZoomChange = async (newZoom?: number, newPan?: number, newTilt?: number) => {
    const body: Record<string, number> = {}
    if (newZoom !== undefined) body.zoom = newZoom
    if (newPan !== undefined) body.pan = newPan
    if (newTilt !== undefined) body.tilt = newTilt
    const res = await fetch("/api/zoom", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })
    const data = await res.json()
    setZoom(data.zoom ?? zoom)
    setPan(data.pan ?? pan)
    setTilt(data.tilt ?? tilt)
  }

  const handleCameraSwitch = async (index: number) => {
    if (index === activeCam || switchingCam) return
    setSwitchingCam(true)
    try {
      const res = await fetch("/api/camera", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ index }),
      })
      const data = await res.json()
      setActiveCam(data.active ?? index)
      setVideoSrc(`/api/video-feed?ts=${Date.now()}`)
    } finally {
      setSwitchingCam(false)
    }
  }

  const handleSetFps = async () => {
    const val = Math.max(0, parseFloat(fpsInput) || 0)
    const res = await fetch("/api/fps", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target_fps: val }),
    })
    const data = await res.json()
    setTargetFps(data.target_fps ?? val)
    setFpsInput(String(data.target_fps ?? val))
  }

  const handleSearchStart = async () => {
    if (!searchQuery.trim()) return
    await fetch("/api/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: searchQuery.trim() }),
    })
    setSearchStatus("searching")
    setSearchQuestion(searchQuery.trim())
  }

  const handleSearchStop = async () => {
    await fetch("/api/search", { method: "DELETE" })
  }

  // Poll search status while searching
  useEffect(() => {
    if (searchStatus !== "searching") return
    const poll = setInterval(() => {
      fetch("/api/search").then(r => r.json()).then(d => {
        setSearchStatus(d.status ?? "idle")
        setSearchQuestion(d.question ?? "")
        setSearchLog(d.log ?? [])
        if (d.status !== "searching") {
          // Update zoom/pan/tilt to match where the search ended
          fetch("/api/zoom").then(r => r.json()).then(z => {
            setZoom(z.zoom ?? 1.0)
            setPan(z.pan ?? 0.0)
            setTilt(z.tilt ?? 0.0)
          })
        }
      }).catch(() => {})
    }, 1000)
    return () => clearInterval(poll)
  }, [searchStatus])

  const handleVlmAddClass = async () => {
    const cls = vlmClassInput.trim().toLowerCase()
    if (!cls || vlmClasses.includes(cls)) return
    const updated = [...vlmClasses, cls]
    await fetch("/api/vlm/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ classes: updated }),
    })
    setVlmClasses(updated)
    setVlmClassInput("")
  }

  const handleVlmRemoveClass = async (cls: string) => {
    const updated = vlmClasses.filter(c => c !== cls)
    await fetch("/api/vlm/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ classes: updated }),
    })
    setVlmClasses(updated)
  }

  const handleVlmAskNow = async () => {
    if (!vlmQuestion.trim() || vlmAsking) return
    setVlmAsking(true)
    setVlmOneTimeResult(null)
    try {
      const res = await fetch("/api/vlm/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: vlmQuestion.trim() }),
      })
      setVlmOneTimeResult(await res.json())
    } catch {
      setVlmOneTimeResult({ question: vlmQuestion, answer: "Error contacting VLM" })
    } finally {
      setVlmAsking(false)
    }
  }

  const handleVlmAddRecurring = async () => {
    if (!vlmQuestion.trim()) return
    await fetch("/api/vlm/questions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: vlmQuestion.trim() }),
    })
    setVlmQuestion("")
    fetch("/api/vlm/questions").then(r => r.json()).then(setVlmQuestions).catch(() => {})
  }

  const handleVlmRemoveQuestion = async (id: string) => {
    await fetch(`/api/vlm/questions/${id}`, { method: "DELETE" })
    fetch("/api/vlm/questions").then(r => r.json()).then(setVlmQuestions).catch(() => {})
  }

  const handleSetVlmInterval = async () => {
    const val = parseFloat(vlmIntervalInput) || 0
    const res = await fetch("/api/vlm/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ interval: val }),
    })
    const data = await res.json()
    setVlmIntervalActive(data.interval ?? 0)
    setVlmIntervalInput(String(data.interval ?? 0))
  }

  const handleSetVlmConf = async () => {
    const pct = Math.max(0, Math.min(100, parseFloat(vlmConfInput) || 0))
    const res = await fetch("/api/vlm/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ conf_threshold: pct / 100 }),
    })
    const data = await res.json()
    const activePct = Math.round((data.conf_threshold ?? pct / 100) * 100)
    setVlmConfActive(activePct)
    setVlmConfInput(String(activePct))
  }

  const handleImageUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files
    if (!files || files.length === 0) return
    setInferring(true)
    setInferResults([])
    const form = new FormData()
    for (const f of files) form.append("files", f)
    try {
      const res = await fetch("/api/infer", { method: "POST", body: form })
      setInferResults(await res.json())
    } catch {
      // ignore
    } finally {
      setInferring(false)
      e.target.value = ""
    }
  }

  const handleDownload = async () => {
    if (!downloadUrl || !downloadName) return
    setDownloadStatus("starting...")
    const params = new URLSearchParams({ url: downloadUrl, name: downloadName })
    const es = new EventSource(`/api/models/download?${params}`)
    es.onmessage = (e) => {
      const data = JSON.parse(e.data)
      if (data.status === "downloading") {
        setDownloadStatus(`downloading... ${data.mb} MB`)
      } else if (data.status === "done") {
        setDownloadStatus("done")
        es.close()
        setDownloadUrl("")
        setDownloadName("")
        fetch("/api/models").then(r => r.json()).then(d => setAvailableModels(d.models ?? []))
        setTimeout(() => { setDownloadStatus(null); setShowDownload(false) }, 2000)
      } else if (data.status === "error") {
        setDownloadStatus(`error: ${data.message}`)
        es.close()
      }
    }
    es.onerror = () => { setDownloadStatus("connection error"); es.close() }
  }

  // Profiles intentionally hidden from the UI for the current demo build —
  // the backend still serves them and they remain switchable via /api/profiles/switch.
  const HIDDEN_PROFILE_IDS = new Set(["water", "gauge"])
  const visibleProfiles = (ps: Profile[]) => ps.filter(p => !HIDDEN_PROFILE_IDS.has(p.id))
  // Hide the raw "Model:" picker — the demo is profile-driven, and switching
  // a profile already swaps the model server-side. The state is still kept
  // in sync (activeModel is used as the detections heading label), but the
  // selector + download UI is not rendered.
  const SHOW_MODEL_SELECTOR = false

  // Fetch profiles
  useEffect(() => {
    fetch("/api/profiles").then(r => r.json()).then((ps: Profile[]) => setProfiles(visibleProfiles(ps))).catch(() => {})
  }, [])

  const handleProfileSwitch = async (id: string) => {
    if (switchingProfile) return
    setSwitchingProfile(true)
    try {
      const res = await fetch("/api/profiles/switch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id }),
      })
      if (res.ok) {
        setDetections([])
        // Refresh profiles and model list
        const [profilesRes, modelsRes, vlmConfigRes] = await Promise.all([
          fetch("/api/profiles"),
          fetch("/api/models"),
          fetch("/api/vlm/config"),
        ])
        if (profilesRes.ok) setProfiles(visibleProfiles(await profilesRes.json()))
        if (modelsRes.ok) {
          const data = await modelsRes.json()
          setAvailableModels(data.models || [])
          setActiveModel(data.active || "")
          setTrtStatus(data.trt_status || {})
        }
        if (vlmConfigRes.ok) {
          const data = await vlmConfigRes.json()
          setVlmIntervalActive(data.interval ?? 0)
          setVlmIntervalInput(String(data.interval ?? 0))
          setVlmConfActive(Math.round((data.conf_threshold ?? 0.7) * 100))
          setVlmConfInput(String(Math.round((data.conf_threshold ?? 0.7) * 100)))
        }
        // Refresh VLM questions
        const vlmRes = await fetch("/api/vlm/questions")
        if (vlmRes.ok) setVlmQuestions(await vlmRes.json())
      }
    } finally {
      setSwitchingProfile(false)
    }
  }

  const handleModelSwitch = async (path: string) => {
    if (path === activeModel || switching) return
    setSwitching(true)
    try {
      const res = await fetch(`/api/models/switch?path=${encodeURIComponent(path)}`, { method: "POST" })
      if (res.ok) { setActiveModel(path); setDetections([]) }
    } finally {
      setSwitching(false)
    }
  }

  return (
    <div className="relative h-screen w-screen overflow-hidden bg-black">

      {/* Settings toggle */}
      <button
        onClick={() => setShowControls(!showControls)}
        className="absolute left-4 top-4 z-30 rounded-md bg-black/70 px-3 py-2 text-white/60 hover:text-white/90 shadow text-xs font-mono"
      >
        {showControls ? "Hide Controls" : "Controls"}
      </button>

      {/* Top-left: profile selector + model selector + status + pause */}
      {showControls && <div className="absolute left-4 top-14 z-20 flex flex-col gap-2 text-xs font-mono">
        {profiles.length > 0 && (
          <div className="rounded-md bg-black/70 px-3 py-2 text-white/80 shadow">
            <div className="text-white/50 mb-1.5">Use Case</div>
            <div className="flex flex-col gap-1">
              {profiles.map((p) => (
                <button
                  key={p.id}
                  onClick={() => handleProfileSwitch(p.id)}
                  disabled={p.active || !p.available || switchingProfile}
                  className={`flex items-center gap-2 rounded px-2 py-1.5 text-left transition-colors ${
                    p.active
                      ? "bg-blue-500/80 text-white"
                      : p.available
                        ? "bg-white/10 text-white/70 hover:bg-white/20 hover:text-white/90"
                        : "bg-white/5 text-white/30 cursor-not-allowed"
                  } disabled:opacity-60`}
                >
                  <span>{p.id === "fire" ? "🔥" : p.id === "water" ? "💧" : p.id === "gauge" ? "🔧" : "👁"}</span>
                  <div className="flex flex-col">
                    <span className="font-semibold text-xs">{p.name}</span>
                    <span className="text-[10px] opacity-70">{p.available ? p.description : "Model not installed"}</span>
                  </div>
                  {p.active && !switchingProfile && <span className="ml-auto">✓</span>}
                  {p.active && switchingProfile && <span className="ml-auto animate-spin">⏳</span>}
                </button>
              ))}
            </div>
          </div>
        )}

        {SHOW_MODEL_SELECTOR && availableModels.length > 0 && (
          <div className="rounded-md bg-black/70 px-3 py-2 text-white/80 shadow">
            <div className="flex items-center gap-2">
              <span className="text-white/60">Model:</span>
              {switching ? (
                <span className="text-yellow-300">switching...</span>
              ) : (
                <select
                  className="bg-transparent text-white/90 outline-none cursor-pointer"
                  value={activeModel}
                  onChange={(e) => handleModelSwitch(e.target.value)}
                >
                  {availableModels.map((m) => {
                    const trt = trtStatus[m]
                    const suffix = trt === "exporting" ? " ⏳" : trt === "done" ? " ⚡" : ""
                    return <option key={m} value={m} className="bg-black">{modelLabel(m)}{suffix}</option>
                  })}
                </select>
              )}
              <button className="text-white/40 hover:text-white/80 ml-1" onClick={() => setShowDownload(!showDownload)}>+</button>
            </div>
            {showDownload && (
              <div className="mt-2 flex flex-col gap-1">
                <input className="bg-white/10 text-white/90 rounded px-2 py-1 text-xs outline-none w-48" placeholder="Model URL (.pt file)" value={downloadUrl} onChange={(e) => setDownloadUrl(e.target.value)} />
                <input className="bg-white/10 text-white/90 rounded px-2 py-1 text-xs outline-none w-48" placeholder="Name (e.g. fire-v2)" value={downloadName} onChange={(e) => setDownloadName(e.target.value)} />
                <button className="bg-white/20 hover:bg-white/30 rounded px-2 py-1 text-xs text-white/90 disabled:opacity-50" onClick={handleDownload} disabled={!downloadUrl || !downloadName || !!downloadStatus}>
                  {downloadStatus ?? "Download"}
                </button>
              </div>
            )}
          </div>
        )}

        {availableCams.length > 0 && (
          <div className="rounded-md bg-black/70 px-3 py-2 text-white/80 shadow">
            <div className="flex items-center gap-2">
              <span className="text-white/60">Camera:</span>
              {switchingCam ? (
                <span className="text-yellow-300">switching...</span>
              ) : (
                <select
                  className="bg-transparent text-white/90 outline-none cursor-pointer"
                  value={activeCam}
                  onChange={(e) => handleCameraSwitch(Number(e.target.value))}
                >
                  {availableCams.map((c) => (
                    <option key={c.index} value={c.index} className="bg-black">
                      {c.name} ({c.resolution})
                    </option>
                  ))}
                </select>
              )}
            </div>
          </div>
        )}

        <div className="rounded-md bg-black/70 px-3 py-2 text-white/80 shadow">
          <div className="flex items-center gap-2">
            <span className={`h-2 w-2 rounded-full ${apiStatus === "connected" ? "bg-green-400" : "bg-yellow-400"}`} />
            <span>Backend: {apiStatus === "connected" ? "connected" : "disconnected"}</span>
          </div>
          {apiStatus !== "connected" && (
            <div className="mt-1 text-white/60">
              Reconnecting{apiRetryMs ? ` in ${Math.ceil(apiRetryMs / 1000)}s` : ""}...
              {apiError && <div className="text-red-300 break-all">{apiError}</div>}
            </div>
          )}
        </div>

        <div className="rounded-md bg-black/70 px-3 py-2 text-white/80 shadow">
          <div className="flex items-center gap-2">
            <span className={`h-2 w-2 rounded-full ${videoStatus === "connected" ? "bg-green-400" : "bg-yellow-400"}`} />
            <span>Video: {videoStatus === "connected" ? "connected" : "disconnected"}</span>
          </div>
          {videoStatus !== "connected" && (
            <div className="mt-1 text-white/60">
              Reconnecting{videoRetryMs ? ` in ${Math.ceil(videoRetryMs / 1000)}s` : ""}...
              {videoError && <div className="text-red-300 break-all">{videoError}</div>}
            </div>
          )}
        </div>

        <button
          onClick={handlePauseToggle}
          className={`rounded-md px-3 py-2 text-sm font-semibold shadow transition-colors ${paused ? "bg-yellow-500/80 hover:bg-yellow-400/80 text-black" : "bg-black/70 hover:bg-black/90 text-white/80"}`}
        >
          {paused ? "▶ Resume" : "⏸ Pause"}
        </button>

        <div className="rounded-md bg-black/70 px-3 py-2 text-white/80 shadow">
          <div className="flex items-center gap-1.5">
            <span className="text-white/50 shrink-0">FPS</span>
            <input
              type="number"
              min="0"
              max="60"
              step="1"
              className="w-14 bg-white/10 text-white/90 rounded px-2 py-0.5 text-xs outline-none text-center"
              value={fpsInput}
              onChange={(e) => setFpsInput(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && handleSetFps()}
            />
            <button className="bg-white/15 hover:bg-white/25 rounded px-2 py-0.5 text-white/80" onClick={handleSetFps}>Set</button>
          </div>
          <div className="mt-1 text-xs text-white/30">target: {targetFps} fps</div>
        </div>

        <div className="rounded-md bg-black/70 px-3 py-2 text-white/80 shadow">
          <div className="flex items-center gap-1.5 mb-1.5">
            <span className="text-white/50 shrink-0">Zoom</span>
            <input
              type="range"
              min="1"
              max="5"
              step="0.1"
              className="flex-1 h-1 accent-cyan-400"
              value={zoom}
              onChange={(e) => handleZoomChange(parseFloat(e.target.value))}
            />
            <span className="text-white/60 w-8 text-right text-xs">{zoom.toFixed(1)}x</span>
          </div>
          {zoom > 1 && (
            <>
              <div className="flex items-center gap-1.5 mb-1">
                <span className="text-white/50 shrink-0 w-6 text-xs">Pan</span>
                <input
                  type="range"
                  min="-1"
                  max="1"
                  step="0.05"
                  className="flex-1 h-1 accent-cyan-400"
                  value={pan}
                  onChange={(e) => handleZoomChange(undefined, parseFloat(e.target.value))}
                />
              </div>
              <div className="flex items-center gap-1.5 mb-1">
                <span className="text-white/50 shrink-0 w-6 text-xs">Tilt</span>
                <input
                  type="range"
                  min="-1"
                  max="1"
                  step="0.05"
                  className="flex-1 h-1 accent-cyan-400"
                  value={tilt}
                  onChange={(e) => handleZoomChange(undefined, undefined, parseFloat(e.target.value))}
                />
              </div>
              <button
                className="w-full bg-white/10 hover:bg-white/20 rounded px-2 py-0.5 text-xs text-white/60"
                onClick={() => handleZoomChange(1.0, 0.0, 0.0)}
              >
                Reset
              </button>
            </>
          )}
        </div>

      </div>}

      {/* Full-screen video feed */}
      <img
        src={videoSrc}
        alt="Detection Feed"
        className="absolute inset-0 h-full w-full object-contain"
        onLoad={handleVideoLoad}
        onError={() => scheduleVideoReconnect("Video feed disconnected. Attempting to reconnect...")}
      />

      {/* HW stats panel */}
      {showControls && hw && (
        <div className="absolute left-4 bottom-[22%] z-20 rounded-md bg-black/70 px-3 py-2 text-xs font-mono text-white/80 shadow">
          <div className="flex flex-col gap-1">
            <div className="flex items-center gap-2">
              <span className="text-white/50 w-8">CPU</span>
              <div className="w-24 bg-white/10 rounded-full h-1.5"><div className="bg-blue-400 h-1.5 rounded-full" style={{ width: `${hw.cpu_pct}%` }} /></div>
              <span>{hw.cpu_pct.toFixed(0)}%</span>
            </div>
            <div className="flex items-center gap-2">
              <span className="text-white/50 w-8">RAM</span>
              <div className="w-24 bg-white/10 rounded-full h-1.5"><div className="bg-purple-400 h-1.5 rounded-full" style={{ width: `${hw.ram_pct}%` }} /></div>
              <span>{hw.ram_used_gb}/{hw.ram_total_gb}GB</span>
            </div>
            {hw.gpu_pct !== null && (
              <div className="flex items-center gap-2">
                <span className="text-white/50 w-8">GPU</span>
                <div className="w-24 bg-white/10 rounded-full h-1.5"><div className="bg-green-400 h-1.5 rounded-full" style={{ width: `${hw.gpu_pct}%` }} /></div>
                <span>{hw.gpu_pct.toFixed(0)}%</span>
              </div>
            )}
            {Object.entries(hw.temps)
              .filter(([name]) => ["cpu-thermal", "gpu-thermal", "tj-thermal"].includes(name))
              .map(([name, temp]) => {
                const label = name === "cpu-thermal" ? "CPU°" : name === "gpu-thermal" ? "GPU°" : "SoC°"
                return (
                  <div key={name} className="flex items-center gap-2">
                    <span className="text-white/50 w-8">{label}</span>
                    <span className={temp > 75 ? "text-red-400" : temp > 60 ? "text-yellow-400" : "text-white/80"}>{temp}°C</span>
                  </div>
                )
              })}
            <div className="border-t border-white/10 mt-1 pt-1 flex flex-col gap-1">
              <div className="flex items-center gap-2"><span className="text-white/50 w-8">FPS</span><span>{hw.video_fps}</span></div>
              <div className="flex items-center gap-2"><span className="text-white/50 w-8">INF</span><span>{hw.inference_fps}</span></div>
            </div>
            {hw.network && hw.network.length > 0 && (
              <div className="border-t border-white/10 mt-1 pt-1 flex flex-col gap-1">
                {hw.network.map((n) => (
                  <div key={n.iface}>
                    <div className="flex items-center gap-2">
                      <span className="text-white/50 w-8 shrink-0">{n.type === "wifi" ? "WiFi" : "ETH"}</span>
                      {n.type === "wifi" && n.quality_pct !== undefined ? (
                        <>
                          <div className="w-24 bg-white/10 rounded-full h-1.5">
                            <div className={`h-1.5 rounded-full ${n.quality_pct >= 60 ? "bg-green-400" : n.quality_pct >= 30 ? "bg-yellow-400" : "bg-red-400"}`} style={{ width: `${n.quality_pct}%` }} />
                          </div>
                          <span className={n.quality_pct >= 60 ? "text-green-400" : n.quality_pct >= 30 ? "text-yellow-400" : "text-red-400"}>{n.signal_dbm} dBm</span>
                        </>
                      ) : n.type === "wifi" ? (
                        <span className="text-white/50">no signal data</span>
                      ) : (
                        <span className="text-green-400">{n.speed_mbps ? `${n.speed_mbps} Mbps` : "connected"}</span>
                      )}
                    </div>
                    {n.ip && <div className="pl-10 text-white/40 text-xs">{n.ip}</div>}
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}

      {/* Top-right: search + test images + VLM panel */}
      {showControls && <div className="absolute right-4 top-4 z-20 flex flex-col gap-2 text-xs font-mono">
        <div className="rounded-md bg-black/70 px-3 py-2 text-white/80 shadow">
          <div className="flex items-center justify-between mb-1.5">
            <span className="text-orange-400/80 font-semibold">Visual Search</span>
            <button className="text-white/40 hover:text-white/80" onClick={() => setShowSearchPanel(!showSearchPanel)}>
              {showSearchPanel ? "▲" : "▼"}
            </button>
          </div>
          {showSearchPanel && (
            <div className="flex flex-col gap-1.5">
              <textarea
                className="bg-white/10 text-white/90 rounded px-2 py-1 text-xs outline-none resize-none w-52"
                rows={2}
                placeholder="What to search for..."
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); handleSearchStart() } }}
                disabled={searchStatus === "searching"}
              />
              <div className="flex gap-1">
                {searchStatus === "searching" ? (
                  <button className="flex-1 bg-red-700/60 hover:bg-red-600/60 rounded px-2 py-1 text-white/90" onClick={handleSearchStop}>
                    Stop
                  </button>
                ) : (
                  <button className="flex-1 bg-orange-700/60 hover:bg-orange-600/60 rounded px-2 py-1 text-white/90 disabled:opacity-40" onClick={handleSearchStart} disabled={!searchQuery.trim()}>
                    Search
                  </button>
                )}
              </div>

              {searchStatus !== "idle" && (
                <div className="mt-1">
                  <div className="flex items-center gap-1.5 mb-1">
                    <span className={`h-2 w-2 rounded-full ${
                      searchStatus === "searching" ? "bg-yellow-400 animate-pulse" :
                      searchStatus === "found" ? "bg-green-400" :
                      searchStatus === "not_found" ? "bg-red-400" :
                      "bg-white/40"
                    }`} />
                    <span className={
                      searchStatus === "found" ? "text-green-400" :
                      searchStatus === "not_found" ? "text-red-400" :
                      searchStatus === "searching" ? "text-yellow-400" :
                      "text-white/60"
                    }>
                      {searchStatus === "searching" ? "Searching..." :
                       searchStatus === "found" ? "Found!" :
                       searchStatus === "not_found" ? "Not found" :
                       searchStatus === "cancelled" ? "Cancelled" :
                       searchStatus === "error" ? "Error" : searchStatus}
                    </span>
                  </div>
                  {searchLog.length > 0 && (
                    <div className="max-h-32 overflow-y-auto bg-black/40 rounded p-1.5 flex flex-col gap-0.5">
                      {searchLog.map((entry, i) => (
                        <div key={i} className="text-xs leading-tight">
                          <span className={
                            entry.step === "found" || entry.step === "result" ? "text-green-400" :
                            entry.step === "error" ? "text-red-400" :
                            entry.step === "vlm" ? "text-cyan-300/80" :
                            entry.step === "scan" ? "text-yellow-400/60" :
                            "text-white/50"
                          }>
                            {entry.step === "vlm" ? `VLM: ${entry.detail.substring(0, 60)}${entry.detail.length > 60 ? "..." : ""}` :
                             entry.step === "scan" ? `${entry.detail}` :
                             entry.step === "confirm" ? `Confirm: ${entry.detail.substring(0, 60)}${entry.detail.length > 60 ? "..." : ""}` :
                             entry.detail}
                          </span>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>
          )}
        </div>

        <div className="rounded-md bg-black/70 px-3 py-2 text-white/80 shadow">
          <label className="cursor-pointer flex items-center gap-2">
            <span>{inferring ? "Running inference..." : "Test images"}</span>
            <input type="file" accept="image/*" multiple className="hidden" disabled={inferring} onChange={handleImageUpload} />
            {!inferring && <span className="bg-white/20 hover:bg-white/30 rounded px-2 py-0.5">Upload</span>}
          </label>
        </div>

        <div className="rounded-md bg-black/70 px-3 py-2 text-white/80 shadow">
          <div className="flex items-center justify-between mb-1.5">
            <div className="flex items-center gap-2">
              <span className={`h-2 w-2 rounded-full ${vlmConnected ? "bg-green-400" : "bg-red-400"}`} />
              <span className="text-cyan-400/80 font-semibold">VLM</span>
              <span className={`text-xs ${vlmConnected ? "text-green-400/60" : "text-red-400/60"}`}>
                {vlmConnected ? "connected" : "offline"}
              </span>
            </div>
            <button className="text-white/40 hover:text-white/80" onClick={() => setShowVlmPanel(!showVlmPanel)}>
              {showVlmPanel ? "▲" : "▼"}
            </button>
          </div>
          {!vlmConnected && vlmLastError && (
            <div className="text-xs text-red-400/70 mb-1 max-w-52 truncate" title={vlmLastError}>
              {vlmLastError.includes("Connection refused") ? "Connection refused (port 8090)" :
               vlmLastError.includes("timed out") ? "Request timed out" :
               "Service unavailable"}
            </div>
          )}
          {showVlmPanel && (
            <div className="flex flex-col gap-1.5">
              <textarea
                className="bg-white/10 text-white/90 rounded px-2 py-1 text-xs outline-none resize-none w-52"
                rows={2}
                placeholder="Ask a question about the scene..."
                value={vlmQuestion}
                onChange={(e) => setVlmQuestion(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); handleVlmAskNow() } }}
              />
              <div className="flex gap-1">
                <button className="flex-1 bg-cyan-700/60 hover:bg-cyan-600/60 rounded px-2 py-1 text-white/90 disabled:opacity-40" onClick={handleVlmAskNow} disabled={!vlmQuestion.trim() || vlmAsking}>
                  {vlmAsking ? "asking..." : "Ask now"}
                </button>
                <button className="flex-1 bg-white/15 hover:bg-white/25 rounded px-2 py-1 text-white/80 disabled:opacity-40" onClick={handleVlmAddRecurring} disabled={!vlmQuestion.trim()}>
                  + Recurring
                </button>
              </div>

              {vlmOneTimeResult && (
                <div className="bg-cyan-900/40 rounded p-2 mt-1">
                  <p className="text-white/50 mb-0.5 truncate">Q: {vlmOneTimeResult.question}</p>
                  <p className="text-cyan-200 leading-snug">{vlmOneTimeResult.answer}</p>
                  <button className="text-white/30 hover:text-white/60 text-xs mt-1" onClick={() => setVlmOneTimeResult(null)}>dismiss</button>
                </div>
              )}

              {vlmQuestions.length > 0 && (
                <div className="border-t border-white/10 pt-1.5 mt-0.5 flex flex-col gap-1">
                  <span className="text-white/40 text-xs">Recurring ({vlmQuestions.length})</span>
                  {vlmQuestions.map((q) => (
                    <div key={q.id} className="flex items-start gap-1">
                      <span className="flex-1 text-white/70 leading-snug break-words">{q.question}</span>
                      <button className="text-white/30 hover:text-red-400 shrink-0 ml-1" onClick={() => handleVlmRemoveQuestion(q.id)}>×</button>
                    </div>
                  ))}
                </div>
              )}

              <div className="border-t border-white/10 pt-1.5 mt-0.5">
                <div className="flex items-center gap-1.5">
                  <span className="text-white/50 shrink-0">Trigger ≥</span>
                  <input type="number" min="0" max="100" step="5" className="w-14 bg-white/10 text-white/90 rounded px-2 py-0.5 text-xs outline-none text-center" value={vlmConfInput} onChange={(e) => setVlmConfInput(e.target.value)} onKeyDown={(e) => e.key === "Enter" && handleSetVlmConf()} />
                  <span className="text-white/50 shrink-0">%</span>
                  <button className="ml-auto bg-white/15 hover:bg-white/25 rounded px-2 py-0.5 text-white/80" onClick={handleSetVlmConf}>Set</button>
                </div>
                <div className="mt-1 text-xs text-white/30">active: {vlmConfActive}% confidence</div>
              </div>

              <div className="border-t border-white/10 pt-1.5 mt-0.5">
                <div className="flex items-center gap-1.5">
                  <span className="text-white/50 shrink-0">Classes</span>
                  <input
                    type="text"
                    className="flex-1 bg-white/10 text-white/90 rounded px-2 py-0.5 text-xs outline-none"
                    placeholder="e.g. fire"
                    value={vlmClassInput}
                    onChange={(e) => setVlmClassInput(e.target.value)}
                    onKeyDown={(e) => e.key === "Enter" && handleVlmAddClass()}
                  />
                  <button className="bg-white/15 hover:bg-white/25 rounded px-2 py-0.5 text-white/80" onClick={handleVlmAddClass}>+</button>
                </div>
                {vlmClasses.length > 0 ? (
                  <div className="mt-1 flex flex-wrap gap-1">
                    {vlmClasses.map(cls => (
                      <span key={cls} className="bg-cyan-800/40 text-cyan-200 text-xs px-1.5 py-0.5 rounded flex items-center gap-1">
                        {cls}
                        <button className="text-white/40 hover:text-red-400" onClick={() => handleVlmRemoveClass(cls)}>x</button>
                      </span>
                    ))}
                  </div>
                ) : (
                  <div className="mt-1 text-xs text-white/30">all classes (no filter)</div>
                )}
              </div>

              <div className="border-t border-white/10 pt-1.5 mt-0.5">
                <div className="flex items-center gap-1.5">
                  <span className="text-white/50 shrink-0">Every</span>
                  <input type="number" min="0" step="1" className="w-14 bg-white/10 text-white/90 rounded px-2 py-0.5 text-xs outline-none text-center" value={vlmIntervalInput} onChange={(e) => setVlmIntervalInput(e.target.value)} onKeyDown={(e) => e.key === "Enter" && handleSetVlmInterval()} />
                  <span className="text-white/50 shrink-0">sec</span>
                  <button className="ml-auto bg-white/15 hover:bg-white/25 rounded px-2 py-0.5 text-white/80" onClick={handleSetVlmInterval}>Set</button>
                </div>
                <div className="mt-1 text-xs">
                  {vlmIntervalActive > 0 ? (
                    <span className="text-purple-400">⏱ running every {vlmIntervalActive}s</span>
                  ) : (
                    <span className="text-white/30">timer off</span>
                  )}
                </div>
              </div>
            </div>
          )}
        </div>
      </div>}

      {/* Inference results overlay */}
      {inferResults.length > 0 && (
        <div className="absolute inset-0 z-30 bg-black/90 overflow-auto p-4">
          <div className="flex justify-between items-center mb-4">
            <h2 className="text-white font-semibold text-sm uppercase tracking-wide">Inference Results</h2>
            <button className="text-white/60 hover:text-white text-xs bg-white/10 px-3 py-1 rounded" onClick={() => setInferResults([])}>Close</button>
          </div>
          <div className="grid grid-cols-1 gap-6 md:grid-cols-2">
            {inferResults.map((r, i) => (
              <div key={i} className="bg-white/5 rounded-lg overflow-hidden">
                {r.error ? (
                  <p className="text-red-400 p-3 text-xs">{r.filename}: {r.error}</p>
                ) : (
                  <>
                    <img src={r.image} alt={r.filename} className="w-full object-contain" />
                    <div className="p-3 font-mono text-xs">
                      <p className="text-white/60 mb-2">{r.filename}</p>
                      {r.detections.length === 0 ? (
                        <p className="text-white/40">No detections</p>
                      ) : (
                        r.detections.map((d, j) => (
                          <div key={j} className="text-white/90 py-0.5">
                            <span className="text-green-400">[{d.confidence}%]</span>{" "}
                            <span className="text-yellow-300">{d.label}</span>
                          </div>
                        ))
                      )}
                    </div>
                  </>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Bottom overlay: classes + detection log */}
      {showControls && <div className="absolute bottom-0 left-0 right-0 h-1/5 bg-black/70 backdrop-blur-sm z-10 flex">
        <div className="w-2/5 flex flex-col border-r border-white/10">
          <div className="px-3 py-2 border-b border-white/20 flex items-center justify-between shrink-0">
            <h2 className="text-white text-sm font-semibold tracking-wide uppercase">Classes</h2>
            <span className="text-white/40 text-xs font-mono">{Object.keys(classStats).length}</span>
          </div>
          <div className="flex-1 overflow-y-auto px-3 py-1 font-mono text-xs">
            {Object.keys(classStats).length === 0 ? (
              <p className="text-white/40 py-1">Loading...</p>
            ) : (
              Object.entries(classStats)
                .sort(([, a], [, b]) => {
                  if ((a.seconds_visible !== null) !== (b.seconds_visible !== null))
                    return a.seconds_visible !== null ? -1 : 1
                  return b.count - a.count
                })
                .map(([name, stats]) => (
                  <div key={name} className={`flex items-center gap-2 py-0.5 ${stats.count === 0 ? "opacity-30" : ""}`}>
                    <span className="flex-1 truncate text-white/80">{name}</span>
                    <span className="text-white/40 w-8 text-right">{stats.count}×</span>
                    {stats.seconds_visible !== null ? (
                      <span className="text-green-400 w-12 text-right">{stats.seconds_visible}s</span>
                    ) : (
                      <span className="text-white/20 w-12 text-right">—</span>
                    )}
                  </div>
                ))
            )}
          </div>
        </div>

        <div className="flex-1 flex flex-col">
          <div className="px-4 py-2 border-b border-white/20 flex items-center justify-between shrink-0">
            <h2 className="text-white text-sm font-semibold tracking-wide uppercase">
              {activeModel ? modelLabel(activeModel) : "Detections"}
            </h2>
            <span className={`text-xs font-mono ${apiStatus === "connected" ? "text-green-400" : "text-yellow-300"}`}>
              {apiStatus === "connected" ? "connected" : "reconnecting"}
            </span>
          </div>
          <div ref={logRef} className="flex-1 overflow-y-auto px-4 py-2 font-mono text-sm">
            {apiStatus !== "connected" ? (
              <p className="text-white/60">Disconnected from backend...</p>
            ) : detections.length === 0 ? (
              <p className="text-white/50">Waiting for detections...</p>
            ) : (
              detections.map((detection, index) => (
                <div key={`${detection.timestamp}-${index}`} className="text-white/90 py-0.5">
                  {detection.periodic ? (
                    <>
                      <span className="text-purple-400">⏱</span>{" "}
                      <span className="text-white/50 text-xs">{new Date(detection.timestamp).toLocaleTimeString()}</span>
                    </>
                  ) : (
                    <>
                      <span className="text-green-400">[{detection.confidence}%]</span>{" "}
                      <span className="text-yellow-300">{detection.label}</span>{" "}
                      <span className="text-white/50 text-xs">{new Date(detection.timestamp).toLocaleTimeString()}</span>
                    </>
                  )}
                  {detection.vlm_answers && detection.vlm_answers.length > 0 && (
                    <div className="pl-2 mt-0.5 flex flex-col gap-0.5">
                      {detection.vlm_answers.length === 1 ? (
                        <span className="text-cyan-300/80 text-xs italic leading-tight">{detection.vlm_answers[0].answer}</span>
                      ) : (
                        detection.vlm_answers.map((qa, qi) => (
                          <div key={qi} className="text-xs leading-tight">
                            <span className="text-white/40">Q: </span><span className="text-white/60 italic">{qa.question}</span>
                            <br />
                            <span className="text-cyan-300/80 italic">{qa.answer}</span>
                          </div>
                        ))
                      )}
                    </div>
                  )}
                </div>
              ))
            )}
          </div>
        </div>
      </div>}
    </div>
  )
}
