"use client"

import * as React from "react"
import { useLocation } from "react-router"
import {
  SidebarGroup,
  SidebarGroupLabel,
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
import { CameraIcon } from "lucide-react"
import { type CameraInfo, fetchCameras } from "~/lib/api"

export function NavCameras() {
  const [cameras, setCameras] = React.useState<CameraInfo[]>([])
  const location = useLocation()
  const activeCameraId = decodeURIComponent(
    location.pathname.replace("/cameras/", "")
  )

  React.useEffect(() => {
    fetchCameras()
      .then(setCameras)
      .catch(() => {})

    const interval = setInterval(() => {
      fetchCameras()
        .then(setCameras)
        .catch(() => {})
    }, 10000)
    return () => clearInterval(interval)
  }, [])

  return (
    <SidebarGroup className="group-data-[collapsible=icon]:hidden">
      <SidebarGroupLabel>Cameras</SidebarGroupLabel>
      <SidebarMenu>
        {cameras.length === 0 ? (
          <SidebarMenuItem>
            <SidebarMenuButton disabled>
              <CameraIcon />
              <span className="text-muted-foreground">No cameras found</span>
            </SidebarMenuButton>
          </SidebarMenuItem>
        ) : (
          <TooltipProvider delayDuration={300}>
            {cameras.map((cam) => (
              <SidebarMenuItem key={cam.id}>
                <Tooltip>
                  <TooltipTrigger asChild>
                    <SidebarMenuButton
                      isActive={activeCameraId === cam.id}
                      render={<a href={`/cameras/${encodeURIComponent(cam.id)}`} />}
                    >
                      <CameraIcon />
                      <span className="truncate">{cam.name}</span>
                      {cam.available && (
                        <span className="ml-auto size-2 shrink-0 rounded-full bg-green-500" />
                      )}
                    </SidebarMenuButton>
                  </TooltipTrigger>
                  <TooltipContent side="right">
                    {cam.name}
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
