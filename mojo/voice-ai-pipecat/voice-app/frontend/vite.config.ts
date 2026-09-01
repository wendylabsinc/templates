import path from "path"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"

// https://vite.dev/config/
//
// Standalone dev (`npm run dev`) serves the React app on Vite's port
// (5173 by default), but the app also calls /api/* and /bot-audio on
// the same origin. Without a proxy those hit Vite (404) instead of the
// Pipecat backend. Set DEV_BACKEND_URL when you start `npm run dev` to
// point both fetches and the WebSocket at the running backend, e.g.
//
//   DEV_BACKEND_URL=https://localhost:3005 npm run dev
//
// The dev server accepts the backend's self-signed cert because we
// disable origin TLS verification (secure: false) for the proxy only.
const devBackend = process.env.DEV_BACKEND_URL ?? "https://localhost:3005"
const wsBackend = devBackend.replace(/^http/, "ws")

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    proxy: {
      "/api": {
        target: devBackend,
        changeOrigin: true,
        secure: false,
      },
      "/bot-audio": {
        target: wsBackend,
        ws: true,
        changeOrigin: true,
        secure: false,
      },
    },
  },
})
