import { defineConfig, devices } from '@playwright/test';

const PORT = 8766;
const schemes = ['light', 'dark'] as const;
const devicesByName = {
  desktop: { ...devices['Desktop Chrome'], viewport: { width: 1280, height: 800 } },
  mobile: { ...devices['Pixel 7'], viewport: { width: 390, height: 844 }, deviceScaleFactor: 2 },
};

/**
 * The documentation screenshots: `e2e/screenshots.ts` against `tests/fixtures/demo_backend.py`, a
 * library of public-domain novels. Run with `npm run screenshots`; the PNGs land in
 * `docs/assets/screenshots/`. Not part of `npm run e2e` or CI.
 */
export default defineConfig({
  testDir: './e2e',
  testMatch: 'screenshots.ts',
  workers: 1,
  fullyParallel: false,
  reporter: 'list',
  timeout: 180_000,
  expect: { timeout: 15_000 },
  use: { baseURL: `http://127.0.0.1:${String(PORT)}` },
  projects: Object.entries(devicesByName).flatMap(([name, device]) =>
    schemes.map((colorScheme) => ({
      name: `${name}-${colorScheme}`,
      use: { ...device, colorScheme },
    })),
  ),
  webServer: {
    command: `npm run build && cd .. && uv run python -m tests.fixtures.demo_backend --dist web/dist --port ${String(PORT)}`,
    url: `http://127.0.0.1:${String(PORT)}/`,
    reuseExistingServer: false,
    timeout: 180_000,
    gracefulShutdown: { signal: 'SIGTERM', timeout: 10_000 },
  },
});
