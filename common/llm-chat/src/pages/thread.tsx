import { Navigate, useParams } from "react-router-dom"
import { ChatInput } from "@/components/chat-input"
import { ChatMessages } from "@/components/chat-messages"
import { cn } from "@/lib/utils"
import { useDocumentTitle } from "@/lib/document-title"
import { useSettings } from "@/lib/settings"
import {
  appendMessage,
  newMessageId,
  streamAssistantReply,
  useThread,
} from "@/lib/threads"

export function ThreadPage() {
  const { threadId } = useParams<{ threadId: string }>()
  const thread = useThread(threadId)
  const { chatFullWidth } = useSettings()
  useDocumentTitle(thread?.title)

  if (!threadId) return <Navigate to="/" replace />
  if (!thread) return <Navigate to="/" replace />

  const handleSend = (value: string) => {
    appendMessage(thread.id, {
      id: newMessageId(),
      role: "user",
      content: value,
    })
    void streamAssistantReply(thread.id, value, () => {})
  }

  return (
    <div className="flex h-full w-full flex-col">
      <div className="chat-messages-shared min-h-0 flex-1">
        <ChatMessages messages={thread.messages} />
      </div>
      <div className="border-t bg-background">
        <div
          className={cn(
            "mx-auto w-full px-4 py-3",
            chatFullWidth ? "max-w-none" : "max-w-3xl",
          )}
        >
          <div className="chat-input-shared">
            <ChatInput onSend={handleSend} />
          </div>
        </div>
      </div>
    </div>
  )
}
