# AVA dashboard (backlog 2.3)

React 18 + TypeScript + Vite single-page app (architecture §2.6, ADR-6), built to
static files that the analysis API serves on its own origin: no CORS, no CDN, no
web fonts, no telemetry (PRD FR-U8). Today it holds the batch upload page (FR-U4):

- drag-and-drop or choose one PCM16 WAV; files over 100 MB or empty are refused
  in the browser before upload;
- files up to 60 s return at once (the upload request is bounded at 5 min); longer
  files come back as a job that the page polls every second. Each status read is
  bounded at 30 s (`?requestTimeoutMs=` can only shorten it, 1-30 s, for diagnostics
  and tests) and never past the 20 min overall deadline; up to 5 consecutive
  network/time-out/5xx errors are tolerated; a 404 ends polling. "Stop waiting"
  appears only while polling and stops polling only: the API has no job
  cancellation yet (backlog 5.2), so an upload in progress is never abandoned;
- every rendered field of a result is validated (a malformed body is an error, not
  a blank page), with an error boundary around the result as a last resort; a
  failed job shows its own reason (`The job failed (HTTP n): detail`);
- verdict, mean score, evidence windows, reason when undetermined, model ID, the
  service's scope text and flags, with an **uncalibrated baseline** label whenever
  the result says `calibration: uncalibrated_baseline`;
- the per-window timeline (FR-U3) as an SVG bar chart plus a table; windows at or
  above 0.5 are marked "scored synthetic", partial windows are faded;
- the PRD §1.4 limitation text (FR-U1).

Browser floor: `AbortSignal.any`/`AbortSignal.timeout` (Chromium 116+, Firefox 124+,
Safari 17.4+); older browsers cannot upload. Server text (error details, file names)
is rendered as text only. A CSP meta tag
limits scripts, styles and connections to the same origin.

## Build and run

Pinned exact versions in `package.json`, locked with integrity hashes by
`package-lock.json` (React 18.3.1, Vite 5.4.11, TypeScript 5.6.3; 67 packages). The
bundled runtime (react, react-dom, scheduler, loose-envify, js-tokens) is MIT. Build-
only tools are MIT, ISC, Apache-2.0 (TypeScript, baseline-browser-mapping) or
BSD-3-Clause (source-map-js); caniuse-lite data is CC-BY-4.0 and is not shipped.

```text
npm ci --prefix dashboard
npm run build --prefix dashboard          # tsc --noEmit, then vite build -> dashboard/dist
```

Serve it from the API with `create_app(service, dashboard_dir="dashboard/dist")`;
the build is mounted at `/` after every API route, so it never shadows one.

## Tests

`tests/browser/dashboard-upload.spec.cjs` (13 Claude-authored Playwright cases,
14 Nov 2026) start the real API with a deterministic fake detector
(`tests/browser/serve_dashboard_api.py`) and drive the built page: synchronous and
job paths, timeline marking, undetermined, 422, detector 500, client-side refusals,
drag and drop, inert hostile text, same-origin requests only, no CSP violations,
and ([mock]) busy, network loss, malformed bodies, failed/vanished jobs, a status
read that never answers, and stop-waiting.

They form the Playwright project `dashboard`, which needs a build, `uv` and
`requirements-api.txt`; `npm run test:browser` (the CI foundation job) runs only the
`foundation` project. A bare `npx playwright test` runs both projects and fails without
a build; use the npm scripts.

```text
npm ci
npm run test:dashboard
```

Known limits: on Windows, stopping the test server kills `uv`; whether the Python
child always exits with it was not confirmed (each run uses a fresh free port). A
WebSocket to an unknown path reaches the static mount and fails the handshake
rather than closing cleanly. Not exercised by a test: the result error boundary
(validation stops every malformed body first) and a response body that stalls
mid-read (Playwright route mocks cannot stall a body; the code reports it as a
time-out).

Infrastructure evidence only: the fake detector says nothing about accuracy. Real
audio through the page with the real checkpoint is backlog 2.QA.

## Not yet

Live call board, alert feed and call detail (backlog 7.1), TanStack Query (one page
does not need it yet), MP3/AMR/GSM input (the API accepts PCM16 WAV only), job
cancellation, authentication (7.3), Urdu/RTL text.
