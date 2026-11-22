// Claude-authored browser cases (19 November 2026, attempt 7) for backlog 7.3 / PRD FR-U7
// (login with username and password, two roles) and §6.3 (audit of logins and exports),
// with FR-U8 (same origin). Authored by Claude (Claude Code), not GPT-6 Astra.
//
// AB cases: the production build served by the real analysis API with the production
// accounts (`services/auth`, `--auth` in serve_dashboard_api.py) in a fresh schema of a
// disposable PostgreSQL database, users `ana` (analyst) and `boss` (admin). They need
// AVA_TEST_POSTGRES_DSN and are skipped without it. AN cases need no database: one uses
// the real API without accounts, one is [mock] (Playwright route) for a store failure.
// Build first: `npm run build --prefix dashboard`.
const { test, expect } = require('@playwright/test');
const fs = require('node:fs');
const path = require('node:path');
const { DIST, freePort, startApi, tones } = require('./support/api-server.cjs');

const DSN_ENV = 'AVA_TEST_POSTGRES_DSN';
const PASSWORD = 'browser-test-password-2026'; // serve_dashboard_api.AUTH_TEST_PASSWORD (fictional)

test.describe.configure({ mode: 'serial' });

function requireBuild() {
  if (!fs.existsSync(path.join(DIST, 'index.html'))) throw new Error('dashboard/dist missing: run `npm run build --prefix dashboard`');
}

function watch(page, base) {
  const seen = { api: [], offOrigin: [], errors: [], sockets: [] };
  page.on('request', request => {
    const url = new URL(request.url());
    if (url.host !== new URL(base).host && !['data:', 'blob:'].includes(url.protocol)) seen.offOrigin.push(request.url());
    if (url.pathname.startsWith('/v1/')) seen.api.push(`${request.method()} ${url.pathname}`);
  });
  page.on('pageerror', error => seen.errors.push(String(error)));
  page.on('websocket', ws => seen.sockets.push(ws.url()));
  return seen;
}

async function signIn(page, username = 'ana', password = PASSWORD) {
  await page.getByLabel('Username').fill(username);
  await page.getByLabel('Password').fill(password);
  await page.getByRole('button', { name: 'Sign in' }).click();
}

test.describe('AB sign in with the production accounts', () => {
  let server;
  let control;

  async function post(pathname, body) {
    const response = await fetch(`${control}${pathname}`, { method: 'POST', headers: { 'content-type': 'application/json' },
                                                            body: JSON.stringify(body) });
    const json = await response.json();
    if (!response.ok) throw new Error(`control ${pathname} refused: ${json.error}`);
    return json;
  }

  test.beforeAll(async () => {
    test.skip(!process.env[DSN_ENV], `set ${DSN_ENV} to a disposable PostgreSQL 16 database`);
    test.setTimeout(180000);
    requireBuild();
    const controlPort = await freePort();
    server = await startApi({
      uvArgs: ['--with-requirements', 'requirements-store.txt', '--with-requirements', 'requirements-auth.txt'],
      // A 3 s socket re-check instead of 60 s keeps AB07 short and its margin wide.
      serverArgs: ['--control-port', String(controlPort), '--history-dsn-env', DSN_ENV, '--auth',
                   '--ws-session-recheck-s', '3'] });
    control = `http://127.0.0.1:${controlPort}`;
  });

  test.afterAll(async () => {
    if (control) await post('/drop-schema', {}).catch(() => undefined);
    if (server) await server.stop();
  });

  test('AB01 FR-U7: nothing but the session check before sign-in; a wrong password is refused; the right one opens the pages', async ({ page, context }) => {
    const seen = watch(page, server.base);
    await page.goto(`${server.base}/#history`);
    await expect(page.getByTestId('login')).toBeVisible();
    await expect(page).toHaveTitle('AVA sign in');
    await expect(page.getByTestId('history-calls')).toHaveCount(0);
    expect(seen.api.length).toBeGreaterThan(0);
    expect(seen.api.filter(r => r !== 'GET /v1/auth/me')).toEqual([]); // a focus event may add a check
    expect(seen.sockets).toEqual([]);

    await signIn(page, 'ana', 'not the password');
    await expect(page.getByTestId('login-error')).toHaveText('Invalid username or password.');
    await signIn(page, 'nobody', PASSWORD);
    await expect(page.getByTestId('login-error')).toHaveText('Invalid username or password.');
    expect(await context.cookies()).toEqual([]);

    await signIn(page);
    await expect(page.getByTestId('account-user')).toHaveText('ana');
    await expect(page.getByTestId('account-role')).toHaveText('analyst');
    await expect(page.getByTestId('history-calls')).toBeVisible();
    await expect(page.getByTestId('calls-error')).toHaveCount(0);
    const cookies = await context.cookies();
    expect(cookies.map(c => [c.name, c.httpOnly, c.sameSite, c.secure, c.path]))
      .toEqual([['ava_session', true, 'Strict', true, '/']]);
    expect(await page.evaluate(() => document.cookie)).toBe('');
    expect(cookies[0].value).toMatch(/^[A-Za-z0-9_-]{43}$/);
    expect(JSON.stringify(seen.api)).not.toContain(cookies[0].value);
    expect(seen.offOrigin).toEqual([]);
    expect(seen.errors).toEqual([]);
    expect(await page.content()).not.toContain(PASSWORD);

    const { rows } = await post('/audit', { limit: 10 });
    expect(rows.slice(0, 3).map(r => [r.actor, r.action, r.outcome, r.detail.reason ?? null, r.detail.username ?? null]))
      .toEqual([['ana', 'auth.login', 'ok', null, null],
                ['anonymous', 'auth.login', 'refused', 'unknown_user', null],
                ['anonymous', 'auth.login', 'refused', 'wrong_password', 'ana']]);
    await page.getByTestId('logout').click(); // later cases start with no open session for ana
    await expect(page.getByTestId('login')).toBeVisible();
  });

  test('AB02 signed in: the live socket opens, an export is audited, an upload is audited', async ({ page }) => {
    const seen = watch(page, server.base);
    await page.goto(`${server.base}/#live`);
    await signIn(page, 'boss');
    await expect(page.getByTestId('account-role')).toHaveText('admin');
    await expect(page.getByTestId('connection')).toHaveText('Live');
    expect(seen.sockets).toHaveLength(1);

    await page.getByRole('link', { name: 'History' }).click();
    const downloaded = page.waitForEvent('download');
    await page.getByTestId('alert-export').click();
    expect((await downloaded).suggestedFilename()).toBe('ava-alerts.csv');
    let { rows } = await post('/audit', { limit: 5 });
    expect([rows[0].actor, rows[0].action, rows[0].detail.rows]).toEqual(['boss', 'alerts.export', 0]);

    await page.getByRole('link', { name: 'Batch analysis' }).click();
    await page.locator('input[type=file]').setInputFiles({ name: 'tone.wav', mimeType: 'audio/wav',
                                                         buffer: tones([{ seconds: 8, hz: 300 }]) });
    await expect(page.getByTestId('verdict')).toBeVisible();
    ({ rows } = await post('/audit', { limit: 1 }));
    expect([rows[0].actor, rows[0].action, rows[0].outcome, rows[0].detail])
      .toEqual(['boss', 'api.analyze', 'ok', { route: '/v1/analyze', method: 'POST', status: 200 }]);
    expect(JSON.stringify(rows)).not.toContain('tone.wav');
    expect(seen.errors).toEqual([]);
  });

  test('AB03 sign out ends the session on the server and clears every page', async ({ page, context }) => {
    await page.goto(`${server.base}/#history`);
    await signIn(page);
    await expect(page.getByTestId('history-calls')).toBeVisible();
    const [cookie] = await context.cookies();
    await page.getByTestId('logout').click();
    await expect(page.getByTestId('login-notice')).toHaveText('You have signed out.');
    await expect(page.getByTestId('history-calls')).toHaveCount(0);
    expect(await context.cookies()).toEqual([]);
    // The old token is dead on the server, not only forgotten by the browser.
    const replay = await fetch(`${server.base}/v1/auth/me`, { headers: { cookie: `ava_session=${cookie.value}` } });
    expect(replay.status).toBe(401);
    const { rows } = await post('/audit', { limit: 1 });
    expect([rows[0].actor, rows[0].action]).toEqual(['ana', 'auth.logout']);
  });

  test('AB04 a session ended elsewhere sends the open page back to sign-in with a notice', async ({ page }) => {
    await page.goto(`${server.base}/#batch`);
    await signIn(page);
    await expect(page.getByTestId('account-user')).toHaveText('ana');
    expect((await post('/auth-disable', { username: 'ana', disabled: true })).sessions_revoked).toBe(1);
    try {
      await page.getByRole('link', { name: 'History' }).click(); // its first read gets 401
      await expect(page.getByTestId('login-notice')).toHaveText('Your session has ended. Sign in again.');
      await expect(page.getByTestId('history-calls')).toHaveCount(0);
      await signIn(page);
      await expect(page.getByTestId('login-error')).toHaveText('Invalid username or password.');
    } finally {
      await post('/auth-disable', { username: 'ana', disabled: false });
    }
    await signIn(page);
    await expect(page.getByTestId('history-calls')).toBeVisible();
    await page.getByTestId('logout').click(); // later cases start with no open session for ana
    await expect(page.getByTestId('login')).toBeVisible();
  });

  test('AB05 repeated failures lock the name without saying whether it exists; no cross-site login', async ({ page }) => {
    await page.goto(server.base);
    for (let i = 0; i < 4; i += 1) {
      await signIn(page, 'ghost', 'wrong password');
      await expect(page.getByTestId('login-error')).toHaveText('Invalid username or password.');
    }
    await signIn(page, 'ghost', 'wrong password');
    await expect(page.getByTestId('login-error'))
      .toHaveText('Too many failed attempts for this username. Wait 15 minutes and try again.');
    const crossSite = await fetch(`${server.base}/v1/auth/login`, {
      method: 'POST', headers: { 'content-type': 'application/json', origin: 'http://evil.example' },
      body: JSON.stringify({ username: 'ana', password: PASSWORD }) });
    expect(crossSite.status).toBe(403);
    expect(crossSite.headers.get('set-cookie')).toBeNull();
  });

  test('AB06 review H1: the idle re-check never keeps a session alive, and an idle session ends on screen', async ({ page }) => {
    await page.clock.install();
    const seen = watch(page, server.base);
    await page.goto(`${server.base}/#history`);
    await signIn(page, 'boss');
    await expect(page.getByTestId('history-calls')).toBeVisible();
    // Active session: the timer's check (idle limit + 5 s) finds it valid; the page stays.
    const before = seen.api.filter(r => r === 'GET /v1/auth/me').length;
    // runFor fires every timer due in the span (fastForward fires each at most once), so
    // a page polling more often than the idle limit would show here.
    await page.clock.runFor((30 * 60 + 5) * 1000);
    await expect.poll(() => seen.api.filter(r => r === 'GET /v1/auth/me').length).toBe(before + 1);
    await expect(page.getByTestId('account-user')).toHaveText('boss');
    // No request for longer than the idle limit (moved on the server): the next timer
    // check is refused and the pages go.
    expect((await post('/age-sessions', { seconds: 31 * 60 })).aged).toBeGreaterThan(0);
    await page.clock.runFor((30 * 60 + 5) * 1000);
    await expect(page.getByTestId('login-notice')).toHaveText('Your session has ended. Sign in again.');
    await expect(page.getByTestId('history-calls')).toHaveCount(0);
    // Between the two timer checks the page asked nothing else (no polling).
    expect(seen.api.filter(r => r === 'GET /v1/auth/me').length).toBe(before + 2);
  });

  test('AB07 a live board whose session is revoked goes back to sign-in when the server closes its socket', async ({ page }) => {
    await page.goto(`${server.base}/#live`);
    await signIn(page);
    await expect(page.getByTestId('connection')).toHaveText('Live');
    expect((await post('/auth-disable', { username: 'ana', disabled: true })).sessions_revoked).toBe(1);
    try {
      // The server re-checks an open socket (every 3 s here, 60 s in production) and closes
      // it 1008; the board then asks the gate to re-check, which is refused. No focus or
      // read happens meanwhile, so only the socket path can end the page.
      await expect(page.getByTestId('login-notice')).toHaveText('Your session has ended. Sign in again.',
                                                                { timeout: 20000 });
      await expect(page.getByTestId('connection')).toHaveCount(0);
    } finally {
      await post('/auth-disable', { username: 'ana', disabled: false });
    }
  });

  test('AB08 re-review N-M1: a PC clock 13 hours ahead of the server does not end the session at once', async ({ page }) => {
    await page.clock.setFixedTime(Date.now() + 13 * 3600 * 1000); // timers keep running
    await page.goto(`${server.base}/#batch`);
    await signIn(page, 'boss');
    await expect(page.getByTestId('account-user')).toHaveText('boss');
    await page.waitForTimeout(1500); // a timer computed from this PC's date would already have fired
    await expect(page.getByTestId('account-user')).toHaveText('boss');
    await expect(page.getByTestId('login')).toHaveCount(0);
  });
});

test.describe('AN without a database', () => {
  let server;

  test.beforeAll(async () => {
    test.setTimeout(180000);
    requireBuild();
    server = await startApi();
  });

  test.afterAll(async () => { if (server) await server.stop(); });

  test('AN01 a server without accounts shows the pages as before, with no sign-in or account bar', async ({ page }) => {
    const seen = watch(page, server.base);
    await page.goto(server.base);
    await expect(page.getByRole('heading', { name: 'AVA batch analysis' })).toBeVisible();
    await expect(page.getByTestId('login')).toHaveCount(0);
    await expect(page.getByTestId('account')).toHaveCount(0);
    expect(seen.api).toEqual(['GET /v1/auth/me']);
  });

  test('AN02 [mock] a failed session check shows its reason and a retry, never the pages', async ({ page }) => {
    let answer = 503;
    await page.route('**/v1/auth/me', route => route.fulfill({
      status: answer, contentType: 'application/json',
      body: JSON.stringify({ detail: answer === 503 ? 'Account store unavailable (unavailable)' : 'Login required' }) }));
    await page.goto(server.base);
    await expect(page.getByTestId('session-error'))
      .toHaveText('The account store is unavailable, so the session cannot be checked. Try again shortly.');
    await expect(page.getByTestId('dropzone')).toHaveCount(0);
    answer = 401;
    await page.getByRole('button', { name: 'Try again' }).click();
    await expect(page.getByTestId('login')).toBeVisible();
  });

  const USER = { username: 'ana', role: 'analyst', expires_ts: Date.now() / 1000 + 3600, idle_s: 1800 };

  test('AN03 [mock] a failed sign-out keeps the session and says so; a failed re-check keeps the page with a warning', async ({ page }) => {
    let me = { status: 200, body: USER };
    await page.route('**/v1/auth/me', route => route.fulfill({ status: me.status, contentType: 'application/json',
                                                              body: JSON.stringify(me.body) }));
    await page.route('**/v1/auth/logout', route => route.fulfill({
      status: 503, contentType: 'application/json', body: JSON.stringify({ detail: 'Account store unavailable (busy)' }) }));
    await page.goto(server.base);
    await expect(page.getByTestId('account-user')).toHaveText('ana');
    await page.getByTestId('logout').click();
    await expect(page.getByTestId('logout-error'))
      .toHaveText('Logging out failed: the account store is unavailable, so the session is still open. Try again.');
    await expect(page.getByTestId('dropzone')).toBeVisible();
    me = { status: 503, body: { detail: 'Account store unavailable (unavailable)' } };
    await page.evaluate(() => window.dispatchEvent(new Event('focus')));
    await expect(page.getByTestId('session-unverified')).toContainText('The session could not be verified');
    me = { status: 200, body: USER };
    await page.evaluate(() => window.dispatchEvent(new Event('focus')));
    await expect(page.getByTestId('session-unverified')).toHaveCount(0);
  });

  // Claude-authored, 22 Nov 2026 (7.QA review M1/M2 of the AR01 fix): no focus event is
  // sent after the warning appears, so only a page's 2xx can clear it.
  test('AN06 [mock] after a failed re-check, a later 2xx re-checks once and clears the warning; bursts and verified sessions send no extra checks', async ({ page }) => {
    let me = { status: 200, body: USER };
    let meCalls = 0;
    await page.route('**/v1/auth/me', route => {
      meCalls += 1;
      return route.fulfill({ status: me.status, contentType: 'application/json', body: JSON.stringify(me.body) });
    });
    const json = body => route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
    await page.route('**/v1/history/calls**', json({ calls: [], next_before: null }));
    await page.route('**/v1/alerts?**', json({ alerts: [], next_cursor: null }));
    await page.goto(server.base);
    await expect(page.getByTestId('account-user')).toHaveText('ana');

    // Verified: 2xx answers send no session check.
    await page.getByRole('link', { name: 'History' }).click();
    await expect(page.getByTestId('history-calls')).toBeVisible();
    await expect(page.getByTestId('alerts-status')).not.toContainText('Loading');
    await page.waitForTimeout(500);
    const verifiedCalls = meCalls;
    await page.getByRole('button', { name: 'Refresh' }).click();
    await page.getByRole('button', { name: 'Search' }).click();
    await page.waitForTimeout(500);
    expect(meCalls).toBe(verifiedCalls);

    // A failed re-check shows the warning.
    me = { status: 503, body: { detail: 'Account store unavailable (unavailable)' } };
    await page.evaluate(() => window.dispatchEvent(new Event('focus')));
    await expect(page.getByTestId('session-unverified')).toContainText('The session could not be verified');
    const failedCalls = meCalls;

    // The service is back: two 2xx in one click (calls and alerts) start exactly one check.
    me = { status: 200, body: USER };
    await page.getByRole('button', { name: 'Refresh' }).click();
    await page.getByRole('button', { name: 'Search' }).click();
    await expect(page.getByTestId('session-unverified')).toHaveCount(0);
    await page.waitForTimeout(500);
    expect(meCalls).toBe(failedCalls + 1);
    // Verified again: further 2xx send nothing more.
    await page.getByRole('button', { name: 'Refresh' }).click();
    await page.waitForTimeout(500);
    expect(meCalls).toBe(failedCalls + 1);

    // A session check that fails while reads succeed (re-review Nit 1: past the earlier
    // check's 5 s limit first, so this phase runs its own limit). Four refreshes start
    // exactly one check, which fails; the warning stays.
    me = { status: 503, body: { detail: 'Account store unavailable (unavailable)' } };
    await page.evaluate(() => window.dispatchEvent(new Event('focus')));
    await expect(page.getByTestId('session-unverified')).toBeVisible();
    await page.waitForTimeout(5500);
    const stuckCalls = meCalls;
    for (let i = 0; i < 4; i += 1) {
      await page.getByRole('button', { name: 'Refresh' }).click();
      await page.waitForTimeout(150);
    }
    await page.waitForTimeout(500);
    expect(meCalls - stuckCalls).toBe(1);
    await expect(page.getByTestId('session-unverified')).toBeVisible(); // still failing: still said
    // The store recovers and the page goes quiet: the answers dropped by the limit leave
    // one deferred check, which clears the warning with no focus and no further request.
    me = { status: 200, body: USER };
    await expect(page.getByTestId('session-unverified')).toHaveCount(0, { timeout: 8000 });
    expect(meCalls - stuckCalls).toBe(2);
    await page.waitForTimeout(1000);
    expect(meCalls - stuckCalls).toBe(2); // and nothing after it
  });

  test('AN04 [mock] review M1: a session check answered after sign-in does not sign the user out again', async ({ page }) => {
    let calls = 0;
    await page.route('**/v1/auth/me', async route => {
      calls += 1;
      if (calls === 2) await new Promise(r => setTimeout(r, 1500)); // a check sent before the login answers late
      await route.fulfill({ status: 401, contentType: 'application/json', body: '{"detail":"Login required"}' });
    });
    await page.route('**/v1/auth/login', route => route.fulfill({ status: 200, contentType: 'application/json',
                                                                 body: JSON.stringify(USER) }));
    await page.goto(server.base);
    await expect(page.getByTestId('login')).toBeVisible();
    await page.evaluate(() => window.dispatchEvent(new Event('focus'))); // the slow check
    await signIn(page);
    await expect(page.getByTestId('account-user')).toHaveText('ana');
    await page.waitForTimeout(2000); // past the late answer
    await expect(page.getByTestId('account-user')).toHaveText('ana');
    await expect(page.getByTestId('login')).toHaveCount(0);
    expect(calls).toBe(2);
  });

  test('AN05 [mock] re-review N-L1: a server restarted with accounts on sends an open page to sign-in on the first 401', async ({ page }) => {
    let accounts = false;
    await page.route('**/v1/auth/me', route => route.fulfill(accounts
      ? { status: 401, contentType: 'application/json', body: '{"detail":"Login required"}' }
      : { status: 404, contentType: 'application/json', body: '{"detail":"Not Found"}' }));
    await page.route('**/v1/history/calls**', route => route.fulfill({
      status: 401, contentType: 'application/json', body: '{"detail":"Login required"}' }));
    await page.goto(server.base);
    await expect(page.getByTestId('dropzone')).toBeVisible();
    accounts = true;
    await page.getByRole('link', { name: 'History' }).click();
    await expect(page.getByTestId('login')).toBeVisible();
  });
});
