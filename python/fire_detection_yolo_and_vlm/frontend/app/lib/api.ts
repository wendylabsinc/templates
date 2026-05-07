const BASE_URL = typeof window !== "undefined"
  ? `${window.location.protocol}//${window.location.hostname}:5702`
  : "http://localhost:5702"

export interface Conversation {
  conversationId: string
  title: string
  createdAt: number
  updatedAt: number
}

export interface ChatMessage {
  chatMessageId: string
  conversationId: string
  createdAt: number
  userId: string
  body: string
}

export interface CameraInfo {
  id: string
  name: string
  available: boolean
}

export async function fetchConversations(): Promise<Conversation[]> {
  const res = await fetch(`${BASE_URL}/conversations`)
  if (!res.ok) throw new Error("Failed to fetch conversations")
  return res.json()
}

export async function fetchCameras(): Promise<CameraInfo[]> {
  const res = await fetch(`${BASE_URL}/cameras`)
  if (!res.ok) throw new Error("Failed to fetch cameras")
  return res.json()
}

export async function fetchMessages(conversationId: string): Promise<ChatMessage[]> {
  const res = await fetch(`${BASE_URL}/conversations/${conversationId}/messages`)
  if (!res.ok) throw new Error("Failed to fetch messages")
  return res.json()
}

export async function sendMessage(
  conversationId: string,
  body: string,
  generateTitle = false,
): Promise<{ reply: ChatMessage | null; generatedTitle: string | null }> {
  const res = await fetch(`${BASE_URL}/conversations/${conversationId}/messages`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ userId: "local-user", body, generateTitle }),
  })
  if (!res.ok) throw new Error("Failed to send message")
  return res.json()
}

export async function createConversation(title: string): Promise<Conversation> {
  const res = await fetch(`${BASE_URL}/conversations`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title }),
  })
  if (!res.ok) throw new Error("Failed to create conversation")
  return res.json()
}

export function getCameraStreamURL(cameraId: string): string {
  return `${BASE_URL}/cameras/${encodeURIComponent(cameraId)}/stream`
}
