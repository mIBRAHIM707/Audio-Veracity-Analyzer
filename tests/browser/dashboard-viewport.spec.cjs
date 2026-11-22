// Claude-authored browser cases (22 November 2026) for PRD §6.6 "The dashboard renders
// correctly at 1366×768 and 1920×1080", found untested by backlog 11.QA
// (docs/qa/acceptance-separation.md, row 6.6.2). Authored by Claude (Claude Code), not
// GPT-6 Astra.
//
// "Renders correctly" is checked as layout properties a browser can measure at each PRD
// size: no horizontal scrolling, every element that carries a test ID lies inside the
// viewport's width, no such element clips its own text, and the alert banner and feed
// are on the first screen. The page is the production build served by the real analysis
// API (tests/browser/serve_dashboard_api.py) with the deterministic fake detector; live
// events enter through the hub's validation from the loopback control server. Visual
// taste is not checked, and nothing here is evidence about detection.
// Build first: `npm run build --prefix dashboard`.
const { test, expect } = require('@playwright/test');
const fs = require('node:fs');
const path = require('node:path');
const { DIST, freePort, startApi, tones } = require('./support/api-server.cjs');

const SIZES = [{ width: 1366, height: 768 }, { width: 1920, height: 1080 }];
let server;
let base;
let control;

test.describe.configure({ mode: 'serial' });

test.beforeAll(async () => {
  test.setTimeout(180000);
  if (!fs.existsSync(path.join(DIST, 'index.html'))) throw new Error('dashboard/dist missing: run `npm run build --prefix dashboard`');
  const controlPort = await freePort();
  server = await startApi({ serverArgs: ['--control-port', String(controlPort)] });
  base = server.base;
  control = `http://127.0.0.1:${controlPort}`;
});

test.afterAll(async () => { if (server) await server.stop(); });

async function post(pathname, body) {
  const response = await fetch(`${control}${pathname}`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) });
  const json = await response.json();
  if (!response.ok) throw new Error(`control ${pathname} refused: ${json.error}`);
  return json;
}
const publish = (type, callId, data = {}) => post('/publish', { type, call_id: callId, data });
const score = (callId, value, extra = {}) => publish('score.updated', callId, {
  score: value, ema: value, state: 'monitoring', degraded: false, effective_stride_s: 2.0, quality: 'ok', direction: 'caller', ...extra,
});

/** Layout problems at the current size, as readable strings; [] when none. */
async function layoutProblems(page) {
  return page.evaluate(() => {
    const problems = [];
    const width = window.innerWidth;
    const root = document.documentElement;
    if (root.scrollWidth > width) problems.push(`page scrolls sideways: ${root.scrollWidth} > ${width}`);
    for (const el of document.querySelectorAll('[data-testid]')) {
      const box = el.getBoundingClientRect();
      const style = getComputedStyle(el);
      if (box.width === 0 || box.height === 0 || style.visibility === 'hidden') continue;
      const id = `${el.getAttribute('data-testid')}${el.dataset.callId ? `[${el.dataset.callId}]` : ''}`;
      if (box.left < -0.5 || box.right > width + 0.5) problems.push(`${id} outside the width: ${Math.round(box.left)}..${Math.round(box.right)} of ${width}`);
      if (el.scrollWidth > el.clientWidth + 1 && style.overflowX !== 'auto' && style.overflowX !== 'scroll') {
        problems.push(`${id} clips its content: ${el.scrollWidth} > ${el.clientWidth}`);
      }
    }
    return problems;
  });
}

test('VP00 the layout check reports a too-wide element, a clipped one and sideways scrolling', async ({ page }) => {
  await page.setViewportSize(SIZES[0]);
  await page.goto(`${base}/#history`);
  await expect(page.getByTestId('history-calls')).toBeVisible();
  expect(await layoutProblems(page)).toEqual([]);
  await page.evaluate(() => {
    const wide = document.createElement('div');
    wide.dataset.testid = 'probe-wide';
    wide.style.width = '2000px';
    wide.textContent = 'wide';
    const clipped = document.createElement('div');
    clipped.dataset.testid = 'probe-clipped';
    Object.assign(clipped.style, { width: '40px', overflow: 'hidden', whiteSpace: 'nowrap' });
    clipped.textContent = 'a label far longer than forty pixels';
    document.querySelector('main').append(wide, clipped);
  });
  const found = await layoutProblems(page);
  expect(found.some(item => item.startsWith('page scrolls sideways'))).toBe(true);
  expect(found.some(item => item.startsWith('probe-wide outside the width'))).toBe(true);
  expect(found.some(item => item.startsWith('probe-clipped clips its content'))).toBe(true);
});

for (const size of SIZES) {
  const label = `${size.width}x${size.height}`;

  test.describe(label, () => {
    test.use({ viewport: size });

    test(`VP01 §6.6/FR-U1/U2 at ${label}: eight live calls, one alerting, fit the width; the alert is on the first screen`, async ({ page }) => {
      await page.goto(`${base}/#live`);
      await expect(page.getByTestId('connection')).toHaveText('Live');
      expect(await page.evaluate(() => [window.innerWidth, window.innerHeight])).toEqual([size.width, size.height]);
      // Eight calls: the PRD §6.1 concurrency target. Long IDs and every flag stress the
      // card layout; one call is in alert with an open alert.
      const ids = Array.from({ length: 8 }, (_, n) => `vp01-${label}-call-${String(n).padStart(2, '0')}-0123456789abcdef`);
      for (const id of ids) {
        await publish('call.started', id);
        await score(id, 0.3);
      }
      await score(ids[1], null, { ema: null, quality: 'low', degraded: true, effective_stride_s: 8.0 });
      await score(ids[2], 0.6, { state: 'suspicious', ema: 0.6 });
      const alerting = ids[5];
      await score(alerting, 0.95, { state: 'alert', ema: 0.9 });
      await publish('alert.raised', alerting, { alert_id: `${alerting}-a1`, direction: 'caller', ema: 0.9, window: 3 });
      await expect(page.locator('[data-testid="active-calls"] [data-testid="call-card"]')).toHaveCount(8);
      await expect(page.getByTestId('alert-banner')).toContainText(`New alert on call ${alerting}`);
      await expect(page.getByTestId('alert-banner')).toBeInViewport();
      await expect(page.locator(`[data-testid="alert-item"][data-alert-id="${alerting}-a1"]`)).toBeInViewport();
      expect(await layoutProblems(page)).toEqual([]);
      for (const id of ids) await publish('call.ended', id, { final_state: 'monitoring', reason: 'bye', speech_seconds: 10 });
      await expect(page.getByTestId('no-active')).toBeVisible();
      expect(await layoutProblems(page)).toEqual([]);
    });

    test(`VP02 §6.6/FR-U3/U4 at ${label}: a batch result and its timeline fit the width`, async ({ page }) => {
      await page.goto(`${base}/`);
      // 300 Hz scores genuine and 3000 Hz synthetic with the fixture detector. Segment
      // edges on the 2 s hop keep every window's mix at 0, 1/2 or 1, away from the
      // fixture's deliberate fault band (a window a quarter synthetic raises).
      const segments = [];
      for (let n = 0; n < 4; n += 1) segments.push({ seconds: 8, hz: n % 2 ? 3000 : 300 });
      await page.getByLabel('Choose a file').setInputFiles({ name: 'viewport-mixed.wav', mimeType: 'audio/wav', buffer: tones(segments, 8000) });
      await expect(page.getByTestId('result')).toBeVisible();
      await expect(page.getByTestId('verdict')).toBeInViewport();
      const bars = page.getByTestId('timeline-bar');
      expect(await bars.count()).toBeGreaterThan(10);
      expect(await layoutProblems(page)).toEqual([]);
    });

    test(`VP03 §6.6/FR-U6 at ${label}: the history page without a store fits the width`, async ({ page }) => {
      await page.goto(`${base}/#history`);
      await expect(page.getByRole('link', { name: 'History' })).toHaveAttribute('aria-current', 'page');
      await expect(page.getByTestId('history-calls')).toBeVisible();
      expect(await layoutProblems(page)).toEqual([]);
    });
  });
}
