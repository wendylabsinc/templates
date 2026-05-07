"use client"

import * as React from "react"
import {
  Avatar,
  AvatarFallback,
} from "~/components/ui/avatar"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "~/components/ui/alert-dialog"
import { Input } from "~/components/ui/input"
import { Label } from "~/components/ui/label"
import {
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
} from "~/components/ui/sidebar"
import { Settings2Icon } from "lucide-react"
import { getUserName, setUserName, getNodeId } from "~/lib/node-id"

export function NavUser() {
  const [name, setName] = React.useState("")
  const [editName, setEditName] = React.useState("")
  const [nodeId, setNodeId] = React.useState("")

  React.useEffect(() => {
    setNodeId(getNodeId())
    setName(getUserName())
  }, [])

  const initials = name
    ? name
        .split(" ")
        .map((w) => w[0])
        .join("")
        .toUpperCase()
        .slice(0, 2)
    : "?"

  return (
    <SidebarMenu>
      <SidebarMenuItem>
        <AlertDialog>
          <AlertDialogTrigger
            render={
              <SidebarMenuButton size="lg" className="cursor-pointer" />
            }
          >
            <Avatar className="size-8 rounded-lg">
              <AvatarFallback className="rounded-lg">{initials}</AvatarFallback>
            </Avatar>
            <div className="grid flex-1 text-left text-sm leading-tight">
              <span className="truncate font-medium">
                {name || "Set your name"}
              </span>
              <span className="truncate text-xs text-foreground/70">
                Node {nodeId.slice(0, 8)}...
              </span>
            </div>
            <Settings2Icon className="ml-auto size-4 text-muted-foreground" />
          </AlertDialogTrigger>
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>Settings</AlertDialogTitle>
            </AlertDialogHeader>
            <div className="grid gap-3 py-2">
              <div className="grid gap-2">
                <Label htmlFor="name">Name</Label>
                <Input
                  id="name"
                  defaultValue={name}
                  onChange={(e) => setEditName(e.target.value)}
                  placeholder="Your name"
                />
              </div>
              <div className="grid gap-1">
                <Label>Node ID</Label>
                <code className="select-all rounded bg-muted px-2 py-1 font-mono text-xs text-muted-foreground">
                  {nodeId}
                </code>
              </div>
            </div>
            <AlertDialogFooter>
              <AlertDialogCancel>Cancel</AlertDialogCancel>
              <AlertDialogAction
                onClick={() => {
                  if (editName.trim()) {
                    setUserName(editName.trim())
                    setName(editName.trim())
                  }
                }}
              >
                Save
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
      </SidebarMenuItem>
    </SidebarMenu>
  )
}
