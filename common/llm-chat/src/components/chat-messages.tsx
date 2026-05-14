import * as React from "react"
import { Virtuoso, type VirtuosoHandle } from "react-virtuoso"
import { ChatMessageView } from "@/components/chat-message"
import { cn } from "@/lib/utils"
import { useSettings } from "@/lib/settings"
import type { ChatMessage } from "@/lib/threads"

type Props = {
  messages: ChatMessage[]
}

export function ChatMessages({ messages }: Props) {
  const { chatFullWidth } = useSettings()
  const ref = React.useRef<VirtuosoHandle>(null)
  const [atBottom, setAtBottom] = React.useState(true)

  React.useEffect(() => {
    if (atBottom && messages.length > 0) {
      ref.current?.scrollToIndex({
        index: messages.length - 1,
        align: "end",
        behavior: "smooth",
      })
    }
  }, [messages, atBottom])

  return (
    <Virtuoso
      ref={ref}
      data={messages}
      followOutput="smooth"
      atBottomStateChange={setAtBottom}
      initialTopMostItemIndex={Math.max(0, messages.length - 1)}
      className="h-full"
      computeItemKey={(_, item) => item.id}
      itemContent={(_, message) => (
        <div
          className={cn(
            "mx-auto w-full px-4",
            chatFullWidth ? "max-w-none" : "max-w-3xl",
          )}
        >
          <ChatMessageView message={message} />
        </div>
      )}
    />
  )
}
