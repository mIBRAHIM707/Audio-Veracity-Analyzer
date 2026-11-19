// Claude-authored browser cases (19 November 2026, attempt 9) for backlog 7.3 / PRD FR-A7
// (analysts mark an alert true or false positive; plan item 3.25, the feedback UI), with
// FR-U7 (only signed-in users label; both roles may) and §6.3 (feedback is audited).
// Authored by Claude (Claude Code), not GPT-6 Astra.
//
// FB cases: the production build served by the real analysis API with the production
// accounts and `AlertFeedbackStore` (serve_dashboard_api.py --auth) in a fresh schema of
// a disposable PostgreSQL database; the alerts are written by the production
// `PostgresEventStore`. They need AVA_TEST_POSTGRES_DSN and are skipped without it.
// FN cases need no database: the real API without accounts, with history and feedback
// answers [mock] (Playwright route) to reach failures the server cannot be put into on
// demand. Infrastructure evidence only: no detector is involved.
// Build first: `npm run build --prefix dashboard`.
const { test, expect } = require('@playwright/test');
const fs = require('node:fs');
const path = require('node:path');
const { DIST, freePort, startApi } = require('./support/api-server.cjs');

const DSN_ENV = 'AVA_TEST_POSTGRES_DSN';
const PASSWORD = 'browser-test-password-2026'; // serve_dashboard_api.AUTH_TEST_PASSWORD (fictional)
const T0 = 1763600000; // 2025-11-20 00:53:20 UTC

test.describe.configure({ mode: 'serial' });

function requireBuild() {
  if (!fs.existsSync(path.join(DIST, 'index.html'))) throw new Error('dashboard/dist missing: run `npm run build --prefix dashboard`');
}

function watch(page, base) {
  const seen = { api: [], offOrigin: [], errors: [] };
  page.on('request', request => {
    const url = new URL(request.url());
    if (url.host !== new URL(base).host && !['data:', 'blob:'].includes(url.protocol)) seen.offOrigin.push(request.url());
    if (url.pathname.startsWith('/v1/')) seen.api.push(`${request.method()} ${decodeURIComponent(url.pathname)}`);
  });
  page.on('pageerror', error => seen.errors.push(String(error)));
  return seen;
}

async function signIn(page, username) {
  await page.getByLabel('Username').fill(username);
  await page.getByLabel('Password').fill(PASSWORD);
  await page.getByRole('button', { name: 'Sign in' }).click();
}

/** Opens the stored detail of `callId` from the calls list and returns the alert's feedback block. */
async function openAlert(page, callId, alertId) {
  await page.locator(`[data-testid="call-row"][data-call-id="${callId}"]`).first().getByRole('button', { name: callId }).click();
  await expect(page.getByTestId('history-detail')).toBeVisible();
  return page.locator(`[data-testid="alert-feedback"][data-alert-id="${alertId}"]`);
}

const feedbackCalls = seen => seen.api.filter(r => r.includes('/feedback'));

test.describe('FB feedback on stored alerts with the production accounts', () => {
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
  const score = (callId, seq, ts, window, value, ema, extra = {}) => store('score.updated', callId, {
    score: value, ema, provisional: window === 0, direction: 'caller', direction_state: 'monitoring',
    direction_alert_id: null, window, scored: true, state: 'monitoring', driving_direction: 'caller',
    quality: 'ok', degraded: false, effective_stride_s: 2.0, ...extra,
  }, seq, ts);

  test.beforeAll(async () => {
    test.skip(!process.env[DSN_ENV], `set ${DSN_ENV} to a disposable PostgreSQL 16 database`);
    test.setTimeout(180000);
    requireBuild();
    const controlPort = await freePort();
    server = await startApi({
      uvArgs: ['--with-requirements', 'requirements-store.txt', '--with-requirements', 'requirements-auth.txt'],
      serverArgs: ['--control-port', String(controlPort), '--history-dsn-env', DSN_ENV, '--auth'] });
    control = `http://127.0.0.1:${controlPort}`;
    // fb1: an alert raised at window 1 and cleared at T0+12, call ended. fb2: an open alert.
    // fb3: an ended call whose alert is used for the per-alert label limit.
    await store('call.started', 'fb1', {}, 1, T0);
    await score('fb1', 2, T0 + 4, 0, 0.2, 0.2);
    await score('fb1', 3, T0 + 6, 1, 0.9, 0.6, { state: 'alert', direction_state: 'alert' });
    await store('alert.raised', 'fb1', { alert_id: 'fb1-a1', direction: 'caller', ema: 0.6, window: 1 }, 4, T0 + 6);
    await score('fb1', 5, T0 + 8, 2, 0.95, 0.8, { state: 'alert', direction_state: 'alert', direction_alert_id: 'fb1-a1' });
    await score('fb1', 6, T0 + 10, 3, 0.1, 0.4, { direction_alert_id: 'fb1-a1' });
    await store('alert.cleared', 'fb1', { alert_id: 'fb1-a1', direction: 'caller', ema: 0.3, window: 4 }, 7, T0 + 12);
    await store('call.ended', 'fb1', { final_state: 'monitoring', reason: 'bye', speech_seconds: 9.0, verdict: 'synthetic',
                                       ended_open_alert_ids: {} }, 8, T0 + 14);
    await store('call.started', 'fb2', {}, 1, T0 + 20);
    await score('fb2', 2, T0 + 22, 0, 0.9, 0.9, { state: 'alert', direction_state: 'alert' });
    await store('alert.raised', 'fb2', { alert_id: 'fb2-a1', direction: 'caller', ema: 0.9, window: 0 }, 3, T0 + 22);
    await store('call.started', 'fb3', {}, 1, T0 + 30);
    await store('alert.raised', 'fb3', { alert_id: 'fb3-a1', direction: 'caller', ema: 0.7, window: 0 }, 2, T0 + 32);
    await store('call.ended', 'fb3', { final_state: 'alert', reason: 'bye', speech_seconds: 3.0, verdict: 'synthetic',
                                       ended_open_alert_ids: {} }, 3, T0 + 34);
  });

  test.afterAll(async () => {
    if (control) await post('/drop-schema', {}).catch(() => undefined);
    if (server) await server.stop();
  });

  test('FB01 FR-A7: an analyst labels a cleared alert, changes the label, and both are stored with scores and audit rows', async ({ page }) => {
    const seen = watch(page, server.base);
    await page.goto(`${server.base}/#history`);
    await signIn(page, 'ana');
    const block = await openAlert(page, 'fb1', 'fb1-a1');
    // Collapsed: opening a call sends no feedback read.
    await expect(block.getByTestId('feedback-toggle')).toHaveText('Label this alert');
    await expect(block.getByTestId('feedback-toggle')).toHaveAttribute('aria-expanded', 'false');
    expect(feedbackCalls(seen)).toEqual([]);

    await block.getByTestId('feedback-toggle').click();
    await expect(block.getByTestId('feedback-current')).toHaveText('No label yet.');
    expect(feedbackCalls(seen)).toEqual(['GET /v1/alerts/fb1-a1/feedback']);

    await block.getByTestId('feedback-true_positive').click();
    await expect(block.getByTestId('feedback-saved')).toHaveText('Stored: True positive (synthetic speech).');
    await expect(block.getByTestId('feedback-current'))
      .toContainText('Current label: True positive (synthetic speech), by ana at ');
    let { rows } = await post('/feedback-rows', { alert_id: 'fb1-a1' });
    expect(rows.map(r => [r.label, r.username, r.alert_status])).toEqual([['true_positive', 'ana', 'cleared']]);
    // The copy holds the alert's caller windows from the raise to the clear (FR-A7 "per-window scores").
    expect(rows[0].windows_total).toBe(rows[0].windows.length);
    expect(rows[0].windows.map(w => w.window)).toEqual([1, 2, 3]);
    expect(rows[0].windows.map(w => w.score)).toEqual([0.9, 0.95, 0.1]);
    let audit = (await post('/audit', { limit: 1 })).rows;
    expect([audit[0].actor, audit[0].action, audit[0].outcome, audit[0].detail.alert_id, audit[0].detail.label])
      .toEqual(['ana', 'alerts.feedback', 'ok', 'fb1-a1', 'true_positive']);

    // A second label replaces the current one; the first stays as history.
    await block.getByTestId('feedback-false_positive').click();
    await expect(block.getByTestId('feedback-saved')).toHaveText('Stored: False positive (not synthetic speech).');
    await expect(block.getByTestId('feedback-current')).toContainText('Current label: False positive (not synthetic speech), by ana');
    await block.getByText('Earlier labels (1)').click();
    await expect(block.getByTestId('feedback-history').locator('li')).toHaveCount(1);
    await expect(block.getByTestId('feedback-history')).toContainText('True positive (synthetic speech), by ana');
    ({ rows } = await post('/feedback-rows', { alert_id: 'fb1-a1' }));
    expect(rows.map(r => r.label)).toEqual(['true_positive', 'false_positive']);
    audit = (await post('/audit', { limit: 2 })).rows;
    expect(audit.map(r => [r.action, r.detail.label])).toEqual([['alerts.feedback', 'false_positive'],
                                                                ['alerts.feedback', 'true_positive']]);

    // Stored on the server, not only on screen: a fresh page reads the same current label.
    await page.reload();
    const again = await openAlert(page, 'fb1', 'fb1-a1');
    await again.getByTestId('feedback-toggle').click();
    await expect(again.getByTestId('feedback-current')).toContainText('Current label: False positive (not synthetic speech), by ana');
    expect(feedbackCalls(seen).filter(r => r.startsWith('POST'))).toEqual(['POST /v1/alerts/fb1-a1/feedback',
                                                                           'POST /v1/alerts/fb1-a1/feedback']);
    expect(seen.offOrigin).toEqual([]);
    expect(seen.errors).toEqual([]);
  });

  test('FB02 an admin labels an open alert; the page says its scores are those received so far', async ({ page }) => {
    await page.goto(`${server.base}/#history`);
    await signIn(page, 'boss');
    const block = await openAlert(page, 'fb2', 'fb2-a1');
    await block.getByTestId('feedback-toggle').click();
    await expect(block).toContainText('The alert was still open when this call detail was loaded');
    await block.getByTestId('feedback-true_positive').click();
    await expect(block.getByTestId('feedback-current')).toContainText('by boss');
    await expect(block.getByTestId('feedback-current')).toContainText('(alert open when labelled)');
    const { rows } = await post('/feedback-rows', { alert_id: 'fb2-a1' });
    expect(rows.map(r => [r.label, r.username, r.alert_status])).toEqual([['true_positive', 'boss', 'open']]);
  });

  test('FB03 clicks while a label is being stored send nothing more: one label, not two', async ({ page }) => {
    const seen = watch(page, server.base);
    await page.goto(`${server.base}/#history`);
    await signIn(page, 'ana');
    const block = await openAlert(page, 'fb3', 'fb3-a1');
    await block.getByTestId('feedback-toggle').click();
    await expect(block.getByTestId('feedback-current')).toHaveText('No label yet.');
    // The real server answers; the POST is held until the test releases it, so every
    // further click lands mid-write whatever the timing.
    let release;
    const held = new Promise(resolve => { release = resolve; });
    await page.route('**/v1/alerts/fb3-a1/feedback', async route => {
      if (route.request().method() === 'POST') await held;
      await route.continue();
    });
    // Two clicks in one task, on the same element: no layout or timing luck involved.
    await block.getByTestId('feedback-true_positive').evaluate(button => { button.click(); button.click(); });
    await block.getByTestId('feedback-false_positive').evaluate(button => button.click());
    await expect(block.getByTestId('feedback-true_positive')).toHaveAttribute('aria-disabled', 'true');
    // Review M1: the panel cannot be hidden while its label is being stored.
    await expect(block.getByTestId('feedback-toggle')).toHaveAttribute('aria-disabled', 'true');
    // force: Playwright itself waits for aria-disabled to clear; a user's click does not.
    await block.getByTestId('feedback-toggle').click({ force: true });
    await block.getByTestId('feedback-true_positive').click({ force: true });
    await expect(block.getByTestId('feedback-toggle')).toHaveAttribute('aria-expanded', 'true');
    await expect(block.getByTestId('feedback-status')).toContainText('Storing');
    release();
    await expect(block.getByTestId('feedback-saved')).toHaveText('Stored: True positive (synthetic speech).');
    await expect(block.getByTestId('feedback-toggle')).not.toHaveAttribute('aria-disabled', 'true');
    await expect(block.getByTestId('feedback-true_positive')).not.toHaveAttribute('aria-disabled', 'true');
    await page.waitForTimeout(500);
    expect(feedbackCalls(seen).filter(r => r.startsWith('POST'))).toHaveLength(1);
    const { rows } = await post('/feedback-rows', { alert_id: 'fb3-a1' });
    expect(rows.map(r => r.label)).toEqual(['true_positive']);
    await page.unroute('**/v1/alerts/fb3-a1/feedback');
  });

  test('FB04 the per-alert label limit: the server refusal is shown, then the buttons are disabled', async ({ page }) => {
    await page.goto(`${server.base}/#history`);
    await signIn(page, 'ana');
    const block = await openAlert(page, 'fb3', 'fb3-a1');
    await block.getByTestId('feedback-toggle').click();
    await expect(block.getByTestId('feedback-current')).toContainText('by ana');
    // 1 label from FB03; 19 more from the same session, sent past the panel (as another tab
    // would), reach the limit of 20. From the page: its Secure cookie is not sent by
    // Playwright's own request client over the loopback http origin.
    const statuses = await page.evaluate(async () => {
      const out = [];
      for (let i = 0; i < 19; i += 1) {
        const response = await fetch('v1/alerts/fb3-a1/feedback', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ label: i % 2 ? 'true_positive' : 'false_positive' }) });
        out.push(response.status);
      }
      return out;
    });
    expect(statuses).toEqual(Array(19).fill(201));
    await block.getByTestId('feedback-false_positive').click(); // the page still shows 1 label
    await expect(block.getByTestId('feedback-error'))
      .toHaveText('This alert already has 20 labels, the most the server keeps; no further label was stored.');
    await block.getByTestId('feedback-reload').click();
    await expect(block.getByTestId('feedback-full')).toBeVisible();
    await expect(block.getByTestId('feedback-true_positive')).toBeDisabled();
    await expect(block.getByTestId('feedback-false_positive')).toBeDisabled();
    await block.getByText('Earlier labels (19)').click();
    await expect(block.getByTestId('feedback-history').locator('li')).toHaveCount(19);
    expect((await post('/feedback-rows', { alert_id: 'fb3-a1' })).rows).toHaveLength(20);
    const audit = (await post('/audit', { limit: 1 })).rows;
    expect([audit[0].actor, audit[0].action, audit[0].outcome, audit[0].detail.status])
      .toEqual(['ana', 'alerts.feedback', 'refused', 409]); // the refusal is audited
  });

  test('FB05 review M1: leaving the page during a write does not cancel it; the label is stored once', async ({ page }) => {
    const seen = watch(page, server.base);
    const failed = [];
    page.on('requestfailed', request => failed.push(request.url()));
    await page.goto(`${server.base}/#history`);
    await signIn(page, 'ana');
    const block = await openAlert(page, 'fb2', 'fb2-a1');
    await block.getByTestId('feedback-toggle').click();
    await expect(block.getByTestId('feedback-current')).toContainText('by boss'); // FB02's label
    let answered;
    const done = new Promise(resolve => { answered = resolve; });
    await page.route('**/v1/alerts/fb2-a1/feedback', async route => {
      if (route.request().method() !== 'POST') return route.continue();
      await new Promise(r => setTimeout(r, 1000));
      await route.continue();
      answered();
    });
    await block.getByTestId('feedback-false_positive').click();
    await page.getByRole('link', { name: 'Batch analysis' }).click(); // the panel unmounts mid-write
    await expect(page.getByTestId('dropzone')).toBeVisible();
    await done;
    await expect.poll(async () => (await post('/feedback-rows', { alert_id: 'fb2-a1' })).rows.map(r => [r.label, r.username]))
      .toEqual([['true_positive', 'boss'], ['false_positive', 'ana']]);
    expect(failed).toEqual([]); // the write was not aborted by the browser
    expect(feedbackCalls(seen).filter(r => r.startsWith('POST'))).toHaveLength(1);
    expect(seen.errors).toEqual([]);
    await page.unroute('**/v1/alerts/fb2-a1/feedback');
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

test.describe('FN feedback without a database', () => {
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

  test('FN01 [mock history] a server without accounts shows no feedback controls and sends no feedback request', async ({ page }) => {
    const seen = watch(page, server.base);
    await storedHistory(page);
    await page.goto(`${server.base}/#history`);
    await page.getByRole('button', { name: 'm1', exact: true }).click();
    await expect(page.getByTestId('session-alerts')).toContainText('m1-a1');
    await expect(page.getByTestId('alert-feedback')).toHaveCount(0);
    expect(feedbackCalls(seen)).toEqual([]);
    expect(seen.api).toContain('GET /v1/auth/me'); // the real server answered 404: no accounts
  });

  test('FN02 [mock] read and write failures are explained; an unanswered write is never retried and says its outcome is unknown', async ({ page }) => {
    await storedHistory(page);
    await page.route('**/v1/auth/me', route => json(route, 200, USER));
    let read = 503;
    const posts = [];
    let write = 'abort';
    await page.route('**/v1/alerts/m1-a1/feedback', async route => {
      if (route.request().method() === 'GET') {
        return read === 503 ? json(route, 503, { detail: 'Feedback store unavailable (busy)' })
          : json(route, 200, { alert_id: 'm1-a1', current: null, feedback: [], feedback_total: 0, feedback_truncated: false });
      }
      posts.push(route.request().postDataJSON());
      if (write === 'abort') return route.abort('connectionreset');
      if (write === 'schema') return json(route, 503, { detail: 'Feedback not stored (schema)' });
      if (write === 'commit') return json(route, 503, { detail: 'Feedback not stored (unavailable)' });
      if (write === 'unreadable') return json(route, 201, { alert_id: 'm1-a1', label: 'false_positive' }); // stored, body short
      return json(route, 200, { alert_id: 'm1-a1', label: 'false_positive' }); // wrong status and body
    });
    await page.goto(`${server.base}/#history`);
    await expect(page.getByTestId('account-user')).toHaveText('ana');
    await page.getByRole('button', { name: 'm1', exact: true }).click();
    const block = page.locator('[data-testid="alert-feedback"][data-alert-id="m1-a1"]');
    await block.getByTestId('feedback-toggle').click();
    await expect(block.getByTestId('feedback-read-error'))
      .toHaveText('Labels could not be read: the account store is busy; try again shortly.');
    await expect(block.getByTestId('feedback-true_positive')).toHaveCount(0); // nothing to label against
    read = 200;
    await block.getByTestId('feedback-reload').click();
    await expect(block.getByTestId('feedback-current')).toHaveText('No label yet.');

    await block.getByTestId('feedback-true_positive').click();
    await expect(block.getByTestId('feedback-error')).toHaveText(
      'The service could not be reached. The label may or may not have been stored: reload the labels to check.');
    await page.waitForTimeout(1000); // a delayed retry would be sent by now (review L5)
    expect(posts).toEqual([{ label: 'true_positive' }]); // exactly one attempt
    await expect(block.getByTestId('feedback-current')).toHaveText('No label yet.'); // nothing claimed

    write = 'schema';
    await block.getByTestId('feedback-false_positive').click();
    await expect(block.getByTestId('feedback-error')).toHaveText('The label was not stored: the database is not migrated for feedback.');
    write = 'wrong';
    await block.getByTestId('feedback-false_positive').click();
    await expect(block.getByTestId('feedback-error')).toHaveText('The service answered with HTTP 200.');
    // Review L1: a connection lost at COMMIT may follow a stored label.
    write = 'commit';
    await block.getByTestId('feedback-false_positive').click();
    await expect(block.getByTestId('feedback-error')).toHaveText(
      'The label may or may not have been stored (the database cannot be reached): reload the labels to check.');
    // Review L5: a 201 whose body cannot be read is reported as stored, not as a failure.
    write = 'unreadable';
    await block.getByTestId('feedback-false_positive').click();
    await expect(block.getByTestId('feedback-error')).toHaveText(
      'The label was stored, but the answer could not be read: reload the labels to see it.');
    await page.waitForTimeout(500);
    expect(posts).toHaveLength(5);
  });

  test('FN03 [mock] a 401 while labelling sends the page back to sign-in', async ({ page }) => {
    await storedHistory(page);
    let signedIn = true;
    await page.route('**/v1/auth/me', route => (signedIn ? json(route, 200, USER) : json(route, 401, { detail: 'Login required' })));
    await page.route('**/v1/alerts/m1-a1/feedback', route => {
      if (route.request().method() === 'GET') {
        return json(route, 200, { alert_id: 'm1-a1', current: null, feedback: [], feedback_total: 0, feedback_truncated: false });
      }
      signedIn = false;
      return json(route, 401, { detail: 'Login required' });
    });
    await page.goto(`${server.base}/#history`);
    await page.getByRole('button', { name: 'm1', exact: true }).click();
    const block = page.locator('[data-testid="alert-feedback"][data-alert-id="m1-a1"]');
    await block.getByTestId('feedback-toggle').click();
    await block.getByTestId('feedback-true_positive').click();
    await expect(page.getByTestId('login-notice')).toHaveText('Your session has ended. Sign in again.');
    await expect(page.getByTestId('history-detail')).toHaveCount(0);
  });
});
