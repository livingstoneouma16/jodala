import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The built app is served by Flask at /v3 (see app/routes/v3.py), so every
// asset URL needs that base baked in. In dev, API calls proxy straight to
// the Flask backend on :5000 so you can `npm run dev` and `python run.py`
// side by side without CORS headaches.
export default defineConfig({
  base: '/v3/',
  plugins: [react()],
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      '/auth': 'http://localhost:5000',
      '/members': 'http://localhost:5000',
      '/clients': 'http://localhost:5000',
      '/loans': 'http://localhost:5000',
      '/repayments': 'http://localhost:5000',
      '/savings': 'http://localhost:5000',
      '/accounting': 'http://localhost:5000',
      '/reports': 'http://localhost:5000',
      '/settings': 'http://localhost:5000',
      '/dashboard': 'http://localhost:5000',
      '/notifications': 'http://localhost:5000',
    },
  },
})
