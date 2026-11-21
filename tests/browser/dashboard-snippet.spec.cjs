// Claude-authored browser cases (19 November 2026, attempt 10) for backlog 7.3 / PRD FR-U5
// ("call detail view with ... playback of retained snippets if retention is on; an
// expired or missing snippet shows a clear message"), with FR-O5 (encrypted at rest,
// TTL), FR-U7 (signed-in users only) and §6.3 (audio exports are audited).
// Authored by Claude (Claude Code), not GPT-6 Astra.
//
// SB cases: the production build served by the real analysis API with the production
// accounts (serve_dashboard_api.py --auth --snippets) in a fresh schema of a disposable
// PostgreSQL database; alerts written by the production `PostgresEventStore`; snippets
// written by the production AES-256-GCM `SnippetStore` (synthetic tones, random key in a
// temp folder) and read by the production `SnippetReader`. They need
// AVA_TEST_POSTGRES_DSN and are skipped without it. SN cases need no database: the real
// API without accounts, with history and snippet answers [mock] (Playwright route).
// Infrastructure evidence only: no detector and no real audio are involved.
// Build first: `npm run build --prefix dashboard`.
const { test, expect } = require('@playwright/test');
const fs = require('node:fs');
const path = require('node:path');
const { DIST, freePort, startApi } = require('./support/api-server.cjs');

const DSN_ENV = 'AVA_TEST_POSTGRES_DSN';
const PASSWORD = 'browser-test-password-2026'; // serve_dashboard_api.AUTH_TEST_PASSWORD (fictional)
const T0 = 1763600000;

test.describe.configure({ mode: 'serial' });

function requireBuild() {
  if (!fs.existsSync(path.join(DIST, 'index.html'))) throw new Error('dashboard/dist missing: run `npm run build --prefix dashboard`');
}

function watch(page, base) {
  const seen = { api: [], offOrigin: [], errors: [], csp: [] };
  // The page's CSP once refused `blob:` audio (media-src fell back to 'self'): any refusal is a failure.
  page.on('console', message => { if (/Content Security Policy/i.test(message.text())) seen.csp.push(message.text()); });
  page.on('request', request => {
    const url = new URL(request.url());
    if (url.host !== new URL(base).host && !['data:', 'blob:'].includes(url.protocol)) seen.offOrigin.push(request.url());
    if (url.pathname.startsWith('/v1/')) seen.api.push(`${request.method()} ${decodeURIComponent(url.pathname)}`);
  });
  page.on('pageerror', error => seen.errors.push(String(error)));
  return seen;
}

const snippetCalls = seen => seen.api.filter(r => r.endsWith('/snippet'));

async function signIn(page, username) {
  await page.getByLabel('Username').fill(username);
  await page.getByLabel('Password').fill(PASSWORD);
  await page.getByRole('button', { name: 'Sign in' }).click();
}

async function openCall(page, callId) {
  await page.locator(`[data-testid="call-row"][data-call-id="${callId}"]`).first().getByRole('button', { name: callId }).click();
  await expect(page.getByTestId('history-detail')).toBeVisible();
}

const block = (page, alertId) => page.locator(`[data-testid="alert-snippet"][data-alert-id="${alertId}"]`);

test.describe('SB snippet playback with the production accounts and retention store', () => {
  let server;
  let control;

  async function post(pathname, body) {
    const response = await fetch(`${control}${pathname}`, { method: 'POST', headers: { 'content-type': 'application/json' },
                                                            body: JSON.stringify(body) });
    const json = await response.json();
    if (!response.ok) throw new Error(`control ${pathname} refused: ${json.error}`);
    return json;
  }
  const store = async (type, callId, data, seq, ts) => {
    const { outcome } = await post('/store', { type, call_id: callId, data, seq, ts });
    expect(outcome, `${type} ${callId} seq ${seq}`).toBe('stored');
  };
  const audits = async limit => (await post('/audit', { limit })).rows;

  test.beforeAll(async () => {
    test.skip(!process.env[DSN_ENV], `set ${DSN_ENV} to a disposable PostgreSQL 16 database`);
    test.setTimeout(180000);
    requireBuild();
    const controlPort = await freePort();
    server = await startApi({
      uvArgs: ['--with-requirements', 'requirements-store.txt', '--with-requirements', 'requirements-auth.txt',
               '--with-requirements', 'requirements-retention.txt'],
      serverArgs: ['--control-port', String(controlPort), '--history-dsn-env', DSN_ENV, '--auth', '--snippets'] });
    control = `http://127.0.0.1:${controlPort}`;
    // sb1: four alerts one after another: a kept snippet, none, an expired one, a changed one.
    await store('call.started', 'sb1', {}, 1, T0);
    let seq = 2;
    for (const [i, suffix] of ['kept', 'none', 'expired', 'changed'].entries()) {
      const id = `sb1-${suffix}`;
      await store('alert.raised', 'sb1', { alert_id: id, direction: 'caller', ema: 0.7, window: 2 * i }, seq++, T0 + 10 * i + 2);
      await store('alert.cleared', 'sb1', { alert_id: id, direction: 'caller', ema: 0.3, window: 2 * i + 1 }, seq++, T0 + 10 * i + 6);
    }
    await store('call.ended', 'sb1', { final_state: 'monitoring', reason: 'bye', speech_seconds: 30.0, verdict: 'synthetic',
                                       ended_open_alert_ids: {} }, seq, T0 + 40);
    expect((await post('/snippet-put', { alert_id: 'sb1-kept', seconds: 2.5, hz: 440 })).stored).toBe(true);
    expect((await post('/snippet-put', { alert_id: 'sb1-expired', seconds: 1, hz: 440, age_s: 7200 })).stored).toBe(true);
    expect((await post('/snippet-put', { alert_id: 'sb1-changed', seconds: 1, hz: 440 })).stored).toBe(true);
    await post('/snippet-tamper', { alert_id: 'sb1-changed' });
  });

  test.afterAll(async () => {
    if (control) await post('/drop-schema', {}).catch(() => undefined);
    if (server) await server.stop();
  });

  test('SB01 FR-U5: a kept snippet plays in the browser after an audit row; the page fetches it only on request', async ({ page }) => {
    const seen = watch(page, server.base);
    await page.addInitScript(() => {
      window.__revoked = [];
      const revoke = URL.revokeObjectURL.bind(URL);
      URL.revokeObjectURL = url => { window.__revoked.push(url); revoke(url); };
    });
    await page.goto(`${server.base}/#history`);
    await signIn(page, 'ana');
    await openCall(page, 'sb1');
    const kept = block(page, 'sb1-kept');
    await expect(kept.getByTestId('snippet-load')).toHaveText('Load retained audio');
    await expect(page.getByTestId('alert-snippet')).toHaveCount(4);
    expect(snippetCalls(seen)).toEqual([]); // opening a call sends no audio request

    const answered = page.waitForResponse(r => r.url().endsWith('/v1/alerts/sb1-kept/snippet'));
    await kept.getByTestId('snippet-load').click();
    const response = await answered;
    expect(response.status()).toBe(200);
    expect(response.headers()['cache-control']).toBe('no-store');
    expect(response.headers()['content-type']).toBe('audio/wav');
    await expect(kept.getByTestId('snippet-info')).toContainText('2.5 s of retained audio at 8 kHz');
    const audio = kept.locator('audio');
    await expect(audio).toHaveAttribute('src', /^blob:/);
    // The browser's own decoder reads the WAV: its duration is the stored 2.5 s.
    const duration = await audio.evaluate(element => new Promise((resolve, reject) => {
      const done = () => (element.error
        ? reject(new Error(`the browser could not decode the snippet (MediaError ${element.error.code})`))
        : resolve(element.duration));
      if (element.readyState >= 1 || element.error) return done();
      element.addEventListener('loadedmetadata', done, { once: true });
      element.addEventListener('error', done, { once: true });
      setTimeout(() => reject(new Error(`no metadata: readyState ${element.readyState}, networkState ${element.networkState}`)),
                 10000);
    }));
    expect(duration).toBeCloseTo(2.5, 2);
    const [row] = await audits(1);
    expect([row.actor, row.action, row.outcome, row.detail]).toEqual(
      ['ana', 'alerts.snippet', 'ok', { alert_id: 'sb1-kept', seconds: 2.5, direction: 'caller' }]);

    // Closing releases the in-memory audio (its blob URL is revoked). Checked with a spy:
    // the page's CSP refuses fetch() of blob URLs, so fetching it would prove nothing.
    const src = await audio.getAttribute('src');
    expect(await page.evaluate(() => window.__revoked)).toEqual([]);
    await kept.getByTestId('snippet-close').click();
    await expect(kept.getByTestId('snippet-player')).toHaveCount(0);
    expect(await page.evaluate(() => window.__revoked)).toEqual([src]);
    expect(snippetCalls(seen)).toEqual(['GET /v1/alerts/sb1-kept/snippet']);
    expect(seen.offOrigin).toEqual([]);
    expect(seen.errors).toEqual([]);
    expect(seen.csp).toEqual([]);
  });

  test('SB02 missing, expired and changed snippets each show their own message, play nothing and write no audit row', async ({ page }) => {
    await page.goto(`${server.base}/#history`);
    await signIn(page, 'boss');
    await openCall(page, 'sb1');
    const before = (await audits(1))[0].id;
    const cases = [
      ['sb1-none', /^No snippet is kept for this alert\. Audio is kept only when retention was on as the call started/],
      ['sb1-expired', /^This alert's snippet has expired: its retention period has ended/],
      ['sb1-changed', /^The stored snippet could not be decrypted or checked/],
    ];
    for (const [alertId, text] of cases) {
      const b = block(page, alertId);
      await b.getByTestId('snippet-load').click();
      await expect(b.getByTestId('snippet-error')).toHaveText(text);
      await expect(b.getByTestId('snippet-player')).toHaveCount(0);
      await expect(b.getByTestId('snippet-load')).toHaveText('Try the snippet again');
    }
    const newer = (await audits(5)).filter(r => r.id > before);
    expect(newer.filter(r => r.action === 'alerts.snippet')).toEqual([]);
  });

  test('SB04 review L6: leaving the call detail with the player open releases the audio', async ({ page }) => {
    await page.addInitScript(() => {
      window.__revoked = [];
      const revoke = URL.revokeObjectURL.bind(URL);
      URL.revokeObjectURL = url => { window.__revoked.push(url); revoke(url); };
    });
    await page.goto(`${server.base}/#history`);
    await signIn(page, 'ana');
    await openCall(page, 'sb1');
    const kept = block(page, 'sb1-kept');
    await kept.getByTestId('snippet-load').click();
    const src = await kept.locator('audio').getAttribute('src');
    expect(src).toMatch(/^blob:/);
    await page.getByRole('link', { name: 'Batch analysis' }).click(); // the player unmounts
    await expect(page.getByTestId('dropzone')).toBeVisible();
    expect(await page.evaluate(() => window.__revoked)).toEqual([src]);
  });

  test('SB03 two clicks in one task send one audited request', async ({ page }) => {
    const seen = watch(page, server.base);
    await page.goto(`${server.base}/#history`);
    await signIn(page, 'ana');
    await openCall(page, 'sb1');
    const before = (await audits(1))[0].id;
    const kept = block(page, 'sb1-kept');
    await kept.getByTestId('snippet-load').evaluate(button => { button.click(); button.click(); });
    await expect(kept.getByTestId('snippet-player')).toBeVisible();
    await page.waitForTimeout(500);
    expect(snippetCalls(seen)).toEqual(['GET /v1/alerts/sb1-kept/snippet']);
    expect((await audits(5)).filter(r => r.id > before && r.action === 'alerts.snippet')).toHaveLength(1);
  });
});

const USER = { username: 'ana', role: 'analyst', expires_ts: Date.now() / 1000 + 3600, idle_s: 1800 };
const STORED = {
  call_id: 'm1', sessions_total: 1, sessions_truncated: false,
  sessions: [{
    session: 1, call_id: 'm1', status: 'ended', started_ts: T0, started_seen: true, state: 'alert', ended_ts: T0 + 10,
    last_event_ts: T0 + 10, final_state: 'alert', end_reason: 'bye', end_inferred: false, speech_seconds: 8,
    verdict: 'synthetic', degraded: false, quality: 'ok',
    alerts: [{ alert_id: 'm1-a1', direction: 'caller', state: 'cleared', raised_ts: T0 + 4, raised_window: 1,
               raised_ema: 0.6, closed_ts: T0 + 8 }],
    alerts_total: 1, alerts_truncated: false, windows: [], windows_total: 0, windows_truncated: false,
  }],
};
const CALLS = {
  calls: [{ ...STORED.sessions[0], alerts_total: 1, alerts_open: 0, windows_total: 0, windows_scored: 0, score_max: null }],
  next_before: null,
};
const json = (route, status, body) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

test.describe('SN snippet playback without a database', () => {
  let server;

  test.beforeAll(async () => {
    test.setTimeout(180000);
    requireBuild();
    server = await startApi();
  });

  test.afterAll(async () => { if (server) await server.stop(); });

  async function storedHistory(page) {
    await page.route('**/v1/history/calls**', route => json(route, 200, CALLS));
    await page.route('**/v1/alerts?**', route => json(route, 200, { alerts: [], next_cursor: null }));
    await page.route('**/v1/calls/m1/history', route => json(route, 200, STORED));
  }

  test('SN01 [mock history] a server without accounts shows no playback control and sends no snippet request', async ({ page }) => {
    const seen = watch(page, server.base);
    await storedHistory(page);
    await page.goto(`${server.base}/#history`);
    await page.getByRole('button', { name: 'm1', exact: true }).click();
    await expect(page.getByTestId('session-alerts')).toContainText('m1-a1');
    await expect(page.getByTestId('alert-snippet')).toHaveCount(0);
    expect(snippetCalls(seen)).toEqual([]);
  });

  test('SN02 [mock] not set up, a body that is not the WAV, an unreachable server and a 401 are each explained', async ({ page }) => {
    await storedHistory(page);
    let signedIn = true;
    await page.route('**/v1/auth/me', route => (signedIn ? json(route, 200, USER) : json(route, 401, { detail: 'Login required' })));
    let answer = 'not-configured';
    const requests = [];
    await page.route('**/v1/alerts/m1-a1/snippet', route => {
      requests.push(answer);
      if (answer === 'not-configured') return json(route, 503, { detail: 'Snippet playback is not configured on this server' });
      if (answer === 'html') return route.fulfill({ status: 200, contentType: 'text/html', body: '<html>proxy page</html>' });
      if (answer === 'abort') return route.abort('connectionreset');
      signedIn = false;
      return json(route, 401, { detail: 'Login required' });
    });
    await page.goto(`${server.base}/#history`);
    await page.getByRole('button', { name: 'm1', exact: true }).click();
    const b = block(page, 'm1-a1');
    await b.getByTestId('snippet-load').click();
    await expect(b.getByTestId('snippet-error')).toHaveText('Snippet playback is not set up on this server (no retention folder and key).');
    answer = 'html';
    await b.getByTestId('snippet-load').click();
    await expect(b.getByTestId('snippet-error')).toHaveText('The service returned audio this page cannot play.');
    await expect(b.getByTestId('snippet-player')).toHaveCount(0);
    answer = 'abort';
    await b.getByTestId('snippet-load').click();
    await expect(b.getByTestId('snippet-error')).toHaveText('The service could not be reached.');
    await page.waitForTimeout(500);
    expect(requests).toEqual(['not-configured', 'html', 'abort']); // one request per click, never retried
    answer = 'unauthorized';
    await b.getByTestId('snippet-load').click();
    await expect(page.getByTestId('login-notice')).toHaveText('Your session has ended. Sign in again.');
    await expect(page.getByTestId('history-detail')).toHaveCount(0);
  });
});
