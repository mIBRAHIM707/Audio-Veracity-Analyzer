# Browser QA tooling

This repository currently has no AVA web application. The automated browser suite
tests controlled infrastructure fixtures, not detection quality or AVA acceptance.
The same commands work in Claude Code, Codex, and an ordinary terminal.

## Install and test

Requires Node.js 22 or newer.

```sh
npm ci
npx playwright install chromium
npm run test:browser
```

On Linux CI, install browser system dependencies with
`npx playwright install --with-deps chromium`.

The test suite starts ephemeral loopback HTTP servers and closes them in `finally`
blocks. No product server, data recording, inference model, or paid AI account is
required. Playwright's HTML report is in `playwright-report`; failed-test traces
and screenshots are in `test-results`. Crawler fixture evidence is also retained
under each test's output folder.

## Crawl an authorized development deployment

```sh
npm run crawl -- --url http://127.0.0.1:3000 --allow-origin http://127.0.0.1:3000 --max-pages 10
```

The server must already be running. Both URL and exact origin are mandatory;
there is no default production target. Only use this against a development target
you own or have permission to inspect. JavaScript on each page runs normally;
even GET requests can have side effects on poorly designed servers. This is
browser request filtering, not a security sandbox for hostile websites.

The crawler:

- Visits at most 1–100 pages, default 10, with a per-navigation timeout of
  100–30000 milliseconds, default 5000 (`--timeout-ms`).
- Deduplicates URLs after removing fragments; it does not validate fragment IDs.
- Follows anchor links on the exact origin only, including scheme and port.
- Blocks cross-origin HTTP requests, non-GET/HEAD requests, service workers,
  WebSockets, and all HTTP redirects (including same-origin redirects).
- Does not click buttons, submit forms, sign in, retain session credentials, or
  exercise AVA workflows automatically. Add explicit acceptance tests once an
  application exists.
- Captures HTTP failures, JavaScript exceptions, navigation failures, screenshots,
  and a browser trace. Console warnings and accessibility checks are not covered.

Default output is `artifacts/browser/<timestamp>-<process-id>/`; `--output DIR`
overrides it. Use a new directory per run to preserve evidence. `report.json`
identifies the target, times, bounds, pages, skipped links, blocked requests, and
errors. Exit code 1 indicates page errors, same-origin HTTP/network failures
(including subresources), trace-write failures, bad arguments, or a fatal failure.
Expected blocked external subresources are recorded but do not alone fail a run.
Screenshots/traces may contain page content; keep generated evidence out of Git.

Open a trace with `npx playwright show-trace <path-to-trace.zip>`.
