import { useEffect, useMemo, useRef, useState } from "react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { ChatInput, ChatInputTextArea, ChatInputSubmit } from "@/components/ui/chat-input"
import { useAnimatedText } from "@/components/ui/animated-text"
import { Input } from "@/components/ui/input"
import { Separator } from "@/components/ui/separator"
import { Textarea } from "@/components/ui/textarea"
import { cn } from "@/lib/utils"
import {
  Cpu,
  MessageCircle,
  RotateCcw,
  Send,
  Sparkles,
  Timer,
  Zap,
} from "lucide-react"

type Role = "user" | "assistant"

interface ChatMessage {
  id: string
  role: Role
  content: string
  createdAt: string
}

interface ChatResponse {
  reply: string
  model: string
  durationMs: number
}

const defaultSystemPrompt =
  "You are a concise, friendly assistant running on Apple Silicon via MLX. Keep answers practical. /no_think"

const starterMessages: ChatMessage[] = [
  {
    id: "welcome",
    role: "assistant",
    content:
      "Ready when you are. Ask about MLX workflows, Apple Silicon tuning, or ideas to prototype with Qwen3 4B.",
    createdAt: new Date().toISOString(),
  },
]

const promptChips = [
  "Summarize the latest chat and suggest next steps.",
  "Explain how MLX quantization works on Apple Silicon.",
  "Draft a quick checklist for benchmarking Qwen3 4B.",
  "Give me three creative demo ideas for on-device AI.",
]

function AnimatedMessage({ content }: { content: string }) {
  const animatedContent = useAnimatedText(content, " ")
  return (
    <p className="mt-2 whitespace-pre-wrap leading-relaxed">
      {animatedContent}
    </p>
  )
}

function App() {
  const [messages, setMessages] = useState<ChatMessage[]>(starterMessages)
  const [input, setInput] = useState("")
  const [systemPrompt, setSystemPrompt] = useState(defaultSystemPrompt)
  const [isSending, setIsSending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [meta, setMeta] = useState<ChatResponse | null>(null)
  const [settings, setSettings] = useState({
    maxTokens: 256,
    temperature: 0.6,
    topP: 0.95,
    topK: 20,
  })

  const endRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" })
  }, [messages, isSending])

  const canSend = input.trim().length > 0 && !isSending

  const messageCount = useMemo(
    () => messages.filter((message) => message.role === "user").length,
    [messages]
  )

  const sendMessage = async () => {
    if (!canSend) return

    const trimmed = input.trim()
    const userMessage: ChatMessage = {
      id: `${Date.now()}-user`,
      role: "user",
      content: trimmed,
      createdAt: new Date().toISOString(),
    }

    const nextMessages = [...messages, userMessage]
    setMessages(nextMessages)
    setInput("")
    setIsSending(true)
    setError(null)

    try {
      const response = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          system: systemPrompt,
          messages: nextMessages.map((message) => ({
            role: message.role,
            content: message.content,
          })),
          maxTokens: settings.maxTokens,
          temperature: settings.temperature,
          topP: settings.topP,
          topK: settings.topK,
        }),
      })

      if (!response.ok) {
        const text = await response.text()
        throw new Error(text || "Request failed")
      }

      const data = (await response.json()) as ChatResponse
      setMeta(data)

      const assistantMessage: ChatMessage = {
        id: `${Date.now()}-assistant`,
        role: "assistant",
        content: data.reply,
        createdAt: new Date().toISOString(),
      }

      setMessages((current) => [...current, assistantMessage])
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to reach server"
      setError(message)
    } finally {
      setIsSending(false)
    }
  }

  const resetChat = () => {
    setMessages(starterMessages)
    setMeta(null)
    setError(null)
  }

  return (
    <div className="relative min-h-screen w-full overflow-hidden bg-slate-50 text-slate-900">
      <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_top,rgba(56,189,248,0.18),transparent_60%),radial-gradient(circle_at_20%_30%,rgba(251,191,36,0.2),transparent_55%),linear-gradient(180deg,rgba(248,250,252,1),rgba(226,232,240,0.9))]" />
      <div className="pointer-events-none absolute inset-0 opacity-50 mix-blend-multiply [background-size:48px_48px] [background-image:linear-gradient(rgba(15,23,42,0.08)_1px,transparent_1px),linear-gradient(90deg,rgba(15,23,42,0.08)_1px,transparent_1px)]" />

      <div className="relative mx-auto flex w-full max-w-6xl flex-col gap-8 px-6 py-10 animate-in fade-in slide-in-from-bottom-2 duration-700">
        <header className="flex flex-col gap-6 md:flex-row md:items-end md:justify-between">
          <div className="space-y-4">
            <div className="inline-flex items-center gap-2 rounded-full border border-white/40 bg-white/70 px-3 py-1 text-xs font-semibold uppercase tracking-[0.2em] text-slate-500 shadow-sm shadow-black/5">
              <Sparkles className="h-3.5 w-3.5" />
              MLX Chat Studio
            </div>
            <div className="space-y-2">
              <h1 className="text-4xl font-semibold tracking-tight text-slate-900 [font-family:'Newsreader',serif]">
                Qwen3 4B, powered by MLX.
              </h1>
              <p className="max-w-xl text-base text-slate-600">
                A full-stack Python + shadcn chat surface for running MLX 4-bit models on
                Apple Silicon.
              </p>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant="soft">GPU ready</Badge>
              <Badge variant="outline">/api/chat</Badge>
              <Badge variant="secondary">Context-aware</Badge>
            </div>
          </div>

          <div className="flex items-center gap-3">
            <Button variant="outline" onClick={resetChat} className="gap-2">
              <RotateCcw className="h-4 w-4" />
              Reset
            </Button>
            <Button className="gap-2" onClick={sendMessage} disabled={!canSend}>
              <Send className="h-4 w-4" />
              Send
            </Button>
          </div>
        </header>

        <main className="grid grid-cols-1 gap-6 lg:grid-cols-[280px_minmax(0,1fr)]">
          <Card className="border-white/60 bg-white/80 backdrop-blur">
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Cpu className="h-4 w-4" />
                Model Studio
              </CardTitle>
              <CardDescription>
                Tune prompt tone, sampling, and token budget for each run.
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-4 pb-6">
              <div className="space-y-2">
                <label className="text-xs font-semibold uppercase tracking-wide text-slate-500">
                  System prompt
                </label>
                <Textarea
                  value={systemPrompt}
                  onChange={(event) => setSystemPrompt(event.target.value)}
                  className="min-h-[120px]"
                />
              </div>

              <Separator />

              <div className="grid grid-cols-2 gap-3">
                <div className="space-y-2">
                  <label className="text-xs font-semibold uppercase tracking-wide text-slate-500">
                    Max tokens
                  </label>
                  <Input
                    type="number"
                    min={32}
                    max={2048}
                    value={settings.maxTokens}
                    onChange={(event) =>
                      setSettings((current) => ({
                        ...current,
                        maxTokens: Number(event.target.value),
                      }))
                    }
                  />
                </div>
                <div className="space-y-2">
                  <label className="text-xs font-semibold uppercase tracking-wide text-slate-500">
                    Temperature
                  </label>
                  <Input
                    type="number"
                    min={0}
                    max={2}
                    step={0.1}
                    value={settings.temperature}
                    onChange={(event) =>
                      setSettings((current) => ({
                        ...current,
                        temperature: Number(event.target.value),
                      }))
                    }
                  />
                </div>
                <div className="space-y-2">
                  <label className="text-xs font-semibold uppercase tracking-wide text-slate-500">
                    Top P
                  </label>
                  <Input
                    type="number"
                    min={0}
                    max={1}
                    step={0.05}
                    value={settings.topP}
                    onChange={(event) =>
                      setSettings((current) => ({
                        ...current,
                        topP: Number(event.target.value),
                      }))
                    }
                  />
                </div>
                <div className="space-y-2">
                  <label className="text-xs font-semibold uppercase tracking-wide text-slate-500">
                    Top K
                  </label>
                  <Input
                    type="number"
                    min={0}
                    max={200}
                    step={1}
                    value={settings.topK}
                    onChange={(event) =>
                      setSettings((current) => ({
                        ...current,
                        topK: Number(event.target.value),
                      }))
                    }
                  />
                </div>
              </div>

              <div className="rounded-2xl border border-slate-200/70 bg-white/70 p-3 text-xs text-slate-500">
                <div className="flex items-center justify-between">
                  <span className="flex items-center gap-1 font-semibold text-slate-700">
                    <MessageCircle className="h-3.5 w-3.5" />
                    Messages
                  </span>
                  <span>{messageCount}</span>
                </div>
                <div className="mt-2 flex items-center justify-between">
                  <span className="flex items-center gap-1">
                    <Zap className="h-3.5 w-3.5" />
                    Mode
                  </span>
                  <span>MLX 4-bit</span>
                </div>
              </div>
            </CardContent>
          </Card>

          <Card className="flex min-h-[540px] flex-col border-white/60 bg-white/85 backdrop-blur">
            <CardHeader className="border-b border-slate-200/70">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div>
                  <CardTitle className="flex items-center gap-2">
                    Live chat
                    {meta?.model ? (
                      <Badge variant="outline" className="ml-2">
                        {meta.model}
                      </Badge>
                    ) : null}
                  </CardTitle>
                  <CardDescription>
                    Responses are generated by mlx-lm on the server.
                  </CardDescription>
                </div>
                {meta?.durationMs ? (
                  <div className="flex items-center gap-2 rounded-full border border-slate-200/70 bg-white/80 px-3 py-1 text-xs text-slate-500">
                    <Timer className="h-3.5 w-3.5" />
                    {meta.durationMs} ms
                  </div>
                ) : null}
              </div>
            </CardHeader>

            <CardContent className="flex flex-1 flex-col gap-4 py-6">
              <div className="flex flex-wrap gap-2">
                {promptChips.map((chip) => (
                  <Button
                    key={chip}
                    variant="secondary"
                    size="sm"
                    className="rounded-full"
                    onClick={() => setInput(chip)}
                  >
                    {chip}
                  </Button>
                ))}
              </div>

              <div className="flex-1 overflow-hidden rounded-3xl border border-slate-200/70 bg-white/70">
                <div className="h-[360px] overflow-y-auto px-4 py-5">
                  <div className="space-y-4">
                    {messages.map((message, index) => {
                      const isLastAssistant =
                        message.role === "assistant" &&
                        index === messages.length - 1

                      return (
                        <div
                          key={message.id}
                          className={cn(
                            "flex w-full animate-in fade-in slide-in-from-bottom-2",
                            message.role === "user" ? "justify-end" : "justify-start"
                          )}
                        >
                          <div
                            className={cn(
                              "max-w-[75%] rounded-2xl border px-4 py-3 text-sm shadow-sm",
                              message.role === "user"
                                ? "border-slate-900 bg-slate-900 text-white"
                                : "border-slate-200/80 bg-white text-slate-900"
                            )}
                          >
                            <div className="text-[11px] font-semibold uppercase tracking-widest text-slate-400">
                              {message.role}
                            </div>
                            {isLastAssistant ? (
                              <AnimatedMessage content={message.content} />
                            ) : (
                              <p className="mt-2 whitespace-pre-wrap leading-relaxed">
                                {message.content}
                              </p>
                            )}
                          </div>
                        </div>
                      )
                    })}

                    {isSending ? (
                      <div className="flex w-full justify-start">
                        <div className="max-w-[70%] rounded-2xl border border-slate-200/80 bg-white px-4 py-3 text-sm text-slate-500 shadow-sm animate-pulse">
                          Thinking with MLX...
                        </div>
                      </div>
                    ) : null}

                    <div ref={endRef} />
                  </div>
                </div>
              </div>

              {error ? (
                <div className="rounded-2xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
                  {error}
                </div>
              ) : null}
            </CardContent>

            <CardFooter className="flex flex-col gap-3 border-t border-slate-200/70">
              <ChatInput
                variant="default"
                value={input}
                onChange={(event) => setInput(event.target.value)}
                onSubmit={sendMessage}
                loading={isSending}
                onStop={() => setIsSending(false)}
                className="w-full"
              >
                <ChatInputTextArea
                  placeholder="Ask something about MLX, Apple Silicon, or on-device AI ideas..."
                  rows={2}
                />
                <ChatInputSubmit />
              </ChatInput>
              <p className="text-xs text-slate-500">
                Tip: press Shift + Enter for a new line. You can also tweak sampling
                settings in the left panel.
              </p>
            </CardFooter>
          </Card>
        </main>
      </div>
    </div>
  )
}

export default App
