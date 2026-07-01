import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom"
import { AppSidebar } from "@/components/app-sidebar"
import { SiteHeader } from "@/components/site-header"
import { SensingProvider } from "@/components/sensing-provider"
import { SidebarInset, SidebarProvider } from "@/components/ui/sidebar"
import { Toaster } from "@/components/ui/sonner"

import LivePage from "@/pages/live"
import SensorsPage from "@/pages/sensors"
import WaterfallPage from "@/pages/waterfall"

export default function App() {
  return (
    <BrowserRouter>
      <SensingProvider>
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
                  <Route path="/" element={<Navigate to="/live" replace />} />
                  <Route path="/live" element={<LivePage />} />
                  <Route path="/sensors" element={<SensorsPage />} />
                  <Route path="/waterfall" element={<WaterfallPage />} />
                </Routes>
              </div>
            </div>
          </SidebarInset>
        </SidebarProvider>
        <Toaster />
      </SensingProvider>
    </BrowserRouter>
  )
}
