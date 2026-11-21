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
  // The dashboard cases need a built `dashboard/dist`, uv and requirements-api.txt; the
  // real-model cases also need the local checkpoint and public clips named in the
  // gitignored `.local/real-model-qa.json`. Neither runs in the foundation (CI) job.
  projects: [
    { name: 'foundation', testIgnore: ['**/dashboard-*.spec.cjs', '**/real-model-*.spec.cjs'] },
    { name: 'dashboard', testMatch: '**/dashboard-*.spec.cjs' },
    // No traces: they would store the uploaded public audio as request bodies.
    { name: 'real-model', testMatch: '**/real-model-*.spec.cjs', timeout: 300000,
      use: { browserName: 'chromium', trace: 'off', screenshot: 'only-on-failure', video: 'off' } },
  ],
});
