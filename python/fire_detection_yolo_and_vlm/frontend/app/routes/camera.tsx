"use client"

import * as React from "react"
import { useParams } from "react-router"
import { AppSidebar } from "~/components/app-sidebar"
import { SiteHeader } from "~/components/site-header"
import { SidebarInset, SidebarProvider } from "~/components/ui/sidebar"
import { getCameraStreamURL } from "~/lib/api"

export default function CameraPage() {
  const { id } = useParams()
  const cameraId = decodeURIComponent(id ?? "0")
  const streamUrl = getCameraStreamURL(cameraId)

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
        <SiteHeader title={cameraId} />
        <div className="flex flex-1 overflow-hidden bg-black">
          <img
            src={streamUrl}
            alt={cameraId}
            className="size-full object-contain"
          />
        </div>
      </SidebarInset>
    </SidebarProvider>
  )
}
