// Claude-authored browser cases (14 November 2026) for backlog 2.3 / PRD FR-U4, FR-U3,
// FR-U8, FR-U1 limitation text and FR-A1/FR-A2 error handling in the page. Authored by
// Claude (Claude Code), not GPT-6 Astra.
//
// The page is the production build (`dashboard/dist`) served by the real analysis API
// (tests/browser/serve_dashboard_api.py) with a deterministic fake detector: 300 Hz
// tones score genuine, 3000 Hz synthetic, 1000 Hz makes the detector raise. Cases
// marked [mock] replace one HTTP response with page.route to reach transport states the
// real server cannot be put into on demand (busy, network loss, malformed body, a job
// vanishing); every other case goes through the real server. Infrastructure evidence
// only: no claim about detection accuracy. Build first: `npm run build --prefix dashboard`.
const { test, expect } = require('@playwright/test');
const fs = require('node:fs');
const path = require('node:path');
const { DIST, startApi, tones } = require('./support/api-server.cjs');

const wav = segments => tones(segments, 8000); // [{ seconds, hz }], hz 0 = silence
let server;
let base;

test.describe.configure({ mode: 'serial' });

test.beforeAll(async () => {
  test.setTimeout(180000);
  if (!fs.existsSync(path.join(DIST, 'index.html'))) throw new Error('dashboard/dist missing: run `npm run build --prefix dashboard`');
  server = await startApi();
  base = server.base;
});

test.afterAll(async () => { if (server) await server.stop(); });

async function open(page, query = '') {
  const offOrigin = [];
  page.on('request', request => {
    const url = new URL(request.url());
    if (url.origin !== base && url.protocol !== 'data:' && url.protocol !== 'blob:') offOrigin.push(request.url());
  });
  const errors = [];
  page.on('pageerror', error => errors.push(String(error)));
  // CSP violations are console errors, not page errors: collect them separately.
  const csp = [];
  page.on('console', message => { if (/Content Security Policy/i.test(message.text())) csp.push(message.text()); });
  await page.goto(`${base}/${query}`);
  return { offOrigin, errors, csp };
}

async function choose(page, name, buffer, mimeType = 'audio/wav') {
  await page.getByLabel('Choose a file').setInputFiles({ name, mimeType, buffer });
}

test('DU01 FR-U4/U1/U8: synchronous upload shows verdict, uncalibrated label, limitations; same-origin only', async ({ page }) => {
  const { offOrigin, errors, csp } = await open(page);
  const limits = page.getByRole('complementary', { name: 'Limitations' });
  await expect(limits).toContainText('not a voice biometric');
  // The bundled stylesheet loaded under the CSP (default <aside> has no border).
  expect(await limits.evaluate(el => getComputedStyle(el).borderTopStyle)).toBe('solid');
  await choose(page, 'tone-3000.wav', wav([{ seconds: 9.6, hz: 3000 }]));
  await expect(page.getByTestId('verdict')).toHaveText('Likely synthetic speech');
  await expect(page.getByTestId('calibration')).toContainText('Uncalibrated baseline');
  await expect(page.getByTestId('model')).toHaveText('browser-fixture-fake-detector');
  await expect(page.getByTestId('scope')).toContainText('Advisory research output only');
  await expect(page.getByTestId('flags')).toHaveText('none');
  // 9.6 s -> full windows at 0, 2, 4 s and a partial 6-9.6 s (1.6 s uncovered >= 1.5 s).
  await expect(page.getByTestId('timeline-bar')).toHaveCount(4);
  await expect(page.getByTestId('timeline-row')).toHaveCount(4);
  await expect(page.getByTestId('timeline-row').last()).toContainText('partial');
  expect(Number(await page.getByTestId('score').textContent())).toBeGreaterThan(0.99);
  expect(offOrigin).toEqual([]);
  expect(errors).toEqual([]);
  expect(csp).toEqual([]);
});

test('DU02 FR-U3: mixed file marks only the synthetic seconds in the timeline', async ({ page }) => {
  await open(page);
  await choose(page, 'mixed.wav', wav([{ seconds: 8, hz: 300 }, { seconds: 8, hz: 3000 }]));
  await expect(page.getByTestId('result')).toBeVisible();
  const rows = page.getByTestId('timeline-row');
  await expect(rows).toHaveCount(7);
  await expect(rows.first()).not.toHaveClass(/synthetic/);
  await expect(rows.first()).toContainText('0.0–4.0');
  await expect(rows.last()).toHaveClass(/synthetic/);
  await expect(rows.last()).toContainText('12.0–16.0');
  await expect(page.locator('[data-testid="timeline-bar"].synthetic')).not.toHaveCount(0);
  await expect(page.locator('[data-testid="timeline-bar"]:not(.synthetic)')).not.toHaveCount(0);
});

test('DU03 FR-A1/A2: a file over 60 s is queued as a job, polled and shown', async ({ page }) => {
  await open(page);
  const polls = [];
  page.on('request', request => { if (request.url().includes('/v1/jobs/')) polls.push(request.url()); });
  await choose(page, 'long.wav', wav([{ seconds: 70, hz: 300 }]));
  await expect(page.getByTestId('verdict')).toHaveText('No synthetic speech indicated', { timeout: 30000 });
  expect(polls.length).toBeGreaterThan(0);
  expect(new Set(polls.map(u => new URL(u).pathname)).size).toBe(1);
  await expect(page.getByTestId('timeline-row')).toHaveCount(34);
});

test('DU04 undetermined: too little evidence shows the reason, no score', async ({ page }) => {
  await open(page);
  await choose(page, 'short.wav', wav([{ seconds: 3, hz: 3000 }]));
  await expect(page.getByTestId('verdict')).toHaveText('Undetermined');
  await expect(page.getByTestId('score')).toHaveText('—');
  await expect(page.getByTestId('reason')).toContainText('partial window');
});

test('DU05 FR-A1 422: a non-WAV file shows the decode refusal and the page recovers', async ({ page }) => {
  const { errors } = await open(page);
  await choose(page, 'notes.txt', Buffer.from('not audio at all'), 'text/plain');
  await expect(page.getByTestId('error')).toContainText('could not be decoded');
  await choose(page, 'tone.wav', wav([{ seconds: 6, hz: 300 }]));
  await expect(page.getByTestId('verdict')).toHaveText('No synthetic speech indicated');
  await expect(page.getByTestId('error')).toHaveCount(0);
  expect(errors).toEqual([]);
});

test('DU06 detector failure (500) is reported, not shown as a verdict', async ({ page }) => {
  await open(page);
  await choose(page, 'fault.wav', wav([{ seconds: 6, hz: 1000 }]));
  await expect(page.getByTestId('error')).toContainText('detector failed');
  await expect(page.getByTestId('result')).toHaveCount(0);
});

test('DU07 client-side refusals: empty file and more than 100 MB are never sent', async ({ page }) => {
  await open(page);
  const posts = [];
  page.on('request', request => { if (request.method() === 'POST') posts.push(request.url()); });
  await choose(page, 'empty.wav', Buffer.alloc(0));
  await expect(page.getByTestId('error')).toContainText('empty');
  await page.getByTestId('dropzone').evaluate(zone => {
    const transfer = new DataTransfer();
    transfer.items.add(new File([new Uint8Array(100 * 1024 * 1024 + 1)], 'huge.wav', { type: 'audio/wav' }));
    zone.dispatchEvent(new DragEvent('drop', { dataTransfer: transfer, bubbles: true, cancelable: true }));
  });
  await expect(page.getByTestId('error')).toContainText('too large');
  await expect(page.getByTestId('error')).toContainText('huge.wav');
  expect(posts).toEqual([]);
});

test('DU08 drag and drop: one file is analysed; two files are refused', async ({ page }) => {
  await open(page);
  const bytes = [...wav([{ seconds: 6, hz: 3000 }])];
  await page.getByTestId('dropzone').evaluate((zone, data) => {
    const transfer = new DataTransfer();
    transfer.items.add(new File([new Uint8Array(data)], 'a.wav', { type: 'audio/wav' }));
    transfer.items.add(new File([new Uint8Array(data)], 'b.wav', { type: 'audio/wav' }));
    zone.dispatchEvent(new DragEvent('drop', { dataTransfer: transfer, bubbles: true, cancelable: true }));
  }, bytes);
  await expect(page.getByTestId('error')).toHaveText('Drop exactly one file.');
  await page.getByTestId('dropzone').evaluate((zone, data) => {
    const transfer = new DataTransfer();
    transfer.items.add(new File([new Uint8Array(data)], 'dropped.wav', { type: 'audio/wav' }));
    zone.dispatchEvent(new DragEvent('drop', { dataTransfer: transfer, bubbles: true, cancelable: true }));
  }, bytes);
  await expect(page.getByTestId('verdict')).toHaveText('Likely synthetic speech');
  await expect(page.getByRole('heading', { name: /dropped\.wav/ })).toBeVisible();
});

test('DU09 hostile file name and server detail render as inert text', async ({ page }) => {
  const { errors } = await open(page);
  const hostile = '<img src=x onerror="window.injected=1">.wav';
  await page.route('**/v1/analyze', route => route.fulfill({ status: 422, contentType: 'application/json',
    body: JSON.stringify({ detail: '<script>window.injected=2</script>' }) })); // [mock] detail text
  await choose(page, hostile, wav([{ seconds: 6, hz: 300 }]));
  await expect(page.getByTestId('error')).toContainText(hostile);
  await expect(page.getByTestId('error')).toContainText('<script>window.injected=2</script>');
  expect(await page.evaluate(() => window.injected)).toBeUndefined();
  await expect(page.locator('[data-testid="error"] img, [data-testid="error"] script')).toHaveCount(0);
  expect(errors).toEqual([]);
});

test('DU10 [mock] busy 503, network loss and a malformed 200 body show clear errors', async ({ page }) => {
  const { errors } = await open(page);
  const tone = wav([{ seconds: 6, hz: 300 }]);
  await page.route('**/v1/analyze', route => route.fulfill({ status: 503, contentType: 'application/json',
    body: JSON.stringify({ detail: 'Another analysis is running; retry later' }) }));
  await choose(page, 'a.wav', tone);
  await expect(page.getByTestId('error')).toContainText('busy');
  await page.unroute('**/v1/analyze');
  await page.route('**/v1/analyze', route => route.abort('connectionrefused'));
  await choose(page, 'b.wav', tone);
  await expect(page.getByTestId('error')).toContainText('could not be reached');
  await page.unroute('**/v1/analyze');
  await page.route('**/v1/analyze', route => route.fulfill({ status: 200, contentType: 'application/json',
    body: JSON.stringify({ verdict: 'synthetic', timeline: 'nope' }) }));
  await choose(page, 'c.wav', tone);
  await expect(page.getByTestId('error')).toContainText('cannot read');
  await expect(page.getByTestId('result')).toHaveCount(0);
  // Every rendered field is validated: a body valid except for one field is refused too.
  const good = { verdict: 'genuine', p_synthetic: 0.1, margin: 0.8, evidence_windows: 2, reason: null,
    calibration: 'uncalibrated_baseline', model_id: 'm', scope: 's', duration_seconds: 6,
    input: { sample_rate: 8000, channels: 1 }, flags: [], aggregation: 'x', processing_seconds: 0.1,
    timeline: [{ index: 0, t_start: 0, t_end: 4, partial: false, p_synthetic: 0.1 }] };
  const broken = [{ duration_seconds: undefined }, { reason: { nested: true } }, { input: null },
    { processing_seconds: 'fast' }, { flags: [{}] }, { evidence_windows: null }];
  for (const [i, change] of broken.entries()) {
    await page.unroute('**/v1/analyze');
    await page.route('**/v1/analyze', route => route.fulfill({ status: 200, contentType: 'application/json',
      body: JSON.stringify({ ...good, ...change }) }));
    await choose(page, `bad-${i}.wav`, tone);
    await expect(page.getByTestId('error')).toContainText(`bad-${i}.wav`);
    await expect(page.getByTestId('error')).toContainText('cannot read');
    await expect(page.getByRole('complementary', { name: 'Limitations' })).toBeVisible();
  }
  await page.unroute('**/v1/analyze');
  await page.route('**/v1/analyze', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(good) }));
  await choose(page, 'good.wav', tone);
  await expect(page.getByTestId('verdict')).toHaveText('No synthetic speech indicated');
  expect(errors).toEqual([]);
});

test('DU11 [mock] job states: running then failed shows the job error; a vanished job (404) stops polling', async ({ page }) => {
  await open(page);
  const tone = wav([{ seconds: 6, hz: 300 }]);
  await page.route('**/v1/analyze', route => route.fulfill({ status: 202, contentType: 'application/json',
    body: JSON.stringify({ job_id: 'job-a', status: 'queued', poll: '/v1/jobs/job-a' }) }));
  let calls = 0;
  await page.route('**/v1/jobs/job-a', route => {
    calls += 1;
    const body = calls < 2 ? { job_id: 'job-a', status: 'running' }
      : { job_id: 'job-a', status: 'failed', error: { status: 422, detail: 'Audio could not be conditioned' } };
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
  });
  await choose(page, 'job.wav', tone);
  await expect(page.getByTestId('status')).toContainText('Job running');
  await expect(page.getByTestId('error')).toContainText('Audio could not be conditioned');
  const failedAfter = calls;
  await page.unroute('**/v1/analyze');
  await page.route('**/v1/analyze', route => route.fulfill({ status: 202, contentType: 'application/json',
    body: JSON.stringify({ job_id: 'job-b', status: 'queued', poll: '/v1/jobs/job-b' }) }));
  let goneCalls = 0;
  await page.route('**/v1/jobs/job-b', route => { goneCalls += 1; return route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"Unknown job"}' }); });
  await choose(page, 'gone.wav', tone);
  await expect(page.getByTestId('error')).toContainText('no longer known');
  await page.waitForTimeout(2500);
  expect(goneCalls).toBe(1);
  expect(calls).toBe(failedAfter);
});

test('DU12 [mock] stop waiting ends polling and returns to idle', async ({ page }) => {
  await open(page);
  await page.route('**/v1/analyze', route => route.fulfill({ status: 202, contentType: 'application/json',
    body: JSON.stringify({ job_id: 'job-c', status: 'queued', poll: '/v1/jobs/job-c' }) }));
  let calls = 0;
  await page.route('**/v1/jobs/job-c', route => { calls += 1; return route.fulfill({ status: 200, contentType: 'application/json', body: '{"job_id":"job-c","status":"queued"}' }); });
  await choose(page, 'wait.wav', wav([{ seconds: 6, hz: 300 }]));
  await expect(page.getByTestId('status')).toContainText('Job queued');
  await expect(page.getByLabel('Choose a file')).toBeDisabled();
  await expect(page.getByRole('button', { name: 'Stop waiting' })).toBeVisible();
  await page.getByRole('button', { name: 'Stop waiting' }).click();
  await expect(page.getByTestId('status')).toBeEmpty();
  await expect(page.getByLabel('Choose a file')).toBeEnabled();
  const stoppedAt = calls;
  await page.waitForTimeout(2500);
  expect(calls).toBe(stoppedAt);
});

test('DU13 [mock] a status read that never answers is bounded; a shutdown failure shows its own reason', async ({ page }) => {
  test.setTimeout(60000);
  await open(page, '?requestTimeoutMs=1000');
  await page.route('**/v1/analyze', route => route.fulfill({ status: 202, contentType: 'application/json',
    body: JSON.stringify({ job_id: 'job-d', status: 'queued', poll: '/v1/jobs/job-d' }) }));
  let hung = 0;
  await page.route('**/v1/jobs/job-d', () => { hung += 1; }); // never fulfilled
  await choose(page, 'stall.wav', wav([{ seconds: 6, hz: 300 }]));
  // 1 + 5 tolerated consecutive time-outs of 1 s each, 1 s apart: about 11 s.
  await expect(page.getByTestId('error')).toContainText('did not answer in time', { timeout: 30000 });
  expect(hung).toBe(6);
  await page.unroute('**/v1/analyze');
  await page.route('**/v1/analyze', route => route.fulfill({ status: 202, contentType: 'application/json',
    body: JSON.stringify({ job_id: 'job-e', status: 'queued', poll: '/v1/jobs/job-e' }) }));
  await page.route('**/v1/jobs/job-e', route => route.fulfill({ status: 200, contentType: 'application/json',
    body: JSON.stringify({ job_id: 'job-e', status: 'failed', error: { status: 503, detail: 'Service stopped' } }) }));
  await choose(page, 'stopped.wav', wav([{ seconds: 6, hz: 300 }]));
  await expect(page.getByTestId('error')).toContainText('The job failed (HTTP 503): Service stopped');
  await expect(page.getByTestId('error')).not.toContainText('busy');
});
