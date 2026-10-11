const { test, expect } = require('@playwright/test');
const http = require('node:http');
const fs = require('node:fs/promises');
const path = require('node:path');
const { crawl } = require('../../scripts/browser/crawl.cjs');

// The crawler owns tracing for its contexts; runner tracing would start twice.
test.use({ trace: 'off' });

// Infrastructure fixtures only: these are not AVA product acceptance tests.
async function serve(handler) {
  const server = http.createServer(handler);
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  return { origin: `http://127.0.0.1:${server.address().port}`,
    close: () => new Promise(resolve => { server.close(resolve); server.closeAllConnections(); }) };
}

test('QA-FND-001/002: bounded fixture crawl deduplicates cycles and writes evidence', async ({}, info) => {
  const fixture = await serve((req, res) => {
    res.setHeader('Content-Type', 'text/html');
    res.end('<title>Infrastructure fixture</title><h1>Fixture only</h1><a href="/">Home</a><a href="/#again">Fragment</a><a href="/two">Two</a><a href="/three">Three</a>');
  });
  try {
    const outputDir = info.outputPath('crawl');
    const report = await crawl({ url: fixture.origin, allowOrigin: fixture.origin, maxPages: 2, outputDir });
    expect(report.pages.map(page => new URL(page.url).pathname)).toEqual(['/', '/two']);
    expect(report.truncated).toBe(true);
    expect(report.ok).toBe(true);
    expect(JSON.parse(await fs.readFile(path.join(outputDir, 'report.json'), 'utf8')).kind).toBe('browser-crawl');
    expect((await fs.stat(path.join(outputDir, 'trace.zip'))).size).toBeGreaterThan(0);
    expect((await fs.stat(path.join(outputDir, '1.png'))).size).toBeGreaterThan(0);
  } finally { await fixture.close(); }
});

test('QA-FND-003: external links, fetches, images and redirects never reach another origin', async ({}, info) => {
  let externalHits = 0;
  const external = await serve((req, res) => { externalHits++; res.end('external'); });
  const fixture = await serve((req, res) => {
    if (req.url === '/redirect') { res.writeHead(302, { Location: `${external.origin}/redirected` }); return res.end(); }
    res.setHeader('Content-Type', 'text/html');
    res.end(`<h1>Fixture</h1><a href="${external.origin}/linked">External port</a><a href="http://localhost:${new URL(external.origin).port}/">Other host</a><a href="/redirect">Redirect</a><img src="${external.origin}/image"><script>fetch('${external.origin}/fetch').catch(() => {});</script>`);
  });
  try {
    const report = await crawl({ url: fixture.origin, allowOrigin: fixture.origin, outputDir: info.outputPath('crawl') });
    expect(externalHits).toBe(0);
    expect(report.skipped.filter(item => item.reason === 'external-origin')).toHaveLength(2);
    expect(report.blocked.some(item => item.url.endsWith('/image'))).toBe(true);
    expect(report.blocked.some(item => item.url.endsWith('/fetch'))).toBe(true);
    expect(report.blocked.some(item => item.reason === 'redirect')).toBe(true);
    expect(report.ok).toBe(false);
  } finally { await fixture.close(); await external.close(); }
});

test('QA-FND-004: HTTP and JavaScript failures remain distinct evidence', async ({}, info) => {
  const fixture = await serve((req, res) => {
    res.setHeader('Content-Type', 'text/html');
    if (req.url === '/missing') { res.statusCode = 404; return res.end('missing'); }
    if (req.url === '/broken') { res.statusCode = 500; return res.end('broken'); }
    res.end('<script>throw new Error("fixture-script-failure")</script><a href="/missing">Missing</a><a href="/broken">Broken</a>');
  });
  try {
    const report = await crawl({ url: fixture.origin, allowOrigin: fixture.origin, outputDir: info.outputPath('crawl') });
    expect(report.pages.map(page => page.status)).toEqual([200, 404, 500]);
    expect(report.pages[0].errors).toContainEqual({ kind: 'pageerror', message: 'fixture-script-failure' });
    expect(report.pages[1].errors).toContainEqual({ kind: 'http', message: 'HTTP 404' });
    expect(report.ok).toBe(false);
  } finally { await fixture.close(); }
});

test('QA-FND-005: navigation timeout produces a report and completes cleanup', async ({}, info) => {
  const fixture = await serve(() => {});
  try {
    const outputDir = info.outputPath('crawl');
    const report = await crawl({ url: fixture.origin, allowOrigin: fixture.origin, timeoutMs: 500, outputDir });
    expect(report.pages[0].errors.some(error => error.kind === 'navigation')).toBe(true);
    expect(report.ok).toBe(false);
    expect(JSON.parse(await fs.readFile(path.join(outputDir, 'report.json'), 'utf8')).finishedAt).toBeTruthy();
  } finally { await fixture.close(); }
});

test('QA-FND-007: scripted POST and WebSocket requests are blocked', async ({}, info) => {
  let postHits = 0;
  let websocketHits = 0;
  const server = http.createServer((req, res) => {
    if (req.method === 'POST') postHits++;
    res.setHeader('Content-Type', 'text/html');
    res.end('<script>fetch("/mutation", {method:"POST"}).catch(() => {}); new WebSocket(location.origin.replace("http:", "ws:") + "/socket");</script><h1>Request fixture</h1>');
  });
  server.on('upgrade', (req, socket) => { websocketHits++; socket.destroy(); });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  try {
    const report = await crawl({ url: origin, allowOrigin: origin, outputDir: info.outputPath('crawl') });
    expect(postHits).toBe(0);
    expect(websocketHits).toBe(0);
    expect(report.blocked.some(item => item.url.endsWith('/mutation'))).toBe(true);
  } finally { await new Promise(resolve => { server.close(resolve); server.closeAllConnections(); }); }
});

test('QA-FND-006: explicit origin opt-in and finite bounds required before launch', async ({}, info) => {
  const base = { url: 'http://127.0.0.1:1234', outputDir: info.outputPath('crawl') };
  await expect(crawl(base)).rejects.toThrow('allowOrigin');
  await expect(crawl({ ...base, allowOrigin: 'http://localhost:1234' })).rejects.toThrow('allowOrigin');
  await expect(crawl({ ...base, allowOrigin: base.url, maxPages: 0 })).rejects.toThrow('maxPages');
  await expect(crawl({ ...base, allowOrigin: base.url, timeoutMs: Infinity })).rejects.toThrow('timeoutMs');
  await expect(crawl({ ...base, url: 'file:///secret', allowOrigin: 'null' })).rejects.toThrow('HTTP(S)');
});

test('QA-FND-008: missing same-origin subresources fail an otherwise valid page', async ({}, info) => {
  let missingHits = 0;
  const fixture = await serve((req, res) => {
    if (req.url === '/missing.js') { missingHits++; res.writeHead(404); return res.end('missing'); }
    res.setHeader('Content-Type', 'text/html');
    res.end('<title>Valid document</title><script src="/missing.js"></script>');
  });
  try {
    const report = await crawl({ url: fixture.origin, allowOrigin: fixture.origin, outputDir: info.outputPath('crawl') });
    expect(report.pages[0].status).toBe(200);
    expect(missingHits).toBe(1);
    expect(report.pages[0].errors).toEqual([]);
    expect(report.errors).toContainEqual(expect.objectContaining({ kind: 'request-http', status: 404,
      url: `${fixture.origin}/missing.js`, resourceType: 'script' }));
    expect(report.ok).toBe(false);
  } finally { await fixture.close(); }
});

test('QA-FND-009: same-origin network failure and timeout are retained as crawl errors', async ({}, info) => {
  const fixture = await serve((req, res) => {
    if (req.url === '/disconnect.js') return req.socket.destroy();
    if (req.url === '/timeout.js') return;
    res.setHeader('Content-Type', 'text/html');
    res.end('<script>fetch("/disconnect.js").catch(() => {}); fetch("/timeout.js").catch(() => {});</script>');
  });
  try {
    const outputDir = info.outputPath('crawl');
    const report = await crawl({ url: fixture.origin, allowOrigin: fixture.origin, timeoutMs: 1000, outputDir });
    for (const resource of ['/disconnect.js', '/timeout.js']) {
      expect(report.errors).toContainEqual(expect.objectContaining({ kind: 'request-failed', url: fixture.origin + resource }));
    }
    expect(report.ok).toBe(false);
    expect(JSON.parse(await fs.readFile(path.join(outputDir, 'report.json'), 'utf8')).errors).toEqual(report.errors);
  } finally { await fixture.close(); }
});

test('QA-FND-011: successful same-origin script and fetch keep crawl healthy', async ({}, info) => {
  const hits = [];
  const fixture = await serve((req, res) => {
    hits.push(req.url);
    if (req.url === '/healthy.js') { res.setHeader('Content-Type', 'text/javascript'); return res.end('fetch("/data.json").then(r => r.json()).then(() => document.body.dataset.ready = "yes");'); }
    if (req.url === '/data.json') { res.setHeader('Content-Type', 'application/json'); return res.end('{"ok":true}'); }
    res.setHeader('Content-Type', 'text/html');
    res.end('<h1>Healthy fixture</h1><script src="/healthy.js"></script>');
  });
  try {
    const report = await crawl({ url: fixture.origin, allowOrigin: fixture.origin, outputDir: info.outputPath('crawl') });
    expect(hits).toEqual(expect.arrayContaining(['/healthy.js', '/data.json']));
    expect(report.errors).toEqual([]);
    expect(report.ok).toBe(true);
  } finally { await fixture.close(); }
});

test('QA-FND-010: unavailable trace destination fails crawl evidence status', async ({}, info) => {
  const fixture = await serve((req, res) => res.end('<h1>Fixture</h1>'));
  try {
    const outputDir = info.outputPath('crawl');
    await fs.mkdir(path.join(outputDir, 'trace.zip'), { recursive: true });
    const report = await crawl({ url: fixture.origin, allowOrigin: fixture.origin, outputDir });
    expect(report.traceError).toBeTruthy();
    expect(report.ok).toBe(false);
  } finally { await fixture.close(); }
});
