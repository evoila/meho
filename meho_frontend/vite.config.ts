import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    host: '0.0.0.0', // Allow external access (for browser automation)
    port: 5173,
    strictPort: true,
    hmr: {
      clientPort: 5173, // Ensure HMR works correctly
    },
    watch: {
      usePolling: false, // Use native file watching (faster on macOS)
    },
  },
  // Environment variables
  define: {
    'import.meta.env.VITE_API_URL': JSON.stringify(
      process.env.VITE_API_URL || 'http://localhost:8000'
    ),
  },
})
