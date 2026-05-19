import { useNavigate } from "react-router-dom"
import { ChatInput } from "@/components/chat-input"
import {
  appendMessage,
  createThread,
  newMessageId,
  streamAssistantReply,
} from "@/lib/threads"

export function HomePage() {
  const navigate = useNavigate()

  const handleSend = (value: string) => {
    const thread = createThread(value)
    appendMessage(thread.id, {
      id: newMessageId(),
      role: "user",
      content: value,
    })
    void streamAssistantReply(thread.id, value, () => {})
    navigate(`/threads/${thread.id}`, { viewTransition: true })
  }

  return (
    <div className="flex h-full w-full flex-col items-center justify-center px-4">
      <div className="w-full max-w-2xl">
        <h1 className="mb-6 text-center text-3xl font-semibold tracking-tight">
          What can I help with?
        </h1>
        <div className="chat-input-shared">
          <ChatInput autoFocus onSend={handleSend} />
        </div>
      </div>
    </div>
  )
}
