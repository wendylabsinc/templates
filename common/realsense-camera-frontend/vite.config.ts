import path from "path"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"

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
        target: "http://localhost:8000",
        changeOrigin: true,
      },
      "/config": "http://localhost:8000",
      "/health": "http://localhost:8000",
      "/start": "http://localhost:8000",
      "/stop": "http://localhost:8000",
    },
  },
})
