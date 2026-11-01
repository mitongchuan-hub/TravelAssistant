import { defineConfig } from '@playwright/test'
import { fileURLToPath } from 'node:url'

const root = fileURLToPath(new URL('..', import.meta.url))
export default defineConfig({
  testDir: './tests/e2e',
  fullyParallel: false,
  workers: 1,
  timeout: 60000,
  expect: { timeout: 12000 },
  reporter: 'list',
  outputDir: './test-results',
  use: {
    baseURL: 'http://127.0.0.1:8765',
    channel: 'chrome',
    headless: true,
    viewport: { width: 430, height: 932 },
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  webServer: {
    command: '"E:/anaconda/envs/TravelAssistant/python.exe" tests/browser/server.py',
    cwd: root,
    url: 'http://127.0.0.1:8765/health',
    reuseExistingServer: false,
    timeout: 30000,
  },
})
