import { Separator } from "@/components/ui/separator"
import { SidebarTrigger } from "@/components/ui/sidebar"
import { useSensing } from "@/hooks/use-sensing-stream"

const DOT: Record<string, string> = {
  open: "bg-green-500",
  connecting: "bg-amber-500",
  closed: "bg-red-500",
}

const LABEL: Record<string, string> = {
  open: "Streaming",
  connecting: "Connecting…",
  closed: "Disconnected",
}

export function SiteHeader() {
  const { status, frame } = useSensing()
  const sensorCount = frame?.sensors.length ?? 0

  return (
    <header className="flex h-(--header-height) shrink-0 items-center gap-2 border-b transition-[width,height] ease-linear group-has-data-[collapsible=icon]/sidebar-wrapper:h-(--header-height)">
      <div className="flex w-full items-center gap-1 px-4 lg:gap-2 lg:px-6">
        <SidebarTrigger className="-ml-1" />
        <Separator orientation="vertical" className="mx-2 h-4 data-vertical:self-auto" />
        <h1 className="text-base font-medium">WiFi Sensing</h1>
        <div className="ml-auto flex items-center gap-2 text-sm text-muted-foreground">
          <span>{sensorCount} sensor{sensorCount === 1 ? "" : "s"}</span>
          <span className={`inline-block h-2 w-2 rounded-full ${DOT[status]}`} />
          <span>{LABEL[status]}</span>
        </div>
      </div>
    </header>
  )
}
