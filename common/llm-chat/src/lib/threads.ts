import { useSyncExternalStore } from "react"

export type ChatMessage = {
  id: string
  role: "user" | "assistant"
  content: string
}

export type Thread = {
  id: string
  title: string
  messages: ChatMessage[]
  createdAt: number
  archived?: boolean
  pinned?: boolean
}

type State = {
  threads: Record<string, Thread>
  order: string[]
}

let state: State = { threads: {}, order: [] }
const listeners = new Set<() => void>()

function emit() {
  for (const l of listeners) l()
}

function subscribe(listener: () => void) {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

function getSnapshot() {
  return state
}

export function useThreads() {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot)
}

export function useThread(id: string | undefined) {
  const s = useThreads()
  return id ? s.threads[id] : undefined
}

function uid() {
  return Math.random().toString(36).slice(2, 10)
}

export function createThread(firstMessage: string): Thread {
  const id = uid()
  const thread: Thread = {
    id,
    title: firstMessage.slice(0, 60) || "New chat",
    messages: [],
    createdAt: Date.now(),
  }
  state = {
    threads: { ...state.threads, [id]: thread },
    order: [id, ...state.order],
  }
  emit()
  return thread
}

export function appendMessage(threadId: string, message: ChatMessage) {
  const t = state.threads[threadId]
  if (!t) return
  state = {
    ...state,
    threads: {
      ...state.threads,
      [threadId]: { ...t, messages: [...t.messages, message] },
    },
  }
  emit()
}

export function updateMessage(
  threadId: string,
  messageId: string,
  updater: (m: ChatMessage) => ChatMessage,
) {
  const t = state.threads[threadId]
  if (!t) return
  state = {
    ...state,
    threads: {
      ...state.threads,
      [threadId]: {
        ...t,
        messages: t.messages.map((m) => (m.id === messageId ? updater(m) : m)),
      },
    },
  }
  emit()
}

export function newMessageId() {
  return uid()
}

function patchThread(threadId: string, patch: Partial<Thread>) {
  const t = state.threads[threadId]
  if (!t) return
  state = {
    ...state,
    threads: { ...state.threads, [threadId]: { ...t, ...patch } },
  }
  emit()
}

export function archiveThread(threadId: string) {
  patchThread(threadId, { archived: true })
}

export function setThreadPinned(threadId: string, pinned: boolean) {
  patchThread(threadId, { pinned })
}

export function renameThread(threadId: string, title: string) {
  const trimmed = title.trim()
  if (!trimmed) return
  patchThread(threadId, { title: trimmed })
}

// Mock streaming: yields chunks of a canned markdown response.
export async function streamAssistantReply(
  threadId: string,
  userPrompt: string,
  onChunk: (id: string) => void,
) {
  const id = newMessageId()
  appendMessage(threadId, { id, role: "assistant", content: "" })

  const reply = buildMockReply(userPrompt)
  const tokens = reply.match(/[\s\S]{1,6}/g) ?? [reply]

  for (const token of tokens) {
    await new Promise((r) => setTimeout(r, 18))
    updateMessage(threadId, id, (m) => ({ ...m, content: m.content + token }))
    onChunk(id)
  }
}

function buildMockReply(prompt: string) {
  return `Here's a thought on **${prompt.slice(0, 80)}**:

## Recommended approach

Use a focused message rather than a sprawl. The core positioning should be:

> Wendy is the open-source dev stack for physical AI: deploy and debug apps on Jetson, Raspberry Pi, and Linux devices without SSH, driver hell, or setup fatigue.

### Next steps

1. Fix the landing funnel first
2. Ship a 60-second demo
3. Publish a weekly changelog

\`\`\`ts
// Example
const wendy = await connect()
await wendy.run("./app")
\`\`\`

That's the gist — happy to dig into any of these.`
}
