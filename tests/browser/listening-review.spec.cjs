const { test, expect } = require('@playwright/test');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

const malicious = '<img src=x onerror="window.injected=true">';
let folder;
let url;
const clips = [
  { path: 'plain.wav', sha256: 'a'.repeat(64), speaker_id: 'fictional-speaker-1', duration_seconds: 1, review_flags: [], source_path: malicious, start_sample: 0, end_sample: 16000 },
  { path: 'flagged #1.wav', sha256: 'b'.repeat(64), speaker_id: 'fictional-speaker-2', duration_seconds: 1, review_flags: ['fixture flag'], source_path: 'fixture.wav', start_sample: 0, end_sample: 16000 },
];
const data = { report_sha256: 'c'.repeat(64), clips };
const valid = () => ({ schema_version: 1, report_sha256: data.report_sha256, reviewer: 'Fictional reviewer', decisions: clips.map(clip => ({ path: clip.path, sha256: clip.sha256, decision: 'unreviewed', notes: '' })) });
async function upload(page, value) {
  await page.getByLabel('Resume exported decisions').setInputFiles({ name: 'review.json', mimeType: 'application/json', buffer: Buffer.from(typeof value === 'string' ? value : JSON.stringify(value)) });
}
test.beforeAll(async () => {
  folder = await fs.mkdtemp(path.join(os.tmpdir(), 'ava-review-fixture-'));
  const template = await fs.readFile('data/review/review_template.html', 'utf8');
  await fs.writeFile(path.join(folder, 'index.html'), template.replace('__AVA_REVIEW_DATA__', JSON.stringify(data).replaceAll('<', '\\u003c')));
  const wav = Buffer.alloc(44 + 32000); wav.write('RIFF'); wav.writeUInt32LE(wav.length - 8, 4); wav.write('WAVEfmt ', 8);
  wav.writeUInt32LE(16, 16); wav.writeUInt16LE(1, 20); wav.writeUInt16LE(1, 22); wav.writeUInt32LE(16000, 24); wav.writeUInt32LE(32000, 28); wav.writeUInt16LE(2, 32); wav.writeUInt16LE(16, 34); wav.write('data', 36); wav.writeUInt32LE(32000, 40);
  for (let i = 0; i < 16000; i++) wav.writeInt16LE(Math.round(2000 * Math.sin(2 * Math.PI * 440 * i / 16000)), 44 + 2 * i);
  for (const clip of clips) await fs.writeFile(path.join(folder, clip.path), wav);
  url = pathToFileURL(path.join(folder, 'index.html')).href;
});
test.afterAll(async () => { if (folder && path.dirname(folder) === os.tmpdir() && path.basename(folder).startsWith('ava-review-fixture-')) await fs.rm(folder, { recursive: true, force: true }); });
test.beforeEach(async ({ page }) => { page.on('dialog', dialog => dialog.accept()); await page.goto(url); });

test('LR01/LR02/LR06: flagged first, unreviewed, real offline audio and inert metadata', async ({ page }) => {
  const external = [];
  page.on('request', request => { if (!['file:', 'blob:'].includes(new URL(request.url()).protocol)) external.push(request.url()); });
  await page.reload();
  await expect(page.locator('article').first()).toHaveAttribute('data-path', 'flagged #1.wav');
  for (const select of await page.locator('article select').all()) await expect(select).toHaveValue('unreviewed');
  await expect(page.locator('.metadata').filter({ hasText: malicious })).toHaveCount(1);
  expect(await page.evaluate(() => window.injected)).toBeUndefined();
  const audio = page.locator('audio').first();
  await expect(audio).toHaveAttribute('preload', 'none');
  const result = await audio.evaluate(async element => { await element.play(); await new Promise(resolve => element.addEventListener('timeupdate', resolve, { once: true })); element.pause(); return { duration: element.duration, time: element.currentTime }; });
  expect(result.duration).toBeCloseTo(1); expect(result.time).toBeGreaterThan(0); expect(external).toEqual([]);
});

test('LR03/LR04/LR07: filtered export includes inventory and import resumes after reset', async ({ page }) => {
  await page.getByRole('button', { name: 'Export decisions' }).click();
  await expect(page.getByRole('status')).toContainText('Enter a reviewer');
  await page.getByLabel('Reviewer name').fill('Fixture reviewer');
  await page.locator('article').first().locator('select').selectOption('needs_recut');
  await page.locator('article').first().locator('textarea').fill('Clip ending needs inspection.');
  await page.getByLabel('Show clips').selectOption('unreviewed');
  await expect(page.locator('article:visible')).toHaveCount(1);
  const downloadPromise = page.waitForEvent('download'); await page.getByRole('button', { name: 'Export decisions' }).click();
  const download = await downloadPromise; const saved = JSON.parse(await fs.readFile(await download.path(), 'utf8'));
  expect(saved.report_sha256).toBe(data.report_sha256); expect(saved.decisions).toHaveLength(2);
  expect(saved.decisions[1]).toMatchObject({ decision: 'needs_recut', notes: 'Clip ending needs inspection.', sha256: clips[1].sha256 });
  await page.reload();
  await expect(page.locator('article').first().locator('select')).toHaveValue('unreviewed');
  await upload(page, saved); await expect(page.getByRole('status')).toContainText('Imported decisions');
  await expect(page.locator('article').first().locator('select')).toHaveValue('needs_recut');
  await expect(page.locator('article').first().locator('textarea')).toHaveValue('Clip ending needs inspection.');
  await expect(page.getByLabel('Reviewer name')).toHaveValue('Fixture reviewer');
});

test('LR05: invalid imports reject atomically across schema, inventory and value errors', async ({ page }) => {
  await page.getByLabel('Reviewer name').fill('Existing reviewer');
  await page.locator('article').first().locator('select').selectOption('reject');
  await page.locator('article').first().locator('textarea').fill('Keep this existing note.');
  const changes = [x => x.schema_version = 2, x => x.report_sha256 = 'wrong', x => x.decisions.pop(), x => x.decisions.push(x.decisions[0]), x => x.decisions[1] = x.decisions[0], x => x.decisions[0].path = 'unknown.wav', x => x.decisions[0].sha256 = 'wrong', x => x.decisions[0].decision = 'approved', x => x.decisions[0].notes = 2, x => x.reviewer = '', x => x.reviewer = {}];
  const invalid = changes.map(change => { const value = valid(); change(value); return value; }); invalid.push('{invalid JSON');
  for (const value of invalid) {
    await upload(page, value); await expect(page.getByRole('status')).toContainText('Import rejected');
    await expect(page.locator('article').first().locator('select')).toHaveValue('reject');
    await expect(page.locator('article').first().locator('textarea')).toHaveValue('Keep this existing note.');
    await expect(page.getByLabel('Reviewer name')).toHaveValue('Existing reviewer');
  }
});

test('LR06/LR08: imported text is inert and dirty edits retain unload protection after export', async ({ page }) => {
  const value = valid(); value.reviewer = malicious; value.decisions[1].notes = malicious;
  await upload(page, value); await expect(page.getByRole('status')).toContainText('Imported decisions');
  await expect(page.getByLabel('Reviewer name')).toHaveValue(malicious);
  await expect(page.locator('article').first().locator('textarea')).toHaveValue(malicious);
  expect(await page.evaluate(() => window.injected)).toBeUndefined();
  const cancelled = () => page.evaluate(() => { const event = new Event('beforeunload', { cancelable: true }); window.dispatchEvent(event); return event.defaultPrevented; });
  expect(await cancelled()).toBe(false);
  await page.getByLabel('Reviewer name').fill('Edited reviewer'); expect(await cancelled()).toBe(true);
  const downloadPromise = page.waitForEvent('download'); await page.getByRole('button', { name: 'Export decisions' }).click(); await downloadPromise;
  expect(await cancelled()).toBe(true); await expect(page.getByRole('status')).toContainText('Verify the downloaded JSON');
});

test('LR09: cancelling import preserves edits made while the file was loading', async ({ page }) => {
  await page.evaluate(() => { File.prototype.text = function () { return new Promise(resolve => { window.finishImport = resolve; }); }; });
  await upload(page, valid());
  await page.getByLabel('Reviewer name').fill('New unsaved reviewer');
  await page.locator('article').first().locator('textarea').fill('Edited while reading file.');
  page.removeAllListeners('dialog');
  page.on('dialog', dialog => dialog.type() === 'confirm' ? dialog.dismiss() : dialog.accept());
  await page.evaluate(value => window.finishImport(JSON.stringify(value)), valid());
  await expect(page.getByRole('status')).toContainText('Import cancelled');
  await expect(page.getByLabel('Reviewer name')).toHaveValue('New unsaved reviewer');
  await expect(page.locator('article').first().locator('textarea')).toHaveValue('Edited while reading file.');
});

test('LR10: stale asynchronous import cannot replace a newer successful import', async ({ page }) => {
  await page.evaluate(() => { window.imports = []; File.prototype.text = function () { return new Promise(resolve => window.imports.push(resolve)); }; });
  await upload(page, valid()); await upload(page, valid());
  const newer = valid(); newer.reviewer = 'Newer import'; newer.decisions[1].decision = 'keep';
  await page.evaluate(value => window.imports[1](JSON.stringify(value)), newer);
  await expect(page.getByRole('status')).toContainText('Imported decisions');
  await page.evaluate(value => window.imports[0](JSON.stringify(value)), valid());
  await expect(page.getByLabel('Reviewer name')).toHaveValue('Newer import');
  await expect(page.locator('article').first().locator('select')).toHaveValue('keep');
});

test('LR11: only one clip plays and hiding it pauses playback', async ({ page }) => {
  const audios = page.locator('audio');
  await audios.nth(0).evaluate(audio => audio.play());
  await audios.nth(1).evaluate(audio => audio.play());
  expect(await audios.nth(0).evaluate(audio => audio.paused)).toBe(true);
  await page.getByLabel('Show clips').selectOption('flagged');
  expect(await audios.nth(1).evaluate(audio => audio.paused)).toBe(true);
});
