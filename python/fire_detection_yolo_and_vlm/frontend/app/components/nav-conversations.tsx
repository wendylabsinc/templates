"use client"

import * as React from "react"
import { useLocation } from "react-router"
import {
  SidebarGroup,
  SidebarGroupLabel,
  SidebarGroupAction,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
} from "~/components/ui/sidebar"
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "~/components/ui/tooltip"
import { MessageSquareIcon, PlusIcon } from "lucide-react"
import { type Conversation, fetchConversations } from "~/lib/api"

export function NavConversations() {
  const [conversations, setConversations] = React.useState<Conversation[]>([])
  const location = useLocation()
  const activeConvId = location.pathname.replace("/conversations/", "")

  React.useEffect(() => {
    fetchConversations()
      .then(setConversations)
      .catch(() => {})

    const interval = setInterval(() => {
      fetchConversations()
        .then(setConversations)
        .catch(() => {})
    }, 5000)
    return () => clearInterval(interval)
  }, [])

  return (
    <SidebarGroup className="group-data-[collapsible=icon]:hidden">
      <SidebarGroupLabel>
        Conversations
      </SidebarGroupLabel>
      <SidebarGroupAction render={<a href="/conversations/new" />} title="New Conversation">
        <PlusIcon />
        <span className="sr-only">New Conversation</span>
      </SidebarGroupAction>
      <SidebarMenu>
        {conversations.length === 0 ? (
          <SidebarMenuItem>
            <SidebarMenuButton disabled>
              <MessageSquareIcon />
              <span className="text-muted-foreground">No conversations yet</span>
            </SidebarMenuButton>
          </SidebarMenuItem>
        ) : (
          <TooltipProvider delayDuration={300}>
            {conversations.map((conv) => (
              <SidebarMenuItem key={conv.conversationId}>
                <Tooltip>
                  <TooltipTrigger asChild>
                    <SidebarMenuButton
                      isActive={activeConvId === conv.conversationId}
                      render={<a href={`/conversations/${conv.conversationId}`} />}
                    >
                      <MessageSquareIcon />
                      <span className="truncate">{conv.title}</span>
                    </SidebarMenuButton>
                  </TooltipTrigger>
                  <TooltipContent side="right">
                    {conv.title}
                  </TooltipContent>
                </Tooltip>
              </SidebarMenuItem>
            ))}
          </TooltipProvider>
        )}
      </SidebarMenu>
    </SidebarGroup>
  )
}
