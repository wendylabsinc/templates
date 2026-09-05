import { defineConfig } from 'vite'

// Match the generated backend port. Change this URL for a remote backend.
export default defineConfig({
  server: { proxy: { '/api': 'http://localhost:{{.PORT}}' } },
})
