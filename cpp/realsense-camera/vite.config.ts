import path from "path"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"

const backendPort = Number("{{.PORT}}") || 7007
const backendTarget = `http://localhost:${backendPort}`

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    proxy: {
      "/stream": {
        target: backendTarget,
        changeOrigin: true,
      },
      "/config": backendTarget,
      "/health": backendTarget,
      "/start": backendTarget,
      "/stop": backendTarget,
    },
  },
})
