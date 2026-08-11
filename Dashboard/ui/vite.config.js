import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    // Proxy to the FastAPI brain so the browser sees one origin and CORS
    // never enters the picture.
    proxy: {
      '/api': {
        // 127.0.0.1, not localhost: uvicorn binds IPv4 only, while Node
        // resolves localhost to ::1 first on macOS.
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
      // No rewrite here: the backend route really is /ws/dialogue.
      '/ws': {
        // 127.0.0.1, not localhost: uvicorn binds IPv4 only, while Node
        // resolves localhost to ::1 first on macOS.
        target: 'http://127.0.0.1:8000',
        ws: true,
      },
    },
  },
})
