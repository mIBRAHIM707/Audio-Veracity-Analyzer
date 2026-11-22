// Claude-authored browser cases (22 November 2026; not GPT-6 Astra) for backlog 11.2:
// the demo rehearsal of PRD roadmap 5.9 ("live tapped call, batch upload, timeline,
// egress proof") and BO-4/BO-8 on the REAL Compose stack. The browser opens the
// dashboard through the `demo` profile's loopback forwarder (services/edge); the call is
// a real SIP call into the lab Asterisk, tapped over ARI and scored by the live service;
// events reach the page through Redis, the API relay and the WebSocket.
//
// Not run on its own: tests/lab/test_compose_demo_lab.py brings the stack up, sets the
// variables below and runs this project (`compose-demo`), then proves egress and takes
// the stack down. Without AVA_DEMO_URL the project fails rather than passing vacuously.
//
// The live detector is the 14 Nov static-INT8 export with an UNTRAINED head and the
// file analysis is the uncalibrated AntiDeepfake baseline: scores carry no meaning and
// are never asserted. No traces or videos (they would hold public audio); clips are
// named only by list position.
const { test, expect } = require('@playwright/test');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const { ROOT, readPcm16Mono, wavFromPcm } = require('./support/api-server.cjs');

const env = process.env;
const base = env.AVA_DEMO_URL;
let callWavs;
let uploadClips;
let callId;

test.describe.configure({ mode: 'serial' });

test.beforeAll(() => {
  if (!base || !/^http:\/\/127\.0\.0\.1:\d+$/.test(base)) {
    throw new Error('AVA_DEMO_URL (http://127.0.0.1:PORT) not set: run tests/lab/test_compose_demo_lab.py');
  }
  for (const name of ['AVA_DEMO_ENV_FILE', 'AVA_DEMO_PROJECT', 'AVA_DEMO_PYTHON', 'AVA_DEMO_CALL_WAVS']) {
    if (!env[name]) throw new Error(`${name} not set: run tests/lab/test_compose_demo_lab.py`);
  }
  callWavs = JSON.parse(env.AVA_DEMO_CALL_WAVS);
  uploadClips = callWavs.map((file, i) => {
    try { return readPcm16Mono(fs.readFileSync(file)); } catch (e) { throw new Error(`clip ${i} unreadable: ${e.code || e.message}`); }
  });
});

function watch(page) {
  const offOrigin = [], errors = [], csp = [];
  page.on('request', r => {
    const u = new URL(r.url());
    const origin = u.protocol === 'ws:' ? `http://${u.host}` : u.origin;
    if (origin !== base && !['data:', 'blob:'].includes(u.protocol)) offOrigin.push(r.url());
  });
  page.on('pageerror', e => errors.push(String(e)));
  page.on('console', m => { if (/Content Security Policy/i.test(m.text())) csp.push(m.text()); });
  return { offOrigin, errors, csp };
}

/** `python -m lab.demo call ...` (the documented demo step); resolves with its records. */
function placeCall() {
  const args = ['-m', 'lab.demo', 'call', '--env-file', env.AVA_DEMO_ENV_FILE, '-p', env.AVA_DEMO_PROJECT];
  for (const wav of callWavs) args.push('--wav', wav);
  return new Promise((resolve, reject) => {
    const child = spawn(env.AVA_DEMO_PYTHON, args, { cwd: ROOT, stdio: ['ignore', 'pipe', 'pipe'], windowsHide: true });
    let out = '', err = '';
    child.stdout.on('data', d => { out += d; });
    child.stderr.on('data', d => { err += d; });
    child.on('error', reject);
    child.on('close', code => {
      const records = out.split('\n').filter(Boolean).map(line => JSON.parse(line));
      if (code !== 0) reject(new Error(`lab.demo call exit ${code}: ${JSON.stringify(records)} ${err.slice(-800)}`));
      else resolve(records);
    });
  });
}

test('DR01 a live tapped lab call appears on the board, shows its timeline and ends', async ({ page }) => {
  test.setTimeout(240000);
  const seen = watch(page);
  const socket = page.waitForEvent('websocket', ws => ws.url() === `${base.replace('http:', 'ws:')}/v1/events`);
  await page.goto(`${base}/#live`);
  await socket; // the WebSocket opened through the forwarder
  await expect(page.getByTestId('connection')).toHaveText('Live', { timeout: 30000 });
  const before = await page.getByTestId('call-card').count();

  const call = placeCall();
  // BO-4: the tapped call shows as active while it runs.
  const card = page.getByTestId('active-calls').getByTestId('call-card').first();
  await expect(card).toBeVisible({ timeout: 45000 });
  callId = await card.getAttribute('data-call-id');
  expect(callId).toBeTruthy();
  await expect(card.getByTestId('call-state')).toHaveText(/Monitoring|Suspicious|Alert|No speech/);
  await card.getByRole('button', { name: 'Show timeline' }).click();
  const detail = page.getByTestId('call-detail');
  await expect(detail).toContainText(callId);
  // FR-U3: scored windows reach the timeline while the call is still active.
  await expect(detail.getByTestId('timeline-row').first()).toBeVisible({ timeout: 60000 });
  const activeRows = await detail.getByTestId('timeline-row').count();
  await expect(page.getByTestId('active-calls').locator(`[data-call-id="${callId}"]`)).toHaveCount(1);
  await expect(card.getByTestId('call-confidence')).toContainText('(smoothed, uncalibrated)');

  const records = await call;
  expect(records.map(r => r.event)).toEqual(['calling', 'call_completed']);
  const ended = page.getByTestId('ended-calls').locator(`[data-call-id="${callId}"]`);
  await expect(ended.getByTestId('call-end-reason')).toHaveText('bye', { timeout: 60000 });
  await expect.poll(() => detail.getByTestId('timeline-row').count(), { timeout: 30000 })
    .toBeGreaterThanOrEqual(Math.max(activeRows, 3));
  const rows = await detail.getByTestId('timeline-row').count();
  expect(await page.getByTestId('call-card').count()).toBe(before + 1);
  expect(seen.offOrigin).toEqual([]);
  expect(seen.errors).toEqual([]);
  expect(seen.csp).toEqual([]);
  console.log(`[11.2 demo] call ${callId}: ${activeRows} timeline rows while active, ${rows} after the end `
    + `(untrained live head: scores carry no meaning)`);
});

test('DR02 batch upload of public audio through the forwarder shows the verdict and timeline', async ({ page }) => {
  test.setTimeout(300000);
  const seen = watch(page);
  await page.goto(`${base}/`);
  const rate = uploadClips[0].rate;
  const same = uploadClips.filter(c => c.rate === rate).map(c => c.samples);
  const buffer = wavFromPcm(same, rate);
  const response = page.waitForResponse(r => r.url() === `${base}/v1/analyze` && r.request().method() === 'POST',
    { timeout: 240000 });
  await page.getByLabel('Choose a file').setInputFiles({ name: 'demo-upload.wav', mimeType: 'audio/wav', buffer });
  const answer = await response;
  expect(answer.status()).toBe(200);
  const result = await answer.json();
  const verdictText = { synthetic: 'Likely synthetic speech', genuine: 'No synthetic speech indicated', undetermined: 'Undetermined' };
  await expect(page.getByTestId('verdict')).toHaveText(verdictText[result.verdict]);
  await expect(page.getByTestId('calibration')).toContainText('Uncalibrated baseline');
  await expect(page.getByTestId('model')).toHaveText(result.model_id);
  await expect(page.getByTestId('timeline-row')).toHaveCount(result.timeline.length);
  expect(result.timeline.length).toBeGreaterThanOrEqual(1);
  expect(seen.offOrigin).toEqual([]);
  expect(seen.errors).toEqual([]);
  console.log(`[11.2 demo] upload: ${result.duration_seconds.toFixed(1)} s, ${result.timeline.length} windows, `
    + `verdict ${result.verdict} (uncalibrated baseline, observation only)`);
});

test('DR03 the same call is in the stored history read from PostgreSQL', async ({ page }) => {
  expect(callId, 'DR01 must have run').toBeTruthy();
  const seen = watch(page);
  await page.goto(`${base}/#history`);
  const row = page.getByTestId('call-row').filter({ has: page.getByRole('button', { name: callId }) });
  await expect(row).toHaveCount(1, { timeout: 30000 });
  await expect(row.getByTestId('call-end')).toHaveText('bye');
  await row.getByRole('button', { name: callId }).click();
  const detail = page.getByTestId('history-detail');
  await expect(detail.getByTestId('session')).toHaveCount(1);
  await expect(detail.getByTestId('session-end')).toHaveText('bye');
  await expect(detail.getByTestId('session-codec')).toHaveText('ulaw');
  expect(seen.offOrigin).toEqual([]);
  expect(seen.errors).toEqual([]);
});
