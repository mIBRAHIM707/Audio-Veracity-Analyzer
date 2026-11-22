// Claude-authored browser cases (22 November 2026) for backlog 7.QA: the signed-in pages
// across a REAL analysis API crash and restart. PRD FR-U7 (local accounts; the session
// lives in PostgreSQL, not in the API process), FR-A7 (feedback), FR-U5 (retained
// snippets), FR-U1 (the board shows a lost connection and recovers) and §6.3 (audit).
// Authored by Claude (Claude Code), not GPT-6 Astra. Extends RC01/RC02
// (dashboard-recovery.spec.cjs), which run without accounts, and the [mock] failure cases
// FN02/SN02, which fake an unreachable server with a Playwright route.
//
// The API process is killed abruptly (process tree or group) and a new one is started on
// the same port with the same schema (`--schema`), snippet folder and key file, as after
// a real crash and restart: what survives is only what the database and the folder hold.
// No Playwright route sits between the page and the server.
//
// Needs AVA_TEST_POSTGRES_DSN naming a disposable PostgreSQL 16 database
// (services/store/README.md); skipped without it. Infrastructure evidence only: no
// detector and no real audio are involved. Build first: `npm run build --prefix dashboard`.
const { test, expect } = require('@playwright/test');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { DIST, freePort, startApi } = require('./support/api-server.cjs');

const DSN_ENV = 'AVA_TEST_POSTGRES_DSN';
const PASSWORD = 'browser-test-password-2026'; // serve_dashboard_api.AUTH_TEST_PASSWORD (fictional)
const T0 = 1763600000;
const UNREACHABLE_LABEL = 'The service could not be reached. The label may or may not have been stored: reload the labels to check.';

test.describe.configure({ mode: 'serial' });

function watch(page, base) {
  const seen = { api: [], offOrigin: [], errors: [], sockets: [] };
  page.on('request', request => {
    const url = new URL(request.url());
    if (url.host !== new URL(base).host && !['data:', 'blob:'].includes(url.protocol)) seen.offOrigin.push(request.url());
    if (url.pathname.startsWith('/v1/')) seen.api.push(`${request.method()} ${decodeURIComponent(url.pathname)}`);
  });
  page.on('pageerror', error => seen.errors.push(String(error)));
  page.on('websocket', ws => seen.sockets.push(ws.url()));
  return seen;
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

async function signIn(page, username) {
  await page.getByLabel('Username').fill(username);
  await page.getByLabel('Password').fill(PASSWORD);
  await page.getByRole('button', { name: 'Sign in' }).click();
}

test.describe('AR signed-in pages across a real API crash and restart', () => {
  let server = null;
  let control = null;
  let tmp = null;
  const schema = `ava_browser_ar_${crypto.randomBytes(6).toString('hex')}`;

  /** Starts the API on `port` (a free one when undefined) with the shared schema, folder and key. */
  async function start(port) {
    const controlPort = await freePort();
    server = await startApi({
      port,
      uvArgs: ['--with-requirements', 'requirements-store.txt', '--with-requirements', 'requirements-auth.txt',
               '--with-requirements', 'requirements-retention.txt'],
      serverArgs: ['--control-port', String(controlPort), '--history-dsn-env', DSN_ENV, '--schema', schema, '--auth',
                   '--snippets', '--snippet-dir', path.join(tmp, 'snippets'), '--snippet-key-file', path.join(tmp, 'key.bin'),
                   // A 2 s socket session re-check instead of 60 s keeps AR03 short.
                   '--ws-session-recheck-s', '2'] });
    control = `http://127.0.0.1:${controlPort}`;
    return server;
  }

  /** Kills the API abruptly and waits until its port no longer answers. */
  async function crash() {
    const { base } = server;
    await server.stop();
    control = null;
    await expect.poll(() => unreachable(base), { timeout: 15000 }).toBe(true);
  }

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
  const auditSince = async id => (await post('/audit', { limit: 100 })).rows.filter(r => r.id > id).reverse();
  const lastAuditId = async () => (await post('/audit', { limit: 1 })).rows[0]?.id ?? 0;

  test.beforeAll(async () => {
    test.skip(!process.env[DSN_ENV], `set ${DSN_ENV} to a disposable PostgreSQL 16 database`);
    test.setTimeout(180000);
    if (!fs.existsSync(path.join(DIST, 'index.html'))) throw new Error('dashboard/dist missing: run `npm run build --prefix dashboard`');
    tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'ava-browser-ar-'));
    await start(undefined);
    // ar2: an ended call with one cleared alert and a retained snippet for it.
    await store('call.started', 'ar2', {}, 1, T0);
    await store('alert.raised', 'ar2', { alert_id: 'ar2-a1', direction: 'caller', ema: 0.7, window: 1 }, 2, T0 + 4);
    await store('alert.cleared', 'ar2', { alert_id: 'ar2-a1', direction: 'caller', ema: 0.3, window: 3 }, 3, T0 + 8);
    await store('call.ended', 'ar2', { final_state: 'monitoring', reason: 'bye', speech_seconds: 8.0, verdict: 'synthetic',
                                       ended_open_alert_ids: {} }, 4, T0 + 10);
    expect((await post('/snippet-put', { alert_id: 'ar2-a1', seconds: 2.0, hz: 440 })).stored).toBe(true);
  });

  test.afterAll(async () => {
    test.setTimeout(180000); // a restart for cleanup can take as long as the first start (review L1)
    // Every case ends with a server running on the shared schema; if one failed while
    // the API was down, start it once more so the schema is not left behind.
    try {
      if (tmp && !control) await start(undefined);
      if (control) await post('/drop-schema', {});
    } catch { /* best effort: a disposable database */ }
    if (server) await server.stop();
    if (tmp) fs.rmSync(tmp, { recursive: true, force: true });
  });

  test('AR01 FR-U7/FR-U1: a signed-in live board survives an API crash; the restarted API accepts the same session without a new sign-in', async ({ page }) => {
    test.setTimeout(240000);
    const seen = watch(page, server.base);
    const { base, port } = server;
    const publish = (type, callId, data = {}) => post('/publish', { type, call_id: callId, data });
    await page.goto(`${base}/#live`);
    await signIn(page, 'ana');
    await expect(page.getByTestId('account-user')).toHaveText('ana');
    await expect(page.getByTestId('connection')).toHaveText('Live');
    await publish('call.started', 'ar1-old');
    await expect(page.locator('[data-testid="call-card"][data-call-id="ar1-old"]')).toBeVisible();
    const before = await lastAuditId();

    await crash();
    // The board says it is disconnected; the socket close makes the gate re-check the
    // session, which cannot be verified either. The page stays: an unreachable server is
    // not a sign-out (AuthGate keeps the pages and shows a banner).
    await expect(page.getByTestId('connection')).toContainText('Live updates disconnected:');
    await expect(page.getByTestId('session-unverified')).toContainText('The session could not be verified: The service could not be reached.');
    await expect(page.getByTestId('login')).toHaveCount(0);
    await expect(page.getByTestId('account-user')).toHaveText('ana');
    await expect.poll(() => seen.sockets.length, { timeout: 15000 }).toBeGreaterThanOrEqual(3); // still retrying

    const restartedAt = seen.api.length;
    await start(port);
    await publish('call.started', 'ar1-new');
    // No reload and no sign-in: the session row in PostgreSQL is what the new process checks.
    await expect(page.getByTestId('connection')).toHaveText('Live', { timeout: 30000 });
    await expect(page.locator('[data-testid="call-card"][data-call-id="ar1-new"]')).toBeVisible();
    await expect(page.getByTestId('login')).toHaveCount(0);
    // Once the service answers again the out-of-date banner goes: the session was verified.
    await expect(page.getByTestId('session-unverified')).toHaveCount(0, { timeout: 15000 });
    // ... through a session check sent after the restarted API's first snapshot answered
    // (review M1): requests made after the restart, the snapshot read before the check.
    const after = seen.api.slice(restartedAt);
    expect(after).toContain('GET /v1/calls');
    expect(after.lastIndexOf('GET /v1/auth/me')).toBeGreaterThan(after.indexOf('GET /v1/calls'));
    // The restart neither signed anyone in nor out.
    const rows = await auditSince(before);
    expect(rows.filter(r => r.action.startsWith('auth.'))).toEqual([]);
    expect(new Set(seen.sockets)).toEqual(new Set([`${base.replace('http', 'ws')}/v1/events`]));
    expect(seen.offOrigin).toEqual([]);
    expect(seen.errors).toEqual([]);
  });

  test('AR02 FR-A7/FR-U5: feedback and snippet requests to a dead API are reported, never claimed or retried; after the restart both work under the same session', async ({ page }) => {
    test.setTimeout(240000);
    const seen = watch(page, server.base);
    const { base, port } = server;
    await page.goto(`${base}/#history`);
    await signIn(page, 'ana');
    await page.locator('[data-testid="call-row"][data-call-id="ar2"]').first().getByRole('button', { name: 'ar2' }).click();
    await expect(page.getByTestId('history-detail')).toBeVisible();
    const feedback = page.locator('[data-testid="alert-feedback"][data-alert-id="ar2-a1"]');
    const snippet = page.locator('[data-testid="alert-snippet"][data-alert-id="ar2-a1"]');
    await feedback.getByTestId('feedback-toggle').click();
    await expect(feedback.getByTestId('feedback-current')).toHaveText('No label yet.');
    await snippet.getByTestId('snippet-load').click();
    await expect(snippet.getByTestId('snippet-info')).toContainText('2.0 s of retained audio at 8 kHz');
    await snippet.getByTestId('snippet-close').click();
    const before = await lastAuditId();

    await crash();
    await feedback.getByTestId('feedback-true_positive').click();
    await expect(feedback.getByTestId('feedback-error')).toHaveText(UNREACHABLE_LABEL);
    await expect(feedback.getByTestId('feedback-current')).toHaveText('No label yet.'); // nothing claimed
    await snippet.getByTestId('snippet-load').click();
    await expect(snippet.getByTestId('snippet-error')).toHaveText('The service could not be reached.');
    await expect(snippet.getByTestId('snippet-player')).toHaveCount(0);
    await page.getByRole('button', { name: 'Refresh' }).click();
    await expect(page.getByTestId('calls-error')).toHaveText('The history service could not be reached.');
    await expect(page.getByTestId('login')).toHaveCount(0); // a dead server is not a sign-out
    await page.waitForTimeout(1000); // a delayed retry would have been sent by now
    expect(seen.api.filter(r => r.startsWith('POST') && r.endsWith('/feedback'))).toEqual(['POST /v1/alerts/ar2-a1/feedback']);
    // One playback before the crash, one attempt while dead: neither retried (review L3).
    expect(seen.api.filter(r => r.endsWith('/snippet'))).toEqual(['GET /v1/alerts/ar2-a1/snippet', 'GET /v1/alerts/ar2-a1/snippet']);

    await start(port);
    // The failed write never reached a server: the labels read back from the database
    // show none, so the page's "may or may not" was the honest answer.
    await feedback.getByTestId('feedback-reload').click();
    await expect(feedback.getByTestId('feedback-current')).toHaveText('No label yet.');
    await feedback.getByTestId('feedback-true_positive').click();
    await expect(feedback.getByTestId('feedback-saved')).toHaveText('Stored: True positive (synthetic speech).');
    expect((await post('/feedback-rows', { alert_id: 'ar2-a1' })).rows.map(r => [r.label, r.username, r.alert_status]))
      .toEqual([['true_positive', 'ana', 'cleared']]);
    // The snippet written before the crash is read by the new process with the same key.
    await snippet.getByTestId('snippet-load').click();
    await expect(snippet.getByTestId('snippet-info')).toContainText('2.0 s of retained audio at 8 kHz');
    await expect(snippet.locator('audio')).toHaveAttribute('src', /^blob:/);
    await page.getByRole('button', { name: 'Refresh' }).click();
    await expect(page.getByTestId('calls-error')).toHaveCount(0);
    await expect(page.getByTestId('login')).toHaveCount(0);
    // Audit: one label and one audio export, by the same session; no sign-in or sign-out.
    const rows = await auditSince(before);
    expect(rows.map(r => [r.actor, r.action, r.outcome])).toEqual([['ana', 'alerts.feedback', 'ok'],
                                                                   ['ana', 'alerts.snippet', 'ok']]);
    await expect(page.getByTestId('session-unverified')).toHaveCount(0);
    expect(seen.offOrigin).toEqual([]);
    expect(seen.errors).toEqual([]);
  });

  test('AR03 FR-U7: a session revoked while the API was down is refused by the restarted API and the page returns to sign-in', async ({ page }) => {
    test.setTimeout(240000);
    const { base, port } = server;
    await page.goto(`${base}/#live`);
    await signIn(page, 'boss');
    await expect(page.getByTestId('connection')).toHaveText('Live');

    await crash();
    await expect(page.getByTestId('connection')).toContainText('Live updates disconnected:');
    await start(port);
    // The admin's account is disabled on the restarted service (its sessions revoked in
    // PostgreSQL) whether or not the page has reconnected yet: a refused handshake or the
    // socket's own session re-check ends the board either way.
    expect((await post('/auth-disable', { username: 'boss', disabled: true })).sessions_revoked).toBeGreaterThanOrEqual(1);
    await expect(page.getByTestId('login-notice')).toHaveText('Your session has ended. Sign in again.', { timeout: 30000 });
    await expect(page.getByTestId('connection')).toHaveCount(0);
    await post('/auth-disable', { username: 'boss', disabled: false });
  });
});
