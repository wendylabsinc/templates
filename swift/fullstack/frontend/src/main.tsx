import { StrictMode } from "react"
import { createRoot } from "react-dom/client"
import { TooltipProvider } from "@/components/ui/tooltip"
import "./index.css"
import App from "./App"

// Sync dark mode with browser preference
function syncDarkMode() {
  const mq = window.matchMedia("(prefers-color-scheme: dark)")
  const apply = () => document.documentElement.classList.toggle("dark", mq.matches)
  apply()
  mq.addEventListener("change", apply)
}
syncDarkMode()

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <TooltipProvider>
      <App />
    </TooltipProvider>
  </StrictMode>
)
