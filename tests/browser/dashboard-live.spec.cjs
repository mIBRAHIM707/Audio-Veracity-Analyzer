// Claude-authored browser cases (16 November 2026) for backlog 7.1 / PRD FR-U1 (live
// call board), FR-U2 (alert feed, sound and visual notification), FR-U3 (call
// timeline), FR-U8 (same origin) and FR-A3/FR-A4 consumption. Authored by Claude
// (Claude Code), not GPT-6 Astra.
//
// The page is the production build served by the real analysis API with a real
// EventHub, CallRegistry and WebSocket route. Events enter through the hub's own
// validation from a loopback test control server (tests/browser/serve_dashboard_api.py
// --control-port). Cases marked [route] put Playwright's routeWebSocket between the page
// and the real server to drop the connection, replay or withhold frames, or inject
// unreadable ones; cases marked [mock] fake one response to reach a state the server
// cannot be put into on demand. Infrastructure evidence only: no detector is involved.
// Build first: `npm run build --prefix dashboard`.
const { test, expect } = require('@playwright/test');
const fs = require('node:fs');
const path = require('node:path');
const { DIST, freePort, startApi } = require('./support/api-server.cjs');

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
const publish = (type, callId, data = {}, ts) => post('/publish', { type, call_id: callId, data, ...(ts === undefined ? {} : { ts }) });
const score = (callId, value, extra = {}, ts) => publish('score.updated', callId, {
  score: value, ema: value, state: 'monitoring', degraded: false, effective_stride_s: 2.0, quality: 'ok', direction: 'caller', ...extra,
}, ts);
const end = (callId, finalState = 'monitoring', reason = 'bye') => publish('call.ended', callId, { final_state: finalState, reason, speech_seconds: 12.5 });

async function open(page, hash = '#live') {
  const offOrigin = [];
  page.on('request', request => {
    const url = new URL(request.url());
    if (url.host !== new URL(base).host && url.protocol !== 'data:' && url.protocol !== 'blob:') offOrigin.push(request.url());
  });
  const sockets = [];
  page.on('websocket', ws => sockets.push(ws.url()));
  const errors = [];
  page.on('pageerror', error => errors.push(String(error)));
  const csp = [];
  page.on('console', message => { if (/Content Security Policy/i.test(message.text())) csp.push(message.text()); });
  await page.goto(`${base}/${hash}`);
  if (hash === '#live') await expect(page.getByTestId('connection')).toHaveText('Live');
  return { offOrigin, sockets, errors, csp };
}

const card = (page, callId) => page.locator(`[data-testid="call-card"][data-call-id="${callId}"]`);

test('LB01 FR-U1/U8: a call appears with state, duration, confidence and sparkline; same origin, no CSP errors', async ({ page }) => {
  const { offOrigin, sockets, errors, csp } = await open(page);
  await expect(page.getByRole('complementary', { name: 'Limitations' })).toContainText('not a voice biometric');
  await expect(page).toHaveTitle('AVA live calls');
  const started = Date.now() / 1000 - 65;
  await publish('call.started', 'lb01', {}, started);
  await score('lb01', 0.2);
  await score('lb01', 0.3, { ema: 0.25 });
  const c = card(page, 'lb01');
  await expect(c.getByTestId('call-state')).toHaveText('Monitoring');
  await expect(c.getByTestId('call-confidence')).toHaveText('0.25 (smoothed, uncalibrated)');
  await expect(c.getByTestId('sparkline')).toHaveAttribute('data-points', '2');
  await expect(c.getByTestId('call-flags')).toHaveText('No quality flags');
  // Started 65 s before now by the server's clock: 01:05 or a second or two later.
  await expect(c.getByTestId('call-duration')).toHaveText(/^01:0[5-9]$/);
  await expect(c.getByTestId('call-alerts')).toHaveText('0 open / 0 total');
  expect(sockets).toEqual([`${base.replace('http', 'ws')}/v1/events`]);
  expect(offOrigin).toEqual([]);
  expect(errors).toEqual([]);
  expect(csp).toEqual([]);
  await end('lb01');
});

test('LB02 FR-U1: every state is written as text; low quality, degraded and null scores are shown', async ({ page }) => {
  await open(page);
  const states = { 'lb02-m': 'Monitoring', 'lb02-s': 'Suspicious', 'lb02-a': 'Alert', 'lb02-n': 'No speech', 'lb02-u': 'Unmonitored' };
  const raw = { 'lb02-m': 'monitoring', 'lb02-s': 'suspicious', 'lb02-a': 'alert', 'lb02-n': 'no_speech', 'lb02-u': 'unmonitored' };
  for (const id of Object.keys(states)) {
    await publish('call.started', id);
    await score(id, 0.4);
  }
  for (const id of Object.keys(states)) {
    const nullScore = raw[id] === 'no_speech' || raw[id] === 'unmonitored';
    await score(id, nullScore ? null : 0.6, { state: raw[id], ema: nullScore ? null : 0.6 });
  }
  // Quality flag from a low-quality update (no score) and the degraded flag.
  await score('lb02-m', null, { ema: null, quality: 'low', degraded: true, effective_stride_s: 4.0 });
  for (const [id, text] of Object.entries(states)) await expect(card(page, id).getByTestId('call-state')).toHaveText(text);
  const m = card(page, 'lb02-m');
  await expect(m.getByTestId('call-flags')).toContainText('Low audio quality');
  await expect(m.getByTestId('call-flags')).toContainText('Degraded (reduced scoring rate)');
  await expect(m.getByTestId('sparkline')).toHaveAttribute('data-points', '3'); // nulls keep their slot
  await expect(m.getByTestId('call-confidence')).toHaveText('0.60 (smoothed, uncalibrated)'); // last valid EMA kept
  await expect(card(page, 'lb02-n').getByTestId('call-confidence')).toHaveText('0.40 (smoothed, uncalibrated)');
  for (const id of Object.keys(states)) await end(id, raw[id]);
});

test('LB03 FR-U2: a new alert sounds, shows a banner and the feed, updates the title; clearing it updates both', async ({ page }) => {
  await page.addInitScript(() => {
    window.__tones = 0;
    const Real = window.AudioContext;
    window.AudioContext = class extends Real {
      createOscillator() {
        const oscillator = super.createOscillator();
        const start = oscillator.start.bind(oscillator);
        oscillator.start = (...args) => { window.__tones += 1; return start(...args); };
        return oscillator;
      }
    };
  });
  await open(page);
  await page.getByRole('button', { name: 'Turn alert sound on' }).click();
  await expect(page.getByRole('button', { name: 'Turn alert sound off' })).toBeVisible();
  await publish('call.started', 'lb03');
  await score('lb03', 0.9, { state: 'alert', ema: 0.8 });
  await publish('alert.raised', 'lb03', { alert_id: 'lb03-a1', direction: 'caller', ema: 0.8, window: 3 });
  await expect(page.getByTestId('alert-banner')).toContainText('New alert on call lb03 (caller)');
  const item = page.locator('[data-testid="alert-item"][data-alert-id="lb03-a1"]');
  await expect(item).toContainText('Alert');
  await expect(item).toContainText('smoothed score 0.80');
  await expect(page).toHaveTitle('(1) AVA live calls');
  await expect.poll(() => page.evaluate(() => window.__tones)).toBe(1);
  await expect(card(page, 'lb03').getByTestId('call-alerts')).toHaveText('1 open / 1 total');
  // A repeated raise of the same alert is not a new alert.
  await publish('alert.raised', 'lb03', { alert_id: 'lb03-a1', direction: 'caller', ema: 0.8, window: 4 });
  await publish('alert.cleared', 'lb03', { alert_id: 'lb03-a1', direction: 'caller', ema: 0.3, window: 9 });
  await expect(item).toContainText('Cleared');
  await expect(card(page, 'lb03').getByTestId('call-alerts')).toHaveText('0 open / 1 total');
  await expect(page).toHaveTitle('AVA live calls');
  expect(await page.evaluate(() => window.__tones)).toBe(1);
  // Sound off: the next alert is shown but silent.
  await page.getByRole('button', { name: 'Turn alert sound off' }).click();
  await publish('alert.raised', 'lb03', { alert_id: 'lb03-a2', direction: 'caller', ema: 0.9, window: 12 });
  await expect(page.locator('[data-testid="alert-item"][data-alert-id="lb03-a2"]')).toContainText('Alert');
  await expect(page.getByTestId('alert-banner')).toBeVisible();
  expect(await page.evaluate(() => window.__tones)).toBe(1);
  await end('lb03', 'alert');
});

test('LB04 FR-A3/A4: a call in progress is loaded from REST with its open alert, silently; replayed frames are not counted twice [route]', async ({ page }) => {
  await publish('call.started', 'lb04');
  for (const s of [0.1, 0.7, 0.9]) await score('lb04', s);
  await publish('alert.raised', 'lb04', { alert_id: 'lb04-a1', direction: 'agent', ema: 0.85, window: 2 });
  const frames = [];
  let pageSide = null;
  await page.routeWebSocket(/\/v1\/events$/, ws => {
    pageSide = ws;
    const upstream = ws.connectToServer();
    upstream.onMessage(message => { frames.push(message); ws.send(message); });
    ws.onMessage(message => upstream.send(message));
  });
  await open(page);
  const c = card(page, 'lb04');
  await expect(c.getByTestId('call-state')).toHaveText('Alert');
  await expect(c.getByTestId('call-alerts')).toHaveText('1 open / 1 total');
  await expect(c.getByTestId('sparkline')).toHaveAttribute('data-points', '3');
  await expect(page.locator('[data-testid="alert-item"][data-alert-id="lb04-a1"]')).toContainText('(agent)');
  await expect(page.getByTestId('alert-banner')).toHaveCount(0); // not a new alert for this page
  await score('lb04', 0.95, { state: 'alert' });
  await expect(c.getByTestId('sparkline')).toHaveAttribute('data-points', '4');
  // Replay every frame the page has received, plus a frame older than the snapshot:
  // all are already applied, so nothing changes.
  const lb04Frames = frames.filter(f => String(f).includes('"lb04"'));
  expect(lb04Frames.length).toBeGreaterThan(0);
  const stale = JSON.stringify({ type: 'score.updated', call_id: 'lb04', seq: 1, ts: Date.now() / 1000,
    data: { score: 0.5, state: 'monitoring', degraded: false, effective_stride_s: 2.0 } });
  for (const frame of [...lb04Frames, stale]) pageSide.send(frame);
  await score('lb04', 0.2, { state: 'alert' }); // a fresh event after the replays, as a marker
  await expect(c.getByTestId('sparkline')).toHaveAttribute('data-points', '5');
  await expect(c.getByTestId('call-state')).toHaveText('Alert'); // the stale 'monitoring' frame was skipped
  await end('lb04', 'alert');
});

test('LB05 FR-U1/U2: an ended call moves to recently ended with its reason; its open alert is marked open at call end', async ({ page }) => {
  await open(page);
  await publish('call.started', 'lb05');
  await score('lb05', 0.9, { state: 'alert' });
  await publish('alert.raised', 'lb05', { alert_id: 'lb05-a1', direction: 'caller', ema: 0.9, window: 1 });
  await expect(page.getByTestId('active-calls').locator('[data-call-id="lb05"]')).toBeVisible();
  await end('lb05', 'alert', 'timeout');
  const ended = page.getByTestId('ended-calls').locator('[data-call-id="lb05"]');
  await expect(ended.getByTestId('call-end-reason')).toHaveText('timeout');
  await expect(ended.getByTestId('call-alerts')).toHaveText('0 open / 1 total');
  await expect(page.locator('[data-testid="alert-item"][data-alert-id="lb05-a1"]')).toContainText('Open at call end');
  await expect(page.getByTestId('active-calls').locator('[data-call-id="lb05"]')).toHaveCount(0);
  // Events after the end are ignored, as in the registry. A later event for another call
  // is the marker that the page has received (and skipped) the late one.
  await score('lb05', 0.1);
  await publish('call.started', 'lb05-marker');
  await expect(card(page, 'lb05-marker')).toBeVisible();
  await expect(ended.getByTestId('sparkline')).toHaveAttribute('data-points', '1');
  await end('lb05-marker');
});

test('LB06 FR-A4: withheld events are repaired by re-reading REST after stream.gap [route]', async ({ page }) => {
  let withhold = false;
  await page.routeWebSocket(/\/v1\/events$/, ws => {
    const upstream = ws.connectToServer();
    upstream.onMessage(message => { if (!(withhold && String(message).includes('"lb06"'))) ws.send(message); });
    ws.onMessage(message => upstream.send(message));
  });
  const reads = [];
  page.on('request', request => { if (request.url().endsWith('/v1/calls?status=all')) reads.push(request.url()); });
  await open(page);
  await publish('call.started', 'lb06');
  await score('lb06', 0.2);
  const c = card(page, 'lb06');
  await expect(c.getByTestId('sparkline')).toHaveAttribute('data-points', '1');
  withhold = true;
  await score('lb06', 0.6, { state: 'suspicious' });
  await score('lb06', 0.7, { state: 'suspicious' });
  await expect(c.getByTestId('call-state')).toHaveText('Monitoring'); // the page has not seen them
  const before = reads.length;
  await post('/loss', { missing: 2 });
  await expect.poll(() => reads.length).toBeGreaterThan(before);
  await expect(c.getByTestId('call-state')).toHaveText('Suspicious');
  await expect(c.getByTestId('sparkline')).toHaveAttribute('data-points', '3');
  withhold = false;
  await score('lb06', 0.3);
  await expect(c.getByTestId('sparkline')).toHaveAttribute('data-points', '4');
  await end('lb06');
});

test('LB07 FR-U1: a dropped connection is shown, retried, and the board catches up on reconnect [route]', async ({ page }) => {
  const connections = [];
  await page.routeWebSocket(/\/v1\/events$/, ws => {
    const upstream = ws.connectToServer();
    connections.push({ ws, upstream });
  });
  await open(page);
  await publish('call.started', 'lb07');
  await score('lb07', 0.2);
  await expect(card(page, 'lb07').getByTestId('sparkline')).toHaveAttribute('data-points', '1');
  const first = connections[0];
  await first.ws.close({ code: 4000, reason: 'test drop' });
  await first.upstream.close();
  await expect(page.getByTestId('connection')).toContainText('Live updates disconnected: test drop');
  await expect(page.getByTestId('connection')).toContainText('The board may be out of date');
  // Published while the page is disconnected: only the resync after reconnecting can show it.
  await score('lb07', 0.9, { state: 'suspicious' });
  await expect(page.getByTestId('connection')).toHaveText('Live', { timeout: 10000 });
  expect(connections.length).toBe(2);
  await expect(card(page, 'lb07').getByTestId('call-state')).toHaveText('Suspicious');
  await expect(card(page, 'lb07').getByTestId('sparkline')).toHaveAttribute('data-points', '2');
  await end('lb07');
});

test('LB08 FR-U3: the call timeline shows each scored window, marks synthetic ones and follows new scores', async ({ page }) => {
  await open(page);
  const t0 = Date.now() / 1000 - 20;
  await publish('call.started', 'lb08', {}, t0);
  await score('lb08', 0.1, {}, t0 + 4);
  await score('lb08', 0.8, { state: 'suspicious', direction: 'agent' }, t0 + 6);
  await score('lb08', null, { ema: null, quality: 'low' }, t0 + 8); // not a window
  await card(page, 'lb08').getByRole('button', { name: 'Show timeline' }).click();
  const detail = page.getByTestId('call-detail');
  const rows = detail.getByTestId('timeline-row');
  await expect(rows).toHaveCount(2);
  await expect(rows.nth(0)).toContainText('2.0–4.0');
  await expect(rows.nth(0)).not.toHaveClass(/synthetic/);
  await expect(rows.nth(1)).toContainText('4.0–6.0');
  await expect(rows.nth(1)).toHaveClass(/synthetic/);
  await expect(rows.nth(1)).toContainText('scored synthetic, agent');
  await expect(detail.getByTestId('timeline-bar')).toHaveCount(2);
  await score('lb08', 0.3, {}, t0 + 10);
  await expect(rows).toHaveCount(3);
  await card(page, 'lb08').getByRole('button', { name: 'Hide timeline' }).click();
  await expect(detail).toHaveCount(0);
  await end('lb08');
});

test('LB09 FR-U1: hostile call IDs render as text; unreadable frames are counted and ignored [route]', async ({ page }) => {
  const connections = [];
  await page.routeWebSocket(/\/v1\/events$/, ws => { connections.push(ws); ws.connectToServer(); });
  const { errors } = await open(page);
  const hostile = '<img src=x onerror="window.__pwned=1">';
  await publish('call.started', hostile);
  await score(hostile, 0.5);
  await expect(page.getByTestId('call-id').filter({ hasText: hostile })).toBeVisible();
  for (const frame of ['not json', '{"type":"score.updated"}', JSON.stringify({ type: 'score.updated', call_id: 'lb09-x', seq: 10 ** 9, ts: 1, data: { score: 7, state: 'monitoring', degraded: false } }),
    JSON.stringify({ type: 'call.party', call_id: 'lb09-y', seq: 10 ** 9, ts: 1, data: {} })]) {
    connections[0].send(frame);
  }
  await expect(page.getByTestId('invalid-events')).toHaveText('4 unreadable event(s) were ignored.');
  await expect(card(page, 'lb09-x')).toHaveCount(0); // a malformed event creates no phantom call
  await score(hostile, 0.6);
  await expect(page.locator('[data-testid="call-card"]').filter({ hasText: hostile }).getByTestId('sparkline')).toHaveAttribute('data-points', '2');
  expect(await page.evaluate(() => window.__pwned)).toBeUndefined();
  expect(errors).toEqual([]);
  await end(hostile);
});

test('LB11 FR-A3/A4: events queued while the snapshot loads, which the snapshot also holds, are applied once [route]', async ({ page }) => {
  // Review finding (16 Nov): no case covered the queue used while REST loads. The first
  // snapshot request is held before it reaches the server; events published meanwhile
  // reach the page by WebSocket (queued) AND are in the snapshot computed after them.
  // The other order (queued events newer than the snapshot) is covered by the pure
  // merge tests in live-state.spec.ts (LS05).
  await publish('call.started', 'lb11');
  await score('lb11', 0.1);
  let release;
  const held = new Promise(resolve => { release = resolve; });
  let first = true;
  await page.route('**/v1/calls?status=all', async route => {
    if (first) { first = false; await held; }
    await route.continue();
  });
  await page.goto(`${base}/#live`);
  await expect(page.getByTestId('connection')).toHaveText('Live');
  await score('lb11', 0.5, { state: 'suspicious' });
  await score('lb11', 0.6, { state: 'suspicious' });
  await page.waitForTimeout(300); // let both frames reach the page while the read is held
  release();
  const c = card(page, 'lb11');
  await expect(c.getByTestId('call-state')).toHaveText('Suspicious');
  await expect(c.getByTestId('sparkline')).toHaveAttribute('data-points', '3');
  await score('lb11', 0.7, { state: 'suspicious' }); // and later events still apply
  await expect(c.getByTestId('sparkline')).toHaveAttribute('data-points', '4');
  await end('lb11');
});

test('LB12 FR-U2: an alert cleared while disconnected is shown cleared after reconnecting; quick alerts are counted [route]', async ({ page }) => {
  const connections = [];
  await page.routeWebSocket(/\/v1\/events$/, ws => { connections.push({ ws, upstream: ws.connectToServer() }); });
  await open(page);
  await publish('call.started', 'lb12');
  await publish('alert.raised', 'lb12', { alert_id: 'lb12-a1', direction: 'caller', ema: 0.8, window: 1 });
  const item = page.locator('[data-testid="alert-item"][data-alert-id="lb12-a1"]');
  await expect(item).toContainText('Alert');
  await connections[0].ws.close({ code: 4000, reason: 'test drop' });
  await connections[0].upstream.close();
  await expect(page.getByTestId('connection')).toContainText('disconnected');
  await publish('alert.cleared', 'lb12', { alert_id: 'lb12-a1', direction: 'caller', ema: 0.2, window: 5 });
  await expect(page.getByTestId('connection')).toHaveText('Live', { timeout: 10000 });
  await expect(item).toContainText('Cleared');
  await expect(card(page, 'lb12').getByTestId('call-alerts')).toHaveText('0 open / 1 total');
  await expect(page).toHaveTitle('AVA live calls');
  await page.getByTestId('alert-banner').getByRole('button', { name: 'Dismiss' }).click();
  await publish('alert.raised', 'lb12', { alert_id: 'lb12-a2', direction: 'caller', ema: 0.8, window: 7 });
  await publish('alert.raised', 'lb12', { alert_id: 'lb12-a3', direction: 'agent', ema: 0.9, window: 7 });
  await expect(page.getByTestId('alert-banner')).toContainText('2 new alerts; newest on call lb12 (agent)');
  await expect(card(page, 'lb12').getByTestId('call-alerts')).toHaveText('2 open / 3 total');
  await expect(page).toHaveTitle('(2) AVA live calls');
  await end('lb12', 'alert');
});

test('LB10 FR-U1: with no live source the board says so and retries with growing back-off; the batch page opens no socket [mock]', async ({ page }) => {
  await page.route('**/v1/calls?status=all', route => route.fulfill({ status: 503, contentType: 'application/json', body: '{"detail":"No live event source is connected"}' }));
  // The server accepts the upgrade and then closes it (as app.py does), so the page's
  // socket opens each time: the back-off must still grow (review finding, 16 Nov).
  const times = [];
  // A close inside the handler would reach the page before `open`; closing a little
  // later lets the page see the socket open first, as with the real server.
  await page.routeWebSocket(/\/v1\/events$/, ws => {
    times.push(Date.now());
    setTimeout(() => ws.close({ code: 1011, reason: 'No live event source is connected' }), 150);
  });
  const opened = [];
  await page.exposeFunction('__wsOpened', () => opened.push(Date.now()));
  await page.addInitScript(() => {
    const Real = window.WebSocket;
    window.WebSocket = class extends Real {
      constructor(...args) { super(...args); this.addEventListener('open', () => window.__wsOpened()); }
    };
  });
  const sockets = [];
  page.on('websocket', ws => sockets.push(ws.url()));
  await page.goto(`${base}/`);
  await expect(page.getByRole('heading', { name: 'AVA batch analysis' })).toBeVisible();
  expect(sockets).toEqual([]);
  await page.getByRole('link', { name: 'Live calls' }).click();
  await expect(page.getByTestId('connection')).toContainText('No live event source is connected');
  await expect(page.getByTestId('no-active')).toBeVisible();
  // Delays: 1 s then 2 s, each with jitter in [0.75, 1.25): at most about 3.75 s in all.
  await expect.poll(() => times.length, { timeout: 8000 }).toBeGreaterThanOrEqual(3);
  expect(opened.length).toBeGreaterThanOrEqual(2); // the page did see each socket open
  // 150 ms open (the close delay above), then the delay: a reset back-off would give at
  // most about 1.4 s each time. Changing the 150 ms changes both bounds below.
  expect(times[1] - times[0]).toBeGreaterThanOrEqual(850);
  expect(times[2] - times[1]).toBeGreaterThanOrEqual(1600);
  await page.getByRole('link', { name: 'Batch analysis' }).click();
  await expect(page.getByRole('heading', { name: 'AVA batch analysis' })).toBeVisible();
  const settled = times.length;
  await page.waitForTimeout(5500); // longer than the next back-off step (4 s x 1.25)
  expect(times.length).toBe(settled); // leaving the board stops reconnecting
});
