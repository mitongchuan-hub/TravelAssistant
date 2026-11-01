import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
  server: {
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: false },
    },
  },
  test: { environment: 'happy-dom', include: ['src/**/*.test.ts', 'tests/**/*.test.ts'], clearMocks: true },
})
