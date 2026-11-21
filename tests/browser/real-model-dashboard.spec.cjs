// Claude-authored browser cases (14 November 2026) for backlog 2.QA: actual public-pilot
// audio through the built page, the real API and the pinned AntiDeepfake MMS-300M
// checkpoint (PRD FR-U4, FR-U3, FR-A1/FR-A2, FR-A8 readiness, FR-U8). Authored by Claude
// (Claude Code), not GPT-6 Astra.
//
// Local only: needs `.local/real-model-qa.json` (gitignored) naming the model root and
// prepared public CSALT clips (genuine pilot and synthetic benchmark); the project
// fails without it rather than reporting a vacuous pass. Clips are concatenated in
// memory and sent as uploads; the project records no traces (they would hold request
// bodies), errors name clips only by list position, and test titles carry no clip
// names. Screenshots on failure show the page only. Infrastructure evidence: the page
// shows exactly what the API computed with the real model. Verdicts are recorded as
// observations, never asserted as correct; this is not an accuracy result.
const { test, expect } = require('@playwright/test');
const fs = require('node:fs');
const path = require('node:path');
const { DIST, ROOT, readPcm16Mono, startApi, wavFromPcm } = require('./support/api-server.cjs');

const CONFIG = path.join(ROOT, '.local', 'real-model-qa.json');
const config = fs.existsSync(CONFIG) ? JSON.parse(fs.readFileSync(CONFIG, 'utf8')) : null;
let server;
let base;
let clips;
let readyModelId;

test.describe.configure({ mode: 'serial' });

function load(kind, paths) {
  if (!Array.isArray(paths) || paths.length === 0) throw new Error(`config: ${kind} needs at least one clip`);
  return paths.map((file, i) => {
    try {
      return readPcm16Mono(fs.readFileSync(file));
    } catch (error) {
      throw new Error(`${kind}[${i}] unreadable: ${error.code || error.message}`); // no path: clip names identify speakers
    }
  });
}

/** Concatenates clips (one rate) until at least `seconds`, cycling if needed. */
function concat(list, seconds) {
  const rate = list[0].rate;
  if (list.some(c => c.rate !== rate)) throw new Error('clips must share one sample rate');
  const chunks = [];
  let total = 0;
  for (let i = 0; total < seconds * rate; i++) {
    const { samples } = list[i % list.length];
    chunks.push(samples);
    total += samples.length;
  }
  return { buffer: wavFromPcm(chunks, rate), seconds: total / rate };
}

test.beforeAll(async () => {
  test.setTimeout(600000); // uv resolving torch, server start (<= 240 s) and warm-up (<= 240 s)
  if (!config) throw new Error('missing .local/real-model-qa.json: the real-model project needs the local checkpoint and clips');
  if (!fs.existsSync(path.join(DIST, 'index.html'))) throw new Error('dashboard/dist missing: run `npm run build --prefix dashboard`');
  clips = { genuine: load('genuine', config.genuine), synthetic: load('synthetic', config.synthetic) };
  server = await startApi({
    uvArgs: ['--extra-index-url', 'https://download.pytorch.org/whl/cpu', '--with-requirements', 'requirements-model.txt'],
    serverArgs: ['--model-root', config.model_root, '--threads', String(config.threads || 4)],
    timeoutMs: 240000,
  });
  base = server.base;
  // FR-A8: readiness runs one validated warm-up inference with the real checkpoint.
  const ready = await (await fetch(`${base}/readyz`, { signal: AbortSignal.timeout(240000) })).json();
  if (ready.status !== 'ready') throw new Error(`model not ready: ${JSON.stringify(ready)}`);
  if (!/^nii-yamagishilab\/mms-300m-anti-deepfake@[0-9a-f]{40}$/.test(ready.model_id)) throw new Error(`unexpected model ${ready.model_id}`);
  readyModelId = ready.model_id;
  console.log(`[2.QA] warm-up ${ready.warmup_seconds.toFixed(1)} s, model ${ready.model_id}`);
});

test.afterAll(async () => { if (server) await server.stop(); });

async function open(page) {
  const offOrigin = [], errors = [], csp = [];
  page.on('request', r => { const u = new URL(r.url()); if (u.origin !== base && !['data:', 'blob:'].includes(u.protocol)) offOrigin.push(r.url()); });
  page.on('pageerror', e => errors.push(String(e)));
  page.on('console', m => { if (/Content Security Policy/i.test(m.text())) csp.push(m.text()); });
  await page.goto(`${base}/`);
  return { offOrigin, errors, csp };
}

/** Asserts the page shows exactly the API's result: verdict, score, every timeline row. */
async function expectPageMatches(page, result, label) {
  const verdictText = { synthetic: 'Likely synthetic speech', genuine: 'No synthetic speech indicated', undetermined: 'Undetermined' };
  await expect(page.getByTestId('verdict')).toHaveText(verdictText[result.verdict]);
  await expect(page.getByTestId('calibration')).toContainText('Uncalibrated baseline');
  expect(result.calibration).toBe('uncalibrated_baseline');
  await expect(page.getByTestId('model')).toHaveText(result.model_id);
  expect(result.model_id).toBe(readyModelId); // the pinned checkpoint, not the fixture detector
  expect(result.model_id).not.toBe('browser-fixture-fake-detector');
  await expect(page.getByTestId('score')).toHaveText(result.p_synthetic === null ? '—' : result.p_synthetic.toFixed(3));
  await expect(page.getByTestId('flags')).toHaveText(result.flags.length ? result.flags.join(', ') : 'none');
  await expect(page.getByTestId('result')).toContainText(`${result.duration_seconds.toFixed(1)} s`);
  if (result.reason) await expect(page.getByTestId('reason')).toHaveText(result.reason);
  const rows = page.getByTestId('timeline-row');
  await expect(rows).toHaveCount(result.timeline.length);
  await expect(page.getByTestId('timeline-bar')).toHaveCount(result.timeline.length);
  const cells = await rows.evaluateAll(trs => trs.map(tr => [...tr.cells].map(td => td.textContent)));
  result.timeline.forEach((w, i) => {
    expect(w.p_synthetic).toBeGreaterThanOrEqual(0);
    expect(w.p_synthetic).toBeLessThanOrEqual(1);
    expect(cells[i][0]).toBe(String(w.index));
    expect(cells[i][1]).toBe(`${w.t_start.toFixed(1)}–${w.t_end.toFixed(1)}`);
    expect(cells[i][2]).toBe(w.p_synthetic.toFixed(3));
    expect(cells[i][3].includes('scored synthetic')).toBe(w.p_synthetic >= 0.5);
  });
  const high = result.timeline.filter(w => w.p_synthetic >= 0.5).length;
  console.log(`[2.QA] ${label}: ${result.duration_seconds.toFixed(1)} s, ${result.timeline.length} windows, `
    + `verdict ${result.verdict}, mean ${result.p_synthetic === null ? 'n/a' : result.p_synthetic.toFixed(3)}, `
    + `${high} windows >= 0.5, processing ${result.processing_seconds.toFixed(1)} s (observation, not accuracy)`);
}

async function uploadSync(page, name, buffer) {
  const response = page.waitForResponse(r => r.url().endsWith('/v1/analyze') && r.request().method() === 'POST', { timeout: 240000 });
  await page.getByLabel('Choose a file').setInputFiles({ name, mimeType: 'audio/wav', buffer });
  const answer = await response;
  expect(answer.status()).toBe(200);
  return answer.json();
}

test('RM01 genuine public pilot audio (about 20 s) through page, API and real model', async ({ page }) => {
  const { offOrigin, errors, csp } = await open(page);
  const file = concat(clips.genuine, 20);
  const result = await uploadSync(page, 'genuine-pilot.wav', file.buffer);
  expect(result.timeline.filter(w => !w.partial).length).toBeGreaterThanOrEqual(2);
  await expectPageMatches(page, result, 'genuine concatenation');
  expect(offOrigin).toEqual([]);
  expect(errors).toEqual([]);
  expect(csp).toEqual([]);
});

test('RM02 synthetic public benchmark audio (about 20 s) through page, API and real model', async ({ page }) => {
  const { offOrigin, errors } = await open(page);
  const file = concat(clips.synthetic, 20);
  const result = await uploadSync(page, 'synthetic-benchmark.wav', file.buffer);
  await expectPageMatches(page, result, 'synthetic concatenation');
  expect(offOrigin).toEqual([]);
  expect(errors).toEqual([]);
});

test('RM03 a single short public clip is shown as undetermined with its reason', async ({ page }) => {
  await open(page);
  const short = clips.genuine.find(c => c.samples.length / c.rate < 6);
  expect(short, 'config must include a genuine clip under 6 s').toBeTruthy();
  const result = await uploadSync(page, 'short-pilot.wav', wavFromPcm([short.samples], short.rate));
  expect(result.verdict).toBe('undetermined');
  await expectPageMatches(page, result, 'single short clip');
  await expect(page.getByTestId('reason')).toHaveText(result.reason);
});

test('RM04 FR-A1/A2: public pilot audio over 60 s is queued, polled and shown', async ({ page }) => {
  await open(page);
  const file = concat(clips.genuine, 62);
  expect(file.seconds).toBeGreaterThan(60);
  const posted = page.waitForResponse(r => r.url().endsWith('/v1/analyze'), { timeout: 60000 });
  const done = page.waitForResponse(async r => {
    if (!r.url().includes('/v1/jobs/')) return false;
    try { return (await r.json()).status === 'done'; } catch { return false; }
  }, { timeout: 280000 });
  await page.getByLabel('Choose a file').setInputFiles({ name: 'long-pilot.wav', mimeType: 'audio/wav', buffer: file.buffer });
  expect((await posted).status()).toBe(202);
  const job = await (await done).json();
  await expectPageMatches(page, job.result, 'genuine concatenation over 60 s (job)');
});

test('RM05 damaged public audio is refused (422) and no verdict is shown', async ({ page }) => {
  await open(page);
  const good = concat(clips.genuine, 8).buffer;
  const truncated = good.subarray(0, 30); // header cut inside the fmt chunk
  const wrongMagic = Buffer.from(good); wrongMagic.write('RIFX', 0);
  for (const [name, buffer] of [['truncated.wav', truncated], ['wrong-magic.wav', wrongMagic]]) {
    const response = page.waitForResponse(r => r.url().endsWith('/v1/analyze'));
    await page.getByLabel('Choose a file').setInputFiles({ name, mimeType: 'audio/wav', buffer });
    expect((await response).status()).toBe(422);
    await expect(page.getByTestId('error')).toContainText('could not be decoded');
    await expect(page.getByTestId('result')).toHaveCount(0);
  }
});
