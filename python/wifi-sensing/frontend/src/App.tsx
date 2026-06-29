import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom"
import { AppSidebar } from "@/components/app-sidebar"
import { SiteHeader } from "@/components/site-header"
import { SidebarInset, SidebarProvider } from "@/components/ui/sidebar"

import CameraPage from "@/pages/camera"
import AudioPage from "@/pages/audio"
import PersistencePage from "@/pages/persistence"
import GpuPage from "@/pages/gpu"
import SystemPage from "@/pages/system"

export default function App() {
  return (
    <BrowserRouter>
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
          <SiteHeader />
          <div className="flex flex-1 flex-col">
            <div className="@container/main flex flex-1 flex-col gap-2">
              <Routes>
                <Route path="/" element={<Navigate to="/camera" replace />} />
                <Route path="/camera" element={<CameraPage />} />
                <Route path="/audio" element={<AudioPage />} />
                <Route path="/persistence" element={<PersistencePage />} />
                <Route path="/gpu" element={<GpuPage />} />
                <Route path="/system" element={<SystemPage />} />
              </Routes>
            </div>
          </div>
        </SidebarInset>
      </SidebarProvider>
    </BrowserRouter>
  )
}
