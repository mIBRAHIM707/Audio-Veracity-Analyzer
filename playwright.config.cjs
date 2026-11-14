const { defineConfig } = require('@playwright/test');

module.exports = defineConfig({
  testDir: './tests/browser',
  timeout: 30000,
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  workers: process.env.CI ? 2 : undefined,
  reporter: [['list'], ['html', { open: 'never' }]],
  outputDir: 'test-results',
  use: { browserName: 'chromium', trace: 'retain-on-failure', screenshot: 'only-on-failure' },
  // The dashboard cases need a built `dashboard/dist`, uv and requirements-api.txt;
  // they run only through `npm run test:dashboard`, never in the foundation job.
  projects: [
    { name: 'foundation', testIgnore: '**/dashboard-*.spec.cjs' },
    { name: 'dashboard', testMatch: '**/dashboard-*.spec.cjs' },
  ],
});
