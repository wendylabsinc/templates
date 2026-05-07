"use client"

import * as React from "react"
import { useParams, useNavigate } from "react-router"
import { useVirtualizer } from "@tanstack/react-virtual"
import { AppSidebar } from "~/components/app-sidebar"
import { SiteHeader } from "~/components/site-header"
import { SidebarInset, SidebarProvider } from "~/components/ui/sidebar"
import { AdvancedChatInput } from "~/components/ui/advanced-ai-chat-input"
import { ChatMessageListItem } from "~/components/chat-message-list-item"
import {
  type ChatMessage,
  type Conversation,
  fetchMessages,
  fetchConversations,
  sendMessage,
  createConversation,
} from "~/lib/api"

export default function ConversationPage() {
  const { id } = useParams()
  const navigate = useNavigate()
  const isNew = id === "new"

  const [conversationId, setConversationId] = React.useState<string | null>(
    isNew ? null : id ?? null
  )
  const [messages, setMessages] = React.useState<ChatMessage[]>([])
  const [conversationTitle, setConversationTitle] = React.useState(isNew ? "New Conversation" : "Chat")
  const [inputValue, setInputValue] = React.useState("")
  const [sending, setSending] = React.useState(false)

  const parentRef = React.useRef<HTMLDivElement>(null)

  const virtualizer = useVirtualizer({
    count: messages.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 60,
    overscan: 10,
  })

  // Fetch conversation title
  React.useEffect(() => {
    if (!conversationId) return
    fetchConversations().then((convs) => {
      const conv = convs.find((c) => c.conversationId === conversationId)
      if (conv) setConversationTitle(conv.title)
    }).catch(() => {})
  }, [conversationId])

  // Poll messages
  React.useEffect(() => {
    if (!conversationId) return
    fetchMessages(conversationId).then(setMessages).catch(() => {})
    const interval = setInterval(() => {
      fetchMessages(conversationId).then(setMessages).catch(() => {})
    }, 2000)
    return () => clearInterval(interval)
  }, [conversationId])

  // Scroll to bottom when messages change
  React.useEffect(() => {
    if (messages.length > 0) {
      virtualizer.scrollToIndex(messages.length - 1, { align: "end" })
    }
  }, [messages.length])

  const handleSend = async () => {
    const text = inputValue.trim()
    if (!text || sending) return

    setSending(true)
    setInputValue("")

    try {
      let cid = conversationId
      if (!cid) {
        const conv = await createConversation("New Conversation")
        cid = conv.conversationId
        setConversationId(cid)
        navigate(`/conversations/${cid}`, { replace: true })
      }

      // Optimistically show the user's message immediately
      const optimistic: ChatMessage = {
        chatMessageId: `optimistic-${Date.now()}`,
        conversationId: cid,
        createdAt: Date.now(),
        userId: "local-user",
        body: text,
      }
      setMessages((prev) => [...prev, optimistic])

      const isFirst = messages.length === 0
      await sendMessage(cid, text, isFirst)

      const updated = await fetchMessages(cid)
      setMessages(updated)
    } catch {
      // Network error
    } finally {
      setSending(false)
    }
  }

  return (
    <SidebarProvider
      style={
        {
          "--sidebar-width": "calc(var(--spacing) * 72)",
          "--header-height": "calc(var(--spacing) * 12)",
        } as React.CSSProperties
      }
    >
      <AppSidebar variant="inset" />
      <SidebarInset>
        <SiteHeader title={conversationTitle} />
        <div className="flex flex-1 flex-col">
          {/* Virtualized message list */}
          <div ref={parentRef} className="flex-1 overflow-auto">
            <div
              style={{ height: `${virtualizer.getTotalSize()}px`, position: "relative" }}
              className="mx-auto w-full max-w-2xl"
            >
              {virtualizer.getVirtualItems().map((virtualItem) => {
                const message = messages[virtualItem.index]
                return (
                  <div
                    key={message.chatMessageId}
                    data-index={virtualItem.index}
                    ref={virtualizer.measureElement}
                    style={{
                      position: "absolute",
                      top: 0,
                      left: 0,
                      width: "100%",
                      transform: `translateY(${virtualItem.start}px)`,
                    }}
                    className="px-4 py-1"
                  >
                    <ChatMessageListItem message={message} />
                  </div>
                )
              })}
            </div>
          </div>

          {/* Thinking indicator */}
          {sending && (
            <div className="mx-auto w-full max-w-2xl px-4 py-1">
              <p className="text-sm text-muted-foreground animate-pulse">Albert is thinking...</p>
            </div>
          )}

          {/* Chat input */}
          <div className="mx-auto w-full max-w-2xl px-4 pb-4 pt-2">
            <AdvancedChatInput
              textareaProps={{
                value: inputValue,
                onChange: (e) => setInputValue(e.target.value),
                placeholder: isNew
                  ? "Start a new conversation..."
                  : "Type a message...",
                onKeyDown: (e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault()
                    handleSend()
                  }
                },
              }}
              onSend={handleSend}
            />
          </div>
        </div>
      </SidebarInset>
    </SidebarProvider>
  )
}
