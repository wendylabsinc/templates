import { Outlet, Route, Routes } from "react-router-dom"
import { AppSidebar } from "@/components/app-sidebar"
import {
  SidebarInset,
  SidebarProvider,
  SidebarTrigger,
} from "@/components/ui/sidebar"
import { TooltipProvider } from "@/components/ui/tooltip"
import { ThemeSync } from "@/lib/theme"
import { HomePage } from "@/pages/home"
import { SettingsPage } from "@/pages/settings"
import { ThreadPage } from "@/pages/thread"

function Layout() {
  return (
    <SidebarProvider>
      <AppSidebar />
      <SidebarInset>
        <header className="flex h-12 shrink-0 items-center gap-2 px-3">
          <SidebarTrigger className="-ml-1" />
        </header>
        <main className="min-h-0 flex-1">
          <Outlet />
        </main>
      </SidebarInset>
    </SidebarProvider>
  )
}

function App() {
  return (
    <TooltipProvider>
      <ThemeSync />
      <Routes>
        <Route element={<Layout />}>
          <Route index element={<HomePage />} />
          <Route path="threads/:threadId" element={<ThreadPage />} />
          <Route path="settings" element={<SettingsPage />} />
        </Route>
      </Routes>
    </TooltipProvider>
  )
}

export default App
