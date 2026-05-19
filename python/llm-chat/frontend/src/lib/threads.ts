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

export async function streamAssistantReply(
  threadId: string,
  _userPrompt: string,
  onChunk: (id: string) => void,
) {
  const thread = state.threads[threadId]
  if (!thread) return

  const history = thread.messages.map(({ role, content }) => ({
    role,
    content,
  }))

  const id = newMessageId()
  appendMessage(threadId, { id, role: "assistant", content: "" })

  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages: history }),
    })

    if (!response.ok) {
      const detail = await response.text()
      throw new Error(detail || `Chat request failed with HTTP ${response.status}`)
    }

    if (!response.body) {
      throw new Error("Chat response did not include a stream")
    }

    const reader = response.body.getReader()
    const decoder = new TextDecoder()

    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      const chunk = decoder.decode(value, { stream: true })
      if (!chunk) continue
      updateMessage(threadId, id, (m) => ({
        ...m,
        content: m.content + chunk,
      }))
      onChunk(id)
    }

    const trailing = decoder.decode()
    if (trailing) {
      updateMessage(threadId, id, (m) => ({
        ...m,
        content: m.content + trailing,
      }))
      onChunk(id)
    }
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error)
    updateMessage(threadId, id, (m) => ({
      ...m,
      content: `llm-chat backend error: ${message}`,
    }))
    onChunk(id)
  }
}
