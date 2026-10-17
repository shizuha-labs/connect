import { defineConfig, mergeConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { createShizuhaConfig } from '@shizuha/ui/vite'

const basePath = process.env.VITE_BASE_PATH || '/'

export default mergeConfig(
  createShizuhaConfig({ port: 80, serviceName: 'connect' }),
  defineConfig({
    plugins: [react()],
    server: {
      proxy: basePath === '/' ? {
        '/api': {
          target: process.env.VITE_API_URL || 'http://localhost:8019',
          changeOrigin: true,
        },
        '/ws': {
          target: process.env.VITE_WS_URL || 'ws://localhost:8019',
          ws: true,
        },
      } : {},
    },
  })
)
