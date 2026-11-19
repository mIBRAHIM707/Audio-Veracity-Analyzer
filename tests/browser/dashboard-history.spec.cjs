// Claude-authored browser cases (19 November 2026) for backlog 7.3 / PRD FR-U6 (history
// and search over past calls and alerts) on FR-A3 (full history in PostgreSQL) and FR-A6
// (alert search, CSV export), plus FR-U8 (same origin). Authored by Claude (Claude Code),
// not GPT-6 Astra.
//
// HB cases: the production build served by the real analysis API whose history routes
// read a real PostgreSQL database through `PostgresCallHistory`; the rows are written by
// the production `PostgresEventStore` from FR-A4 envelopes (serve_dashboard_api.py
// --history-dsn-env). They need AVA_TEST_POSTGRES_DSN naming a disposable database
// (services/store/README.md) and are skipped without it. HM cases need no database: one
// uses the real API without a store (its real 503), the others are [mock] (Playwright
// route) to reach failures and races the server cannot be put into on demand.
// Infrastructure evidence only: no detector is involved. Build first:
// `npm run build --prefix dashboard`.
const { test, expect } = require('@playwright/test');
const fs = require('node:fs');
const path = require('node:path');
const { DIST, freePort, startApi } = require('./support/api-server.cjs');

const DSN_ENV = 'AVA_TEST_POSTGRES_DSN';
const T0 = 1763500000; // 2025-11-18 21:06:40 UTC
const CALLER_NUMBER = '+923001234567';
const HOSTILE_ID = '<img src=x onerror="window.__xss=1">';

test.describe.configure({ mode: 'serial' });

function requireBuild() {
  if (!fs.existsSync(path.join(DIST, 'index.html'))) throw new Error('dashboard/dist missing: run `npm run build --prefix dashboard`');
}

async function open(page, base) {
  const offOrigin = [];
  page.on('request', request => {
    const url = new URL(request.url());
    if (url.host !== new URL(base).host && !['data:', 'blob:'].includes(url.protocol)) offOrigin.push(request.url());
  });
  const errors = [];
  page.on('pageerror', error => errors.push(String(error)));
  const csp = [];
  page.on('console', message => { if (/Content Security Policy/i.test(message.text())) csp.push(message.text()); });
  const sockets = [];
  page.on('websocket', ws => sockets.push(ws.url()));
  await page.goto(`${base}/#history`);
  return { offOrigin, errors, csp, sockets };
}

const callRows = page => page.getByTestId('call-row');
const alertIds = page => page.getByTestId('alert-row').evaluateAll(rows => rows.map(r => r.dataset.alertId));

test.describe('HB stored history from PostgreSQL', () => {
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
    direction_alert_id: null, window, scored: value !== null, state: 'monitoring', driving_direction: 'caller',
    quality: 'ok', degraded: false, effective_stride_s: 2.0, ...extra,
  }, seq, ts);
  const ended = (callId, seq, ts, finalState = 'monitoring') => store('call.ended', callId, {
    final_state: finalState, reason: 'bye', speech_seconds: 7.5, verdict: 'genuine', ended_open_alert_ids: {},
  }, seq, ts);
  const raised = (callId, seq, ts, alertId, ema, window, direction = 'caller') =>
    store('alert.raised', callId, { alert_id: alertId, direction, ema, window }, seq, ts);

  test.beforeAll(async () => {
    test.skip(!process.env[DSN_ENV], `set ${DSN_ENV} to a disposable PostgreSQL 16 database`);
    test.setTimeout(180000);
    requireBuild();
    const controlPort = await freePort();
    server = await startApi({ uvArgs: ['--with-requirements', 'requirements-store.txt'],
                              serverArgs: ['--control-port', String(controlPort), '--history-dsn-env', DSN_ENV] });
    control = `http://127.0.0.1:${controlPort}`;
    // The same stored alerts as tests/unit/test_alert_search.py (raised, EMA at raise, peak):
    //   k1-a1 T0+6 0.55/0.55 ended_open (session killed, superseded by the re-tap)
    //   k1-a2 T0+24 0.70/0.80 cleared at T0+28 (call k2)
    //   o-a1 T0+54 0.90/0.95 open; o-a0 T0+54 no direction, peak 0.97, open
    //   d-a1 T0+60 0.61/0.61 ended_open (call done1)
    await store('call.started', 'k1', { from: CALLER_NUMBER }, 1, T0);
    await score('k1', 2, T0 + 4, 0, 0.2, 0.2);
    await score('k1', 3, T0 + 6, 1, 0.9, 0.55, { state: 'alert', direction_state: 'alert' });
    await raised('k1', 4, T0 + 6, 'k1-a1', 0.55, 1);
    await store('call.started', 'k1', { from: CALLER_NUMBER }, 1, T0 + 20); // the re-tap: a new session
    await score('k1', 2, T0 + 24, 0, 0.1, 0.1);
    await score('k1', 3, T0 + 26, 0, 0.3, 0.2, { direction: 'agent' });
    await ended('k1', 4, T0 + 30);
    await store('call.started', 'k2', { from: CALLER_NUMBER }, 1, T0 + 20);
    await raised('k2', 2, T0 + 24, 'k1-a2', 0.7, 2);
    await score('k2', 3, T0 + 26, 3, 0.9, 0.8, { state: 'alert' });
    await store('alert.cleared', 'k2', { alert_id: 'k1-a2', direction: 'caller', ema: 0.3, window: 4 }, 4, T0 + 28);
    await ended('k2', 5, T0 + 31);
    await store('call.started', 'open1', {}, 1, T0 + 50);
    await score('open1', 2, T0 + 52, 0, 0.99, 0.99);
    await raised('open1', 3, T0 + 54, 'o-a1', 0.9, 3);
    await store('alert.raised', 'open1', { alert_id: 'o-a0' }, 4, T0 + 54);
    await score('open1', 5, T0 + 56, 1, 0.97, 0.95, { state: 'alert' });
    await score('open1', 6, T0 + 58, 0, 0.97, 0.97, { state: 'alert', direction: 'agent' });
    await store('call.started', 'done1', {}, 1, T0 + 59);
    await raised('done1', 2, T0 + 60, 'd-a1', 0.61, 0, 'agent');
    await ended('done1', 3, T0 + 61, 'alert');
    await store('call.started', HOSTILE_ID, {}, 1, T0 + 70);
    await ended(HOSTILE_ID, 2, T0 + 71);
  });

  test.afterAll(async () => {
    if (control) await post('/drop-schema', {}).catch(() => undefined);
    if (server) await server.stop();
  });

  test('HB01 FR-U6/U8: stored calls and alerts newest first; same origin, no socket, no caller data', async ({ page }) => {
    const { offOrigin, errors, csp, sockets } = await open(page, server.base);
    await expect(page).toHaveTitle('AVA history');
    await expect(page.getByRole('link', { name: 'History' })).toHaveAttribute('aria-current', 'page');
    await expect(page.getByRole('complementary', { name: 'Limitations' })).toContainText('not a voice biometric');
    await expect(callRows(page)).toHaveCount(6);
    expect(await callRows(page).evaluateAll(rows => rows.map(r => r.dataset.callId)))
      .toEqual([HOSTILE_ID, 'done1', 'open1', 'k2', 'k1', 'k1']);
    const killed = callRows(page).nth(5);
    await expect(killed.getByTestId('call-end')).toHaveText('monitor_lost (inferred: monitor lost)');
    await expect(killed.getByTestId('call-alerts')).toHaveText('0 / 1');
    await expect(callRows(page).nth(2).getByTestId('call-end')).toHaveText('Open (no end stored)');
    await expect(callRows(page).nth(2).getByTestId('call-alerts')).toHaveText('2 / 2');
    await expect(page.getByTestId('calls-more')).toHaveCount(0);
    await expect(page.getByTestId('alert-row')).toHaveCount(5);
    expect(await alertIds(page)).toEqual(['d-a1', 'o-a1', 'o-a0', 'k1-a2', 'k1-a1']);
    const cleared = page.locator('[data-testid="alert-row"][data-alert-id="k1-a2"]');
    await expect(cleared.getByTestId('alert-peak')).toHaveText('0.80');
    await expect(cleared.getByTestId('alert-state-text')).toHaveText('Cleared');
    await expect(cleared).toContainText('2025-11-18 21:07:04 UTC'); // raised T0+24
    await expect(page.locator('[data-testid="alert-row"][data-alert-id="k1-a1"]').getByTestId('alert-state-text'))
      .toHaveText('Open at call end');
    await expect(page.locator('body')).not.toContainText(CALLER_NUMBER);
    expect(sockets).toEqual([]); // the history page opens no live event connection
    expect(offOrigin).toEqual([]);
    expect(errors).toEqual([]);
    expect(csp).toEqual([]);
  });

  test('HB02 FR-A6 filters: state, UTC time range and minimum confidence reach the store', async ({ page }) => {
    await open(page, server.base);
    await expect(page.getByTestId('alert-row')).toHaveCount(5);
    const search = async () => {
      const answered = page.waitForResponse(r => r.url().includes('/v1/alerts?'));
      await page.getByRole('button', { name: 'Search' }).click();
      return new URL((await answered).url()).searchParams;
    };
    await page.getByTestId('alert-state').selectOption('open');
    let params = await search();
    expect(params.get('state')).toBe('open');
    await expect.poll(() => alertIds(page)).toEqual(['o-a1', 'o-a0']);
    await page.getByTestId('alert-state').selectOption('all');
    await page.getByTestId('alert-min').fill('0.96');
    params = await search();
    expect(params.get('min_confidence')).toBe('0.96');
    await expect.poll(() => alertIds(page)).toEqual(['o-a0']); // peak 0.97; o-a1 peaks at 0.95
    await page.getByTestId('alert-min').fill('');
    // since inclusive, until exclusive: T0+24 .. T0+54 holds k1-a2 only (o-a1/o-a0 raised at T0+54).
    await page.getByTestId('alert-since').fill('2025-11-18T21:07:04');
    await page.getByTestId('alert-until').fill('2025-11-18T21:07:34');
    params = await search();
    expect([params.get('since'), params.get('until')]).toEqual([String(T0 + 24), String(T0 + 54)]);
    await expect.poll(() => alertIds(page)).toEqual(['k1-a2']);
    // A filter nothing matches.
    await page.getByTestId('alert-since').fill('2030-01-01T00:00'); // Chromium drops ":00" seconds
    await page.getByTestId('alert-until').fill('');
    await search();
    await expect(page.getByTestId('alerts-status')).toContainText('No alerts match.');
    await expect(page.getByTestId('alert-row')).toHaveCount(0);
  });

  test('HB03 refused filters are explained and never sent', async ({ page }) => {
    await open(page, server.base);
    await expect(page.getByTestId('alert-row')).toHaveCount(5);
    const sent = [];
    page.on('request', r => { if (r.url().includes('/v1/alerts')) sent.push(r.url()); });
    const searchButton = page.getByRole('button', { name: 'Search' });
    await page.getByTestId('alert-since').fill('2025-11-18T21:08');
    await page.getByTestId('alert-until').fill('2025-11-18T21:07');
    await searchButton.click();
    await expect(page.getByTestId('alert-form-error')).toHaveText('"From" must be earlier than "to".');
    await page.getByTestId('alert-until').fill('');
    await page.getByTestId('alert-min').fill('1.5');
    await searchButton.click();
    await expect(page.getByTestId('alert-form-error')).toHaveText('Minimum confidence must be from 0 to 1.');
    // Review M1: unfinished input reads as '' in the browser and must not become "no filter".
    await page.getByTestId('alert-min').fill('');
    await page.getByTestId('alert-since').fill('');
    await page.getByTestId('alert-since').focus();
    await page.keyboard.type('11'); // only the month segment of the date
    expect(await page.getByTestId('alert-since').evaluate(el => [el.value, el.validity.badInput])).toEqual(['', true]);
    await searchButton.click();
    await expect(page.getByTestId('alert-form-error')).toContainText('Complete or correct "Raised from"');
    await page.getByTestId('alert-since').fill('');
    await page.getByTestId('alert-min').pressSequentially('1e');
    expect(await page.getByTestId('alert-min').evaluate(el => [el.value, el.validity.badInput])).toEqual(['', true]);
    await searchButton.click();
    await expect(page.getByTestId('alert-form-error')).toContainText('Complete or correct "Minimum confidence"');
    expect(sent).toEqual([]);
    await expect(page.getByTestId('alert-row')).toHaveCount(5); // the earlier results stay
  });

  test('HB04 FR-A3 detail: every stored session of a call, the killed one inferred, no caller data', async ({ page }) => {
    await open(page, server.base);
    await page.locator('[data-testid="alert-row"][data-alert-id="k1-a1"]').getByRole('button', { name: 'k1' }).click();
    const detail = page.getByTestId('history-detail');
    await expect(detail).toHaveAttribute('data-call-id', 'k1');
    await expect(detail.getByTestId('session')).toHaveCount(2);
    const [newest, killed] = [detail.getByTestId('session').nth(0), detail.getByTestId('session').nth(1)];
    expect(Number(await newest.getAttribute('data-session'))).toBeGreaterThan(Number(await killed.getAttribute('data-session')));
    await expect(detail).toContainText('2 stored monitoring sessions');
    await expect(newest.getByTestId('session-end')).toHaveText('bye');
    await expect(newest.getByTestId('session-verdict')).toHaveText('genuine');
    await expect(newest.getByTestId('timeline-row')).toHaveCount(2);
    await expect(newest.getByTestId('timeline-row').nth(1)).toContainText('agent window 0');
    await expect(killed.getByTestId('session-end')).toHaveText('monitor_lost (inferred: monitor lost)');
    await expect(killed.getByTestId('session-verdict')).toHaveText('—');
    await expect(killed.getByTestId('session-alerts')).toContainText('k1-a1 (caller): Open at call end');
    // Bars in seconds since the session started: windows at +4 s and +6 s, 2 s stride.
    await expect(killed.getByTestId('timeline-row').nth(0)).toContainText('2.0–4.0');
    await expect(killed.getByTestId('timeline-row').nth(1)).toContainText('4.0–6.0');
    // Review M2: uncalibrated scores are never labelled as probabilities here.
    await expect(killed.getByTestId('timeline-row').nth(1)).toContainText('at or above 0.5');
    await expect(killed.getByRole('columnheader', { name: 'Score (uncalibrated)' })).toBeVisible();
    await expect(detail).not.toContainText('p(synthetic)');
    await expect(detail).not.toContainText('scored synthetic');
    expect(await killed.locator('svg').getAttribute('aria-label')).toMatch(/^Per-window detector score \(uncalibrated, not a probability\)/);
    await expect(page.locator('body')).not.toContainText(CALLER_NUMBER);
    await detail.getByRole('button', { name: 'Close' }).click();
    await expect(page.getByTestId('history-detail')).toHaveCount(0);
    // Focus returns to the button that opened the detail (review L6).
    await expect(page.locator('[data-testid="alert-row"][data-alert-id="k1-a1"]').getByRole('button', { name: 'k1' }))
      .toBeFocused();
  });

  test('HB05 hostile stored text is shown as text and never runs', async ({ page }) => {
    const { errors } = await open(page, server.base);
    const row = callRows(page).first();
    await expect(row.getByRole('button')).toHaveText(HOSTILE_ID);
    await row.getByRole('button').click();
    await expect(page.getByTestId('history-detail').getByRole('heading', { level: 2 })).toContainText(HOSTILE_ID);
    await expect(page.getByTestId('history-detail').getByTestId('session')).toHaveCount(1);
    await expect(page.getByTestId('history-detail').getByTestId('timeline-empty')).toBeVisible();
    expect(await page.evaluate(() => window.__xss)).toBeUndefined();
    expect(await page.locator('img').count()).toBe(0);
    expect(errors).toEqual([]);
  });

  test('HB06 FR-A6 CSV export of the current filters, through the real API', async ({ page }) => {
    await open(page, server.base);
    await page.getByTestId('alert-state').selectOption('ended_open');
    await page.getByRole('button', { name: 'Search' }).click();
    await expect.poll(() => alertIds(page)).toEqual(['d-a1', 'k1-a1']);
    const asked = page.waitForRequest(r => r.url().includes('format=csv'));
    const downloaded = page.waitForEvent('download');
    await page.getByTestId('alert-export').click();
    const params = new URL((await asked).url()).searchParams;
    expect([params.get('state'), params.get('limit'), params.get('format')]).toEqual(['ended_open', '10000', 'csv']);
    const download = await downloaded;
    expect(download.suggestedFilename()).toBe('ava-alerts.csv');
    const text = fs.readFileSync(await download.path(), 'utf8');
    const lines = text.trim().split(/\r\n/);
    expect(lines).toHaveLength(3);
    expect(lines[0]).toMatch(/^alert_id,/);
    expect(lines.slice(1).map(l => l.split(',')[0])).toEqual(['d-a1', 'k1-a1']);
    expect(text).not.toContain(CALLER_NUMBER);
    await expect(page.getByTestId('alert-export-status')).toHaveText('Exported every alert matching the search shown.');
  });

  test('HB07 paging: older calls and more alerts load without repeats until the last page', async ({ page }) => {
    test.setTimeout(120000);
    // 55 more calls (one alert each): 61 calls and 60 alerts in all.
    for (let i = 0; i < 55; i += 1) {
      const id = `bulk-${String(i).padStart(2, '0')}`;
      await store('call.started', id, {}, 1, T0 + 100 + i);
      await raised(id, 2, T0 + 100 + i, `${id}-a`, 0.7, 0);
    }
    await open(page, server.base);
    await expect(callRows(page)).toHaveCount(50);
    const more = page.waitForResponse(r => r.url().includes('/v1/history/calls?') && r.url().includes('before='));
    await page.getByTestId('calls-more').click();
    await more;
    await expect(callRows(page)).toHaveCount(61);
    const sessions = await callRows(page).evaluateAll(rows => rows.map(r => Number(r.dataset.session)));
    expect(new Set(sessions).size).toBe(61);
    expect([...sessions].sort((a, b) => b - a)).toEqual(sessions); // newest first throughout
    await expect(page.getByTestId('calls-more')).toHaveCount(0);
    // Review M3: the filter must reach the store; 55 bulk calls + open1 have no stored end.
    const filtered = page.waitForResponse(r => r.url().includes('/v1/history/calls?') && r.url().includes('status=active'));
    await page.getByTestId('calls-filter').selectOption('active');
    await filtered;
    await expect(callRows(page)).toHaveCount(50);
    const filteredMore = page.waitForResponse(r => r.url().includes('status=active') && r.url().includes('before='));
    await page.getByTestId('calls-more').click();
    await filteredMore;
    await expect(callRows(page)).toHaveCount(56);
    expect(new Set(await page.getByTestId('call-end').allTextContents())).toEqual(new Set(['Open (no end stored)']));
    expect(await callRows(page).evaluateAll(rows => rows.map(r => r.dataset.callId))).toContain('open1');
    await expect(page.getByTestId('calls-more')).toHaveCount(0);
    // Alerts: the first page is 100, so all 60 arrive at once and no "Load more" shows.
    await expect(page.getByTestId('alert-row')).toHaveCount(60);
    await expect(page.getByTestId('alerts-more')).toHaveCount(0);
  });
});

test.describe('HM without a history database', () => {
  let server;

  test.beforeAll(async () => {
    test.setTimeout(180000);
    requireBuild();
    server = await startApi();
  });

  test.afterAll(async () => { if (server) await server.stop(); });

  test('HM01 a server without a store: both lists say so; the other pages are untouched', async ({ page }) => {
    const { errors } = await open(page, server.base);
    await expect(page.getByTestId('calls-error')).toHaveText(
      'Call history is not available: this server has no history database configured.');
    await expect(page.getByTestId('alerts-error')).toHaveText(
      'Call history is not available: this server has no history database configured.');
    await page.getByRole('link', { name: 'Batch analysis' }).click();
    await expect(page.getByTestId('dropzone')).toBeVisible();
    expect(errors).toEqual([]);
  });

  test('HM02 [mock] store failures, unreadable answers and a lost network each get their own text', async ({ page }) => {
    let mode = 'timeout';
    await page.route('**/v1/alerts?**', route => {
      if (mode === 'timeout') return route.fulfill({ status: 503, json: { detail: 'Call history store unavailable (timeout)' } });
      if (mode === 'malformed') return route.fulfill({ status: 200, json: { alerts: [{ alert_id: 'x', state: 'gone' }], next_cursor: null } });
      return route.abort('internetdisconnected');
    });
    await page.route('**/v1/history/calls?**', route => route.fulfill({ status: 200, json: { calls: 'nope', next_before: null } }));
    await open(page, server.base);
    await expect(page.getByTestId('alerts-error')).toHaveText(
      'Call history is not available: the read took too long; narrow the filters.');
    await expect(page.getByTestId('calls-error')).toHaveText('The service returned call history this page cannot read.');
    mode = 'malformed';
    await page.getByRole('button', { name: 'Search' }).click();
    await expect(page.getByTestId('alerts-error')).toHaveText('The service returned call history this page cannot read.');
    mode = 'offline';
    await page.getByRole('button', { name: 'Search' }).click();
    await expect(page.getByTestId('alerts-error')).toHaveText('The history service could not be reached.');
  });

  test('HM03 [mock] a slow older search never overwrites a newer one', async ({ page }) => {
    const row = (id, ts) => ({ alert_id: id, direction: 'caller', state: 'open', raised_ts: ts, raised_window: 0,
                               raised_ema: 0.6, closed_ts: null, peak_ema: 0.6, call_id: 'c', session: 1 });
    let release;
    const held = new Promise(resolve => { release = resolve; });
    let oldAnswered;
    const oldDone = new Promise(resolve => { oldAnswered = resolve; });
    await page.route('**/v1/history/calls?**', route => route.fulfill({ status: 200, json: { calls: [], next_before: null } }));
    await page.route('**/v1/alerts?**', async route => {
      const state = new URL(route.request().url()).searchParams.get('state');
      if (state === 'open') {
        await held; // the older search answers last
        try {
          await route.fulfill({ status: 200, json: { alerts: [row('old', T0)], next_cursor: null } });
        } catch { /* the page already aborted it */ }
        oldAnswered();
        return undefined;
      }
      return route.fulfill({ status: 200, json: { alerts: state === 'cleared' ? [row('new', T0 + 1)] : [], next_cursor: null } });
    });
    await open(page, server.base);
    await expect(page.getByTestId('alerts-status')).toContainText('No alerts match.');
    await page.getByTestId('alert-state').selectOption('open');
    const slow = page.waitForRequest(r => r.url().includes('state=open'));
    await page.getByRole('button', { name: 'Search' }).click();
    await slow;
    await page.getByTestId('alert-state').selectOption('cleared');
    await expect(page.getByTestId('alerts-status')).toContainText('Loading alerts');
    await page.getByTestId('alert-min').press('Enter'); // a new search while the older one runs
    await expect.poll(() => alertIds(page)).toEqual(['new']);
    release();
    await oldDone;
    // Let the page handle the late answer (two animation frames) before looking again.
    await page.evaluate(() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))));
    expect(await alertIds(page)).toEqual(['new']);
  });

  test('HM04 [mock] export: the search shown is exported; more matches than exported is said; a refusal is an error', async ({ page }) => {
    let refuse = false;
    const csvAsks = [];
    await page.route('**/v1/history/calls?**', route => route.fulfill({ status: 200, json: { calls: [], next_before: null } }));
    await page.route('**/v1/alerts?**', route => {
      const params = new URL(route.request().url()).searchParams;
      if (params.get('format') !== 'csv') return route.fulfill({ status: 200, json: { alerts: [], next_cursor: null } });
      csvAsks.push(params);
      if (refuse) return route.fulfill({ status: 503, json: { detail: 'Call history store unavailable (unavailable)' } });
      return route.fulfill({ status: 200, contentType: 'text/csv; charset=utf-8', body: 'alert_id\r\nx\r\n',
                             headers: { 'X-AVA-Next-Cursor': 'abc' } });
    });
    await open(page, server.base);
    await expect(page.getByTestId('alerts-status')).toContainText('No alerts match.');
    await page.getByTestId('alert-state').selectOption('cleared');
    await page.getByRole('button', { name: 'Search' }).click();
    await expect(page.getByTestId('alerts-status')).toContainText('No alerts match.');
    // Edited but not searched: the export still follows the search shown (review L1).
    await page.getByTestId('alert-state').selectOption('open');
    await page.getByTestId('alert-min').fill('0.9');
    const downloaded = page.waitForEvent('download');
    await page.getByTestId('alert-export').click();
    await downloaded;
    expect([csvAsks[0].get('state'), csvAsks[0].get('min_confidence')]).toEqual(['cleared', null]);
    await expect(page.getByTestId('alert-export-status'))
      .toContainText('Exported the newest 10,000 alerts matching the search shown; more match.');
    refuse = true;
    await page.getByTestId('alert-export').click();
    await expect(page.getByTestId('alert-export-error'))
      .toHaveText('Call history is not available: the history database cannot be reached.');
  });

  test('HM06 [mock] a search made while the calls list loads is not replaced by the default search', async ({ page }) => {
    let release;
    const held = new Promise(resolve => { release = resolve; });
    const asked = [];
    await page.route('**/v1/history/calls?**', async route => {
      await held;
      await route.fulfill({ status: 200, json: { calls: [], next_before: null } }).catch(() => undefined);
    });
    await page.route('**/v1/alerts?**', route => {
      const params = new URL(route.request().url()).searchParams;
      asked.push(params.toString());
      if (params.get('format') === 'csv') {
        return route.fulfill({ status: 200, contentType: 'text/csv; charset=utf-8', body: 'alert_id\r\n' });
      }
      return route.fulfill({ status: 200, json: { alerts: [], next_cursor: null } });
    });
    await open(page, server.base);
    await page.getByTestId('alert-state').selectOption('open');
    await page.getByRole('button', { name: 'Search' }).click();
    await expect(page.getByTestId('alerts-status')).toContainText('No alerts match.');
    release();
    await expect(page.getByTestId('calls-status')).toContainText('No stored calls match.');
    await page.evaluate(() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))));
    const downloaded = page.waitForEvent('download');
    await page.getByTestId('alert-export').click();
    await downloaded;
    expect(asked).toEqual(['state=open&limit=100', 'state=open&limit=10000&format=csv']);
  });

  test('HM05 [mock] list bounds and a vanished call', async ({ page }) => {
    const call = (session, id) => ({ session, call_id: id, status: 'ended', started_ts: T0, started_seen: true, state: null,
      ended_ts: T0 + 1, last_event_ts: T0 + 1, final_state: 'monitoring', end_reason: 'bye', end_inferred: false,
      speech_seconds: 1, verdict: null, degraded: false, quality: 'ok', alerts_total: 0, alerts_open: 0,
      windows_total: 0, windows_scored: 0, score_max: null });
    // An endless store: every page is full and points to the next one.
    await page.route('**/v1/history/calls?**', route => {
      const before = Number(new URL(route.request().url()).searchParams.get('before') ?? 100000);
      const calls = Array.from({ length: 50 }, (_, i) => call(before - 1 - i, `c${before - 1 - i}`));
      return route.fulfill({ status: 200, json: { calls, next_before: before - 50 } });
    });
    await page.route('**/v1/alerts?**', route => route.fulfill({ status: 200, json: { alerts: [], next_cursor: null } }));
    await page.route('**/v1/calls/*/history', route => route.fulfill({ status: 404, json: { detail: 'No stored history for this call' } }));
    await open(page, server.base);
    for (let loaded = 50; loaded < 1000; loaded += 50) {
      await expect(callRows(page)).toHaveCount(loaded);
      await page.getByTestId('calls-more').click();
    }
    await expect(callRows(page)).toHaveCount(1000);
    await expect(page.getByTestId('calls-capped')).toContainText('Showing the first 1,000 stored calls');
    await expect(page.getByTestId('calls-more')).toHaveCount(0);
    await callRows(page).first().getByRole('button').click();
    await expect(page.getByTestId('detail-error')).toHaveText('No stored history for this call.');
  });
});
