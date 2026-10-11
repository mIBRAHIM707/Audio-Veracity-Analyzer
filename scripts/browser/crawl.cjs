const { chromium } = require('@playwright/test');
const fs = require('node:fs/promises');
const path = require('node:path');
const { parseArgs } = require('node:util');

function validateTarget(value) {
  const url = new URL(value);
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) {
    throw new Error('Target must be an HTTP(S) URL without embedded credentials.');
  }
  url.hash = '';
  return url;
}

async function crawl({ url, allowOrigin, maxPages = 10, timeoutMs = 5000, outputDir }) {
  const target = validateTarget(url);
  if (allowOrigin !== target.origin) throw new Error('Explicit allowOrigin must exactly match the target origin.');
  if (!Number.isInteger(maxPages) || maxPages < 1 || maxPages > 100) throw new Error('maxPages must be 1–100.');
  if (!Number.isInteger(timeoutMs) || timeoutMs < 100 || timeoutMs > 30000) throw new Error('timeoutMs must be 100–30000.');
  if (!outputDir) throw new Error('An outputDir is required.');
  await fs.mkdir(outputDir, { recursive: true });
  const report = { kind: 'browser-crawl', target: target.href, origin: target.origin,
    startedAt: new Date().toISOString(), maxPages, pages: [], errors: [], blocked: [], skipped: [], truncated: false };
  let browser;
  let context;
  try {
    browser = await chromium.launch();
    context = await browser.newContext({ serviceWorkers: 'block', acceptDownloads: false });
    await context.tracing.start({ screenshots: true, snapshots: true });
    await context.routeWebSocket('**/*', socket => socket.close());
    await context.route('**/*', async route => {
      const request = route.request();
      const requestURL = new URL(request.url());
      if (requestURL.origin !== target.origin || !['GET', 'HEAD'].includes(request.method())) {
        report.blocked.push({ url: request.url(), reason: 'origin-or-method' });
        return route.abort('blockedbyclient');
      }
      try {
        // Do not let browser redirect handling bypass the origin boundary.
        const response = await route.fetch({ maxRedirects: 0, timeout: timeoutMs });
        if (response.status() >= 400) {
          report.errors.push({ kind: 'request-http', url: request.url(), status: response.status(),
            resourceType: request.resourceType(), message: `HTTP ${response.status()}` });
        }
        if (response.status() >= 300 && response.status() < 400 && response.headers().location) {
          report.blocked.push({ url: request.url(), reason: 'redirect', location: response.headers().location });
          return await route.fulfill({ status: 409, contentType: 'text/plain', body: 'Crawler does not follow redirects.' });
        }
        return await route.fulfill({ response });
      } catch (error) {
        report.errors.push({ kind: 'request-failed', url: request.url(),
          resourceType: request.resourceType(), message: error.message });
        await route.abort('failed').catch(() => {});
      }
    });
    const queue = [target.href];
    const seen = new Set(queue);
    while (queue.length && report.pages.length < maxPages) {
      const current = queue.shift();
      const page = await context.newPage();
      page.setDefaultTimeout(timeoutMs);
      const entry = { url: current, status: null, errors: [], screenshot: `${report.pages.length + 1}.png` };
      page.on('pageerror', error => entry.errors.push({ kind: 'pageerror', message: error.message }));
      page.on('dialog', dialog => dialog.dismiss());
      try {
        const response = await page.goto(current, { waitUntil: 'load', timeout: timeoutMs });
        entry.status = response?.status() ?? null;
        if (entry.status >= 400) entry.errors.push({ kind: 'http', message: `HTTP ${entry.status}` });
        entry.title = await page.title();
        for (const href of await page.locator('a[href]').evaluateAll(links => links.map(link => link.href))) {
          let link;
          try { link = validateTarget(href); } catch { report.skipped.push({ url: href, reason: 'unsupported-url' }); continue; }
          if (link.origin !== target.origin) { report.skipped.push({ url: href, reason: 'external-origin' }); continue; }
          if (!seen.has(link.href)) { seen.add(link.href); queue.push(link.href); }
        }
      } catch (error) { entry.errors.push({ kind: 'navigation', message: error.message }); }
      try { await page.screenshot({ path: path.join(outputDir, entry.screenshot), timeout: timeoutMs }); }
      catch (error) { entry.errors.push({ kind: 'screenshot', message: error.message }); entry.screenshot = null; }
      finally { await page.close(); }
      report.pages.push(entry);
    }
    report.truncated = queue.length > 0;
  } catch (error) {
    report.fatalError = error.message;
    throw error;
  } finally {
    if (context) {
      // Drain in-flight route handlers before the final report is serialized.
      await context.unrouteAll({ behavior: 'wait' });
      await context.tracing.stop({ path: path.join(outputDir, 'trace.zip') }).catch(error => { report.traceError = error.message; });
    }
    if (browser) await browser.close();
    report.finishedAt = new Date().toISOString();
    report.ok = !report.fatalError && !report.traceError && !report.errors.length
      && report.pages.every(page => !page.errors.length);
    await fs.writeFile(path.join(outputDir, 'report.json'), JSON.stringify(report, null, 2) + '\n');
  }
  return report;
}

if (require.main === module) {
  (async () => {
    const { values } = parseArgs({ options: { url: { type: 'string' }, 'allow-origin': { type: 'string' },
      'max-pages': { type: 'string', default: '10' }, 'timeout-ms': { type: 'string', default: '5000' },
      output: { type: 'string' } } });
    if (!values.url || !values['allow-origin']) throw new Error('Usage: npm run crawl -- --url URL --allow-origin ORIGIN [--max-pages 10] [--output DIR]');
    const outputDir = values.output || path.join('artifacts', 'browser', `${Date.now()}-${process.pid}`);
    const report = await crawl({ url: values.url, allowOrigin: values['allow-origin'],
      maxPages: Number(values['max-pages']), timeoutMs: Number(values['timeout-ms']), outputDir });
    console.log(`Crawled ${report.pages.length} pages; evidence: ${outputDir}`);
    if (!report.ok) process.exitCode = 1;
  })().catch(error => { console.error(error.message); process.exitCode = 1; });
}

module.exports = { crawl, validateTarget };
