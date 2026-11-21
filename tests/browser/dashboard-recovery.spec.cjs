// Claude-authored browser cases (21 November 2026) for backlog 7.QA: offline assets and
// failure/reconnect behaviour of the dashboard against REAL processes, with no Playwright
// route between the page and the server. PRD FR-U1 (the board shows a lost connection
// and recovers), FR-U6 (history), FR-U8 (same origin, no internet dependency) and the
// product constraint "no runtime downloads, telemetry or CDN assets". Authored by Claude
// (Claude Code), not GPT-6 Astra; extends the route/mock reconnect cases LB07, LB10, LB12
// and HM02, which fake the failure, with a real analysis API process that is killed and
// started again on the same port (the page keeps its origin).
//
// RC01 and RC03 need only the built dashboard and uv; RC02 also needs
// AVA_TEST_POSTGRES_DSN naming a disposable PostgreSQL database (services/store/README.md)
// and is skipped without it. Infrastructure evidence only: no detector is involved.
// Build first: `npm run build --prefix dashboard`.
const { test, expect } = require('@playwright/test');
const fs = require('node:fs');
const path = require('node:path');
const { DIST, freePort, startApi } = require('./support/api-server.cjs');

const DSN_ENV = 'AVA_TEST_POSTGRES_DSN';

function requireBuild() {
  if (!fs.existsSync(path.join(DIST, 'index.html'))) throw new Error('dashboard/dist missing: run `npm run build --prefix dashboard`');
}

async function control(base, pathname, body) {
  const response = await fetch(`${base}${pathname}`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) });
  const json = await response.json();
  if (!response.ok) throw new Error(`control ${pathname} refused: ${json.error}`);
  return json;
}

/** True once nothing answers on `base` any more (the killed server released the port). */
async function unreachable(base) {
  try {
    await fetch(`${base}/healthz`, { signal: AbortSignal.timeout(2000) });
    return false;
  } catch {
    return true;
  }
}

function watch(page, base) {
  const offOrigin = [];
  page.on('request', request => {
    const url = new URL(request.url());
    if (url.host !== new URL(base).host && !['data:', 'blob:'].includes(url.protocol)) offOrigin.push(request.url());
  });
  const errors = [];
  page.on('pageerror', error => errors.push(String(error)));
  const sockets = [];
  page.on('websocket', ws => sockets.push(ws.url()));
  return { offOrigin, errors, sockets };
}

test.describe('RC01 live board across a real API crash and restart', () => {
  let server;

  test.afterAll(async () => { if (server) await server.stop(); });

  test('RC01 FR-U1: the lost connection is shown, retried while the API is down, and the restarted API\'s calls replace the board', async ({ page }) => {
    test.setTimeout(240000);
    requireBuild();
    let controlBase = `http://127.0.0.1:${await freePort()}`;
    server = await startApi({ serverArgs: ['--control-port', new URL(controlBase).port] });
    const { base, port } = server;
    const publish = (type, callId, data = {}) => control(controlBase, '/publish', { type, call_id: callId, data });
    const score = (callId, value, extra = {}) => publish('score.updated', callId, {
      score: value, ema: value, state: 'monitoring', degraded: false, effective_stride_s: 2.0, quality: 'ok', direction: 'caller', ...extra,
    });
    const card = callId => page.locator(`[data-testid="call-card"][data-call-id="${callId}"]`);
    const { offOrigin, errors, sockets } = watch(page, base);

    await page.goto(`${base}/#live`);
    await expect(page.getByTestId('connection')).toHaveText('Live');
    await publish('call.started', 'rc01-old');
    await score('rc01-old', 0.3);
    await score('rc01-old', 0.4, { ema: 0.35 });
    await expect(card('rc01-old').getByTestId('sparkline')).toHaveAttribute('data-points', '2');
    await publish('call.started', 'rc01-cont');
    await score('rc01-cont', 0.9, { state: 'alert', ema: 0.8 });
    await publish('alert.raised', 'rc01-cont', { alert_id: 'rc01-a1', direction: 'caller', ema: 0.8, window: 1 });
    const alertItem = page.locator('[data-testid="alert-item"][data-alert-id="rc01-a1"]');
    await expect(alertItem).toContainText('Alert');
    expect(sockets.length).toBe(1);

    // The second process's control port is chosen while the first still holds `port`,
    // so the two cannot collide (review L6).
    const nextControl = `http://127.0.0.1:${await freePort()}`;
    // The API process dies abruptly (process tree or group killed, no graceful
    // shutdown): the page sees a real close, not one a route made up.
    await server.stop();
    await expect.poll(() => unreachable(base), { timeout: 15000 }).toBe(true);
    const status = page.getByTestId('connection');
    await expect(status).toContainText('Live updates disconnected:');
    await expect(status).toContainText('The board may be out of date.');
    // The last known state stays on screen, marked out of date, rather than vanishing.
    await expect(card('rc01-old').getByTestId('call-state')).toHaveText('Monitoring');
    await expect(card('rc01-old').getByTestId('call-confidence')).toHaveText('0.35 (smoothed, uncalibrated)');
    // While nothing listens the page keeps trying (back-off 1 s, 2 s, ... with jitter):
    // at least two further attempts, each failing, and the board still says so.
    await expect.poll(() => sockets.length, { timeout: 15000 }).toBeGreaterThanOrEqual(3);
    await expect(status).toContainText('Live updates disconnected:');
    // During the outage the board keeps the alert as it was; it is marked unconfirmed
    // only once a snapshot from the restarted API cannot confirm it (below).
    await expect(alertItem).toHaveClass('alert-open');

    // A new API process on the same port: an empty registry, as after any API restart.
    controlBase = nextControl;
    server = await startApi({ port, serverArgs: ['--control-port', new URL(controlBase).port] });
    await publish('call.started', 'rc01-new');
    await score('rc01-new', 0.6, { state: 'suspicious' });
    // No reload: the page reconnects by itself within its back-off (at most 12.5 s a step).
    await expect(status).toHaveText('Live', { timeout: 30000 });
    // The snapshot of the restarted API is the truth: its call is shown, and the call
    // only the dead process knew is no longer listed as active.
    await expect(card('rc01-new').getByTestId('call-state')).toHaveText('Suspicious');
    await expect(page.getByTestId('active-calls').locator('[data-call-id="rc01-old"]')).toHaveCount(0);
    // Live events from the new process reach the page.
    await score('rc01-new', 0.7, { state: 'suspicious' });
    await expect(card('rc01-new').getByTestId('sparkline')).toHaveAttribute('data-points', '2');
    // The live process outlives an API restart, so a call it was scoring continues on the
    // new API with no fresh start: the restarted registry recreates it implicitly and
    // knows none of its alerts. Its alert was never cleared, so no later read may show
    // it as cleared.
    await score('rc01-cont', 0.9, { state: 'alert', ema: 0.85 });
    await expect(card('rc01-cont').getByTestId('call-state')).toHaveText('Alert');
    await control(controlBase, '/loss', { missing: 1 }); // forces a REST re-read (stream.gap)
    await score('rc01-cont', 0.9, { state: 'alert', ema: 0.86 }); // marker after the re-read
    await expect(card('rc01-cont').getByTestId('call-confidence')).toHaveText('0.86 (smoothed, uncalibrated)');
    await expect(alertItem).toHaveClass('alert-open unconfirmed');
    await expect(alertItem.getByTestId('alert-unconfirmed')).toHaveText('(not confirmed by the service since reconnecting)');
    await expect(alertItem).not.toContainText('Cleared');
    // When the live process does clear it, the page shows that.
    await publish('alert.cleared', 'rc01-cont', { alert_id: 'rc01-a1', direction: 'caller', ema: 0.3, window: 9 });
    await expect(alertItem).toHaveClass('alert-cleared');
    expect(new Set(sockets)).toEqual(new Set([`${base.replace('http', 'ws')}/v1/events`]));
    expect(offOrigin).toEqual([]);
    expect(errors).toEqual([]);
    await publish('call.ended', 'rc01-new', { final_state: 'suspicious', reason: 'bye', speech_seconds: 3.0 });
  });
});

test.describe('RC02 history across a real API crash and restart', () => {
  let server;
  let controlBase;

  test.afterAll(async () => {
    if (controlBase) await control(controlBase, '/drop-schema', {}).catch(() => undefined);
    if (server) await server.stop();
  });

  test('RC02 FR-U6: a dead API is reported on refresh, old rows are not shown as current, and the restarted API is read without a reload', async ({ page }) => {
    test.skip(!process.env[DSN_ENV], `set ${DSN_ENV} to a disposable PostgreSQL 16 database`);
    test.setTimeout(300000);
    requireBuild();
    const start = async port => {
      controlBase = `http://127.0.0.1:${await freePort()}`;
      return startApi({ port, uvArgs: ['--with-requirements', 'requirements-store.txt'],
                        serverArgs: ['--control-port', new URL(controlBase).port, '--history-dsn-env', DSN_ENV] });
    };
    const T0 = 1763600000;
    const stored = async (callId, finalState) => {
      for (const [seq, type, data, ts] of [
        [1, 'call.started', {}, T0],
        [2, 'call.ended', { final_state: finalState, reason: 'bye', speech_seconds: 4.0, verdict: 'genuine', ended_open_alert_ids: {} }, T0 + 9],
      ]) {
        const { outcome } = await control(controlBase, '/store', { type, call_id: callId, data, seq, ts });
        expect(outcome).toBe('stored');
      }
    };
    const rows = () => page.getByTestId('call-row').evaluateAll(r => r.map(x => x.dataset.callId));

    server = await start(undefined);
    const { base, port } = server;
    const { offOrigin, errors, sockets } = watch(page, base);
    await stored('rc02-before', 'monitoring');
    await page.goto(`${base}/#history`);
    await expect.poll(rows).toEqual(['rc02-before']);
    await expect(page.getByTestId('alerts-status')).not.toContainText('Loading');

    // The first process's schema is dropped before it dies, so the database keeps no
    // leftovers; the page has already rendered its rows.
    await control(controlBase, '/drop-schema', {});
    controlBase = undefined;
    await server.stop();
    await expect.poll(() => unreachable(base), { timeout: 15000 }).toBe(true);

    await page.getByRole('button', { name: 'Refresh' }).click();
    await expect(page.getByTestId('calls-error')).toHaveText('The history service could not be reached.');
    await expect(page.getByTestId('call-row')).toHaveCount(0); // nothing shown as if it were current
    await page.getByRole('button', { name: 'Search' }).click();
    await expect(page.getByTestId('alerts-error')).toHaveText('The history service could not be reached.');

    server = await start(port);
    await stored('rc02-after', 'alert');
    await page.getByRole('button', { name: 'Refresh' }).click();
    await expect.poll(rows).toEqual(['rc02-after']);
    await expect(page.getByTestId('calls-error')).toHaveCount(0);
    await page.getByRole('button', { name: 'Search' }).click();
    await expect(page.getByTestId('alerts-error')).toHaveCount(0);
    await expect(page.getByTestId('alerts-status')).not.toContainText('Loading');
    expect(sockets).toEqual([]); // the history page opens no live socket
    expect(offOrigin).toEqual([]);
    expect(errors).toEqual([]);
  });
});

test.describe('RC03 offline assets', () => {
  // Serial only here (one server for both cases); a failure in RC01/RC02 does not skip
  // these (review L4).
  test.describe.configure({ mode: 'serial' });
  let server;

  test.beforeAll(async () => {
    test.setTimeout(180000);
    requireBuild();
    server = await startApi();
  });

  test.afterAll(async () => { if (server) await server.stop(); });

  // Review L1: every absolute URL literal anywhere in the built files, not only those
  // passed straight to a loader, plus protocol-relative ones in strings, CSS url() and
  // HTML attributes. Only names that browsers never fetch are allowed: XML namespaces
  // and React's error-decoder link (text in an error message).
  const ABSOLUTE_URL = /\b(?:https?|wss?|ftp):\/\/[^\s"'`)<>\\]*/gi;
  const RELATIVE_HOST = /(?:["'`=(]\s*)\/\/[a-z0-9-]+(?:\.[a-z0-9-]+)+/gi;
  const NEVER_FETCHED = [
    /^http:\/\/www\.w3\.org\/(?:1999\/xlink|XML\/1998\/namespace|2000\/svg|1998\/Math\/MathML|1999\/xhtml)$/,
    /^https:\/\/reactjs\.org\/docs\/error-decoder\.html\?invariant=$/,
  ];
  const offOrigin = text => [...text.matchAll(ABSOLUTE_URL)].map(m => m[0]).filter(u => !NEVER_FETCHED.some(r => r.test(u)))
    .concat([...text.matchAll(RELATIVE_HOST)].map(m => m[0]));

  test('RC03a the built files reference nothing outside the origin; the served page forbids other origins', async () => {
    const files = [path.join(DIST, 'index.html'), ...fs.readdirSync(path.join(DIST, 'assets')).map(f => path.join(DIST, 'assets', f))];
    for (const f of fs.readdirSync(DIST)) if (f !== 'assets' && f !== 'index.html') files.push(path.join(DIST, f));
    expect(files.filter(f => f.endsWith('.js')).length).toBeGreaterThan(0);
    expect(files.filter(f => f.endsWith('.css')).length).toBeGreaterThan(0);
    const found = [];
    for (const file of files) {
      if (!/\.(?:js|css|html)$/.test(file)) { found.push(`${path.basename(file)}: unexpected file type`); continue; }
      for (const url of offOrigin(fs.readFileSync(file, 'utf8'))) found.push(`${path.basename(file)}: ${url}`);
    }
    expect(found).toEqual([]);
    // Controls, so a broken pattern cannot pass silently.
    for (const bad of ['fetch("https://cdn.example/x")', 'import{a}from"https://esm.example/a.js"', 'new Worker(`//cdn.example/w.js`)',
      'const u="wss://events.example/s";', 'background:url(//cdn.example/a.png)', '<link href=//fonts.example/a.css>',
      '<meta http-equiv="refresh" content="0;url=https://elsewhere.example/">', 'el.src="//t.example/p.gif"',
      'http://www.w3.org/2000/svg/../x']) {
      expect(offOrigin(bad), bad).not.toEqual([]);
    }
    for (const fine of ['<script src="/assets/index.js">', 'createElementNS("http://www.w3.org/2000/svg","svg")',
      'a = b // a comment', 'x = 1 //y']) {
      expect(offOrigin(fine), fine).toEqual([]);
    }

    const html = await (await fetch(`${server.base}/`)).text();
    const meta = html.match(/<meta\s+http-equiv="Content-Security-Policy"\s+content="([^"]+)"/i);
    expect(meta, 'the served page carries its CSP').not.toBeNull();
    const directives = Object.fromEntries(meta[1].split(';').map(d => d.trim()).filter(Boolean)
      .map(d => { const [name, ...sources] = d.split(/\s+/); return [name, sources]; }));
    const allowed = new Set(["'self'", "'none'", 'data:', 'blob:']);
    for (const [name, sources] of Object.entries(directives)) {
      for (const source of sources) expect(allowed.has(source), `${name} allows ${source}`).toBe(true);
    }
    expect(directives['default-src']).toEqual(["'self'"]);
    expect(directives['connect-src']).toEqual(["'self'"]);
    expect(directives['script-src']).toEqual(["'self'"]);
  });

  test('RC03b every page loads fully with every other origin unreachable; nothing is fetched from elsewhere', async ({ browser }) => {
    const context = await browser.newContext({ serviceWorkers: 'block' });
    const base = server.base;
    const blocked = [];
    await context.route('**/*', route => {
      const url = new URL(route.request().url());
      if (url.host !== new URL(base).host) { blocked.push(url.href); return route.abort('internetdisconnected'); }
      return route.continue();
    });
    // Review M4: the page's own CSP stops an off-origin request inside the renderer,
    // before the route above sees it, so CSP violations are recorded as well.
    const violations = [];
    await context.exposeFunction('__cspViolation', text => violations.push(text));
    await context.addInitScript(() => {
      document.addEventListener('securitypolicyviolation', e => window.__cspViolation(`${e.violatedDirective} ${e.blockedURI}`));
    });
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(String(error)));
    page.on('console', message => { if (/Content Security Policy/i.test(message.text())) violations.push(message.text()); });
    const failedAssets = [];
    page.on('response', response => {
      const type = response.request().resourceType();
      if (['document', 'script', 'stylesheet', 'font', 'image', 'media'].includes(type) && response.status() >= 400) {
        failedAssets.push(`${response.status()} ${response.url()}`);
      }
    });
    // Only same-origin socket and fetch failures are expected (this server has no live
    // source); any other failed request is a finding.
    page.on('requestfailed', request => {
      const sameOrigin = new URL(request.url()).host === new URL(base).host;
      if (!(sameOrigin && ['websocket', 'fetch'].includes(request.resourceType()))) failedAssets.push(`failed ${request.url()}`);
    });
    try {
      await page.goto(`${base}/`, { waitUntil: 'networkidle' });
      await expect(page.getByRole('heading', { name: 'AVA batch analysis' })).toBeVisible();
      await expect(page.getByTestId('dropzone')).toBeVisible();
      // The stylesheet applied: its rules reached the page from the origin.
      expect(await page.evaluate(() => [...document.styleSheets].reduce((n, s) => n + s.cssRules.length, 0))).toBeGreaterThan(0);
      await page.getByRole('link', { name: 'Live calls' }).click();
      await expect(page.getByTestId('connection')).toBeVisible();
      await page.getByRole('link', { name: 'History' }).click();
      await expect(page.getByRole('heading', { name: 'AVA call history' })).toBeVisible();
      await expect(page.getByTestId('calls-error')).toBeVisible(); // this server has no store: an answer, not a hang
      await expect(page.getByTestId('alerts-error')).toBeVisible();
      // Violations reach the test asynchronously: let the page settle first (re-review).
      await page.waitForLoadState('networkidle');
      await page.waitForTimeout(500);
      expect(blocked).toEqual([]);
      expect(violations).toEqual([]);
      expect(failedAssets).toEqual([]);
      expect(errors).toEqual([]);
      // Control: an off-origin fetch made in the page is caught by these listeners.
      await page.evaluate(() => fetch('https://cdn.example/x').catch(() => undefined));
      await expect.poll(() => violations.length).toBeGreaterThan(0);
      expect(violations.join(' ')).toContain('cdn.example');
    } finally {
      await context.close();
    }
  });
});
