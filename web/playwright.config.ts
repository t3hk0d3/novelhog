import { defineConfig, devices } from '@playwright/test';

const PORT = 8765;
const schemes = ['light', 'dark'] as const;
const devicesByName = {
  desktop: devices['Desktop Chrome'],
  phone: devices['Pixel 7'],
};

/**
 * End-to-end smoke tests (spec section 13) against `tests/fixtures/fake_backend.py`: the real API
 * and worker on a temporary database serving the built SPA. Run with `npm run e2e`; they are not
 * part of `npm run check`. One backend serves every project, so the tests run one at a time.
 */
export default defineConfig({
  testDir: './e2e',
  workers: 1,
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  reporter: process.env.CI ? 'github' : 'list',
  timeout: 60_000,
  expect: { timeout: 10_000 },
  use: { baseURL: `http://127.0.0.1:${String(PORT)}`, trace: 'retain-on-failure' },
  projects: Object.entries(devicesByName).flatMap(([name, device]) =>
    schemes.map((colorScheme) => ({
      name: `${name}-${colorScheme}`,
      use: { ...device, colorScheme },
    })),
  ),
  webServer: {
    command: `npm run build && cd .. && uv run python -m tests.fixtures.fake_backend --dist web/dist --port ${String(PORT)}`,
    url: `http://127.0.0.1:${String(PORT)}/`,
    reuseExistingServer: false,
    timeout: 180_000,
    gracefulShutdown: { signal: 'SIGTERM', timeout: 10_000 },
  },
});
