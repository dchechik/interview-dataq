import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    // Dev runs Vite and uvicorn separately; prod serves the built bundle from
    // FastAPI, so the app always talks to a same-origin /api.
    proxy: { '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true } },
  },
  // maplibre-gl ships its own Web Worker. Vite's dep pre-bundling rewrites the
  // worker URL to a path it does not actually serve, so the worker 404s in dev.
  // GeoJSON sources are tiled *in that worker*, so the basemap (raster, no
  // worker) renders while every data layer silently draws nothing.
  optimizeDeps: { exclude: ['maplibre-gl'] },
  build: { outDir: 'dist', sourcemap: true },
})
