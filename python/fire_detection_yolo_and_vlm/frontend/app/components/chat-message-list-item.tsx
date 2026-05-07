import { cn } from "~/lib/utils"
import type { ChatMessage } from "~/lib/api"

const ALBERT_USER_ID = "albert"

export function ChatMessageListItem({ message }: { message: ChatMessage }) {
  const isCurrentUser = message.userId !== ALBERT_USER_ID

  if (isCurrentUser) {
    // User message: left-aligned plain text
    return (
      <div className="py-2 text-left">
        <p className="text-sm">{message.body}</p>
      </div>
    )
  }

  // AI message: full-width, secondary background, rounded
  return (
    <div className="w-full rounded-xl bg-muted px-3.5 py-2.5">
      <p className="text-sm">{message.body}</p>
    </div>
  )
}
