import { defineConfig, devices } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

const runtimeRoot = path.join(tmpdir(), 'vid2gif-playwright');
const stateRoot = path.join(runtimeRoot, 'state');
const libraryRoot = path.join(runtimeRoot, 'library');
mkdirSync(stateRoot, { recursive: true });
mkdirSync(libraryRoot, { recursive: true });

const python = process.env.VID2GIF_TEST_PYTHON
  || (process.platform === 'win32'
    ? path.resolve('.venv/Scripts/python.exe')
    : 'python');
const serverCommand = `"${python}" -c "from app.routes import app; app.run(host='127.0.0.1', port=19040, debug=False, threaded=True)"`;

export default defineConfig({
  testDir: './frontend/browser',
  fullyParallel: false,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: process.env.CI ? 'github' : 'list',
  use: {
    baseURL: 'http://127.0.0.1:19040',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },
  webServer: {
    command: serverCommand,
    url: 'http://127.0.0.1:19040/gifs',
    reuseExistingServer: !process.env.CI,
    timeout: 30_000,
    env: {
      ...process.env,
      STATE_ROOT: stateRoot,
      LIB_ROOT: libraryRoot,
    },
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
});
