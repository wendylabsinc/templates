import * as React from "react"
import { Link, useLocation, useNavigate, useParams } from "react-router-dom"
import {
  Archive,
  MessageSquare,
  MoreHorizontal,
  Pencil,
  Pin,
  PinOff,
  Plus,
  Settings,
} from "lucide-react"

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuAction,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarRail,
} from "@/components/ui/sidebar"
import {
  archiveThread,
  renameThread,
  setThreadPinned,
  useThreads,
} from "@/lib/threads"

export function AppSidebar({ ...props }: React.ComponentProps<typeof Sidebar>) {
  const { threadId } = useParams()
  const navigate = useNavigate()
  const location = useLocation()
  const { threads, order } = useThreads()
  const isSettings = location.pathname === "/settings"

  const [archiveId, setArchiveId] = React.useState<string | null>(null)
  const [renameId, setRenameId] = React.useState<string | null>(null)
  const [renameValue, setRenameValue] = React.useState("")

  const archiveTarget = archiveId ? threads[archiveId] : undefined
  const renameTarget = renameId ? threads[renameId] : undefined

  React.useEffect(() => {
    if (renameTarget) setRenameValue(renameTarget.title)
  }, [renameTarget])

  const confirmArchive = () => {
    if (!archiveId) return
    const wasOpen = archiveId === threadId
    archiveThread(archiveId)
    setArchiveId(null)
    if (wasOpen) navigate("/")
  }

  const confirmRename = () => {
    if (!renameId) return
    renameThread(renameId, renameValue)
    setRenameId(null)
  }

  const visible = order.filter((id) => threads[id] && !threads[id].archived)
  const pinned = visible.filter((id) => threads[id].pinned)
  const unpinned = visible.filter((id) => !threads[id].pinned)

  return (
    <>
      <Sidebar {...props}>
        <SidebarHeader>
          <div className="flex items-center justify-between gap-2 px-2 py-1">
            <div className="flex items-center gap-2">
              <img
                src="/wendy_logo.svg"
                alt="Wendy"
                className="h-5 w-auto dark:invert"
              />
              <span className="text-sm font-semibold">Chat</span>
            </div>
            <Button
              size="sm"
              variant="ghost"
              className="h-7 gap-1 px-2"
              onClick={() => navigate("/")}
            >
              <Plus className="size-4" />
              New
            </Button>
          </div>
        </SidebarHeader>
        <SidebarContent>
          {pinned.length > 0 && (
            <SidebarGroup>
              <SidebarGroupLabel>Pinned</SidebarGroupLabel>
              <SidebarGroupContent>
                <SidebarMenu>
                  {pinned.map((id) => (
                    <ThreadItem
                      key={id}
                      id={id}
                      title={threads[id].title}
                      pinned
                      active={threadId === id}
                      onPinToggle={() => setThreadPinned(id, false)}
                      onArchive={() => setArchiveId(id)}
                      onRename={() => setRenameId(id)}
                    />
                  ))}
                </SidebarMenu>
              </SidebarGroupContent>
            </SidebarGroup>
          )}

          <SidebarGroup>
            <SidebarGroupLabel>Threads</SidebarGroupLabel>
            <SidebarGroupContent>
              <SidebarMenu>
                {visible.length === 0 && (
                  <div className="px-2 py-4 text-xs text-muted-foreground">
                    No threads yet. Start a chat to create one.
                  </div>
                )}
                {unpinned.map((id) => (
                  <ThreadItem
                    key={id}
                    id={id}
                    title={threads[id].title}
                    pinned={false}
                    active={threadId === id}
                    onPinToggle={() => setThreadPinned(id, true)}
                    onArchive={() => setArchiveId(id)}
                    onRename={() => setRenameId(id)}
                  />
                ))}
              </SidebarMenu>
            </SidebarGroupContent>
          </SidebarGroup>
        </SidebarContent>
        <SidebarFooter>
          <Button
            asChild
            variant={isSettings ? "secondary" : "ghost"}
            className="w-full justify-start gap-2"
          >
            <Link to="/settings">
              <Settings className="size-4" />
              Settings
            </Link>
          </Button>
        </SidebarFooter>
        <SidebarRail />
      </Sidebar>

      <AlertDialog
        open={archiveId !== null}
        onOpenChange={(open) => {
          if (!open) setArchiveId(null)
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Archive this thread?</AlertDialogTitle>
            <AlertDialogDescription>
              {archiveTarget
                ? `"${archiveTarget.title}" will be hidden from the sidebar.`
                : "This thread will be hidden from the sidebar."}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={confirmArchive}>
              Archive
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog
        open={renameId !== null}
        onOpenChange={(open) => {
          if (!open) setRenameId(null)
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Rename thread</AlertDialogTitle>
            <AlertDialogDescription>
              Pick a new title for this conversation.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <form
            onSubmit={(e) => {
              e.preventDefault()
              confirmRename()
            }}
            className="space-y-2"
          >
            <Label htmlFor="rename-thread" className="sr-only">
              Thread title
            </Label>
            <Input
              id="rename-thread"
              autoFocus
              value={renameValue}
              onChange={(e) => setRenameValue(e.target.value)}
              placeholder="Thread title"
            />
            <AlertDialogFooter>
              <AlertDialogCancel type="button">Cancel</AlertDialogCancel>
              <Button type="submit" disabled={!renameValue.trim()}>
                Save
              </Button>
            </AlertDialogFooter>
          </form>
        </AlertDialogContent>
      </AlertDialog>
    </>
  )
}

type ThreadItemProps = {
  id: string
  title: string
  pinned: boolean
  active: boolean
  onPinToggle: () => void
  onArchive: () => void
  onRename: () => void
}

function ThreadItem({
  id,
  title,
  pinned,
  active,
  onPinToggle,
  onArchive,
  onRename,
}: ThreadItemProps) {
  return (
    <SidebarMenuItem>
      <SidebarMenuButton asChild isActive={active}>
        <Link to={`/threads/${id}`}>
          {pinned ? (
            <Pin className="size-4 shrink-0" />
          ) : (
            <MessageSquare className="size-4 shrink-0" />
          )}
          <span className="truncate">{title}</span>
        </Link>
      </SidebarMenuButton>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <SidebarMenuAction
            showOnHover
            aria-label="Thread actions"
            onClick={(e) => e.preventDefault()}
          >
            <MoreHorizontal />
          </SidebarMenuAction>
        </DropdownMenuTrigger>
        <DropdownMenuContent side="right" align="start">
          <DropdownMenuItem onSelect={onPinToggle}>
            {pinned ? (
              <>
                <PinOff className="size-4" />
                Unpin
              </>
            ) : (
              <>
                <Pin className="size-4" />
                Pin
              </>
            )}
          </DropdownMenuItem>
          <DropdownMenuItem onSelect={onRename}>
            <Pencil className="size-4" />
            Rename
          </DropdownMenuItem>
          <DropdownMenuItem onSelect={onArchive}>
            <Archive className="size-4" />
            Archive
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </SidebarMenuItem>
  )
}
