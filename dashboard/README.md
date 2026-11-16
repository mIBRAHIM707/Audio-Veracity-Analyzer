# AVA dashboard (backlog 2.3)

React 18 + TypeScript + Vite single-page app (architecture §2.6, ADR-6), built to
static files that the analysis API serves on its own origin: no CORS, no CDN, no
web fonts, no telemetry (PRD FR-U8). It holds the live call board (below) and the
batch upload page (FR-U4):

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

Infrastructure evidence only: the fake detector says nothing about accuracy.

### Real checkpoint and public audio (backlog 2.QA, local only)

`tests/browser/real-model-dashboard.spec.cjs` (5 Claude-authored cases, project
`real-model`) starts the API with the pinned AntiDeepfake MMS-300M checkpoint
(`serve_dashboard_api.py --model-root`), waits for `/readyz`, and uploads prepared
public CSALT clips concatenated in memory: genuine pilot and synthetic benchmark
audio (about 20 s each), one short clip (undetermined), a job over 60 s, and two
damaged files (422). Each case asserts that the page shows exactly the API's
verdict, mean, model ID and every timeline row. Verdicts are logged as observations,
never asserted: this is not an accuracy result. It needs a gitignored
`.local/real-model-qa.json` and is skipped without it:

```json
{"model_root": "<local checkpoint dir>", "threads": 4,
 "genuine": ["<prepared PCM16 mono clip>", "..."], "synthetic": ["..."]}
```

```text
npm run test:real-model
```

## Live call board (backlog 7.1, 16 Nov 2026)

`#live` (link "Live calls"; the batch page stays the default, so a batch-only server
never opens an event connection). Sources: `GET /v1/calls?status=all` and
`WS /v1/events` on the same origin (`src/live.ts` pure state, `src/LiveBoard.tsx`).

- FR-U1 card per active call: call ID, duration (server event time, corrected for the
  browser clock from the newest event), state as text (`Monitoring`, `Suspicious`,
  `Alert`, `No speech`, `Unmonitored`; colour only adds to it), confidence = the
  newest smoothed score (EMA, labelled uncalibrated), a sparkline of the last 20
  window scores (gaps for unscored updates, dashed line at 0.5), open/total alerts and
  flags: low audio quality, degraded (reduced scoring rate), no events for 5 min
  (registry `stale`). Up to 20 ended calls are listed with their end reason. The
  limitation text is shown above the board.
- FR-U2 alert feed (newest first, at most 50): a new live alert shows a `role=alert`
  banner, puts the open-alert count in the tab title, and plays a short Web Audio tone
  (no audio file) once the user turns sound on (browsers block sound until a gesture).
  Alerts already open when the page loads come from the call details, without sound.
  Cleared alerts and alerts open at call end are marked as such.
- FR-U3 call timeline ("Show timeline"): `GET /v1/calls/{id}` scored points as bars
  spanning the scoring stride before each score's publish time (seconds since the call
  started; 2 s normally), windows at or above 0.5 marked, direction in the note column,
  re-read when the call has new events (throttled to one read a second; a read in
  progress is never aborted by a newer event, the next one follows it); the table shows
  the newest 120 rows, the chart every window; the first column is the event `seq`.
  Older 10 s summaries are counted.
- Merge rule: REST snapshot first, then events with `seq` above the call's `last_seq`
  (new registry summary field); events that arrive while the snapshot loads are queued.
  `stream.gap` (dropped or upstream-lost events) re-reads REST. A closed socket shows
  "Live updates disconnected … the board may be out of date" and reconnects after 1, 2,
  4, 8, then every 10 s, each with jitter of ±25%, re-reading REST each time. The
  back-off restarts only after a healthy connection (a frame received or open 5 s), so
  a server that accepts and at once closes (no live source, too many subscribers) is not
  retried every second. A 503 (no live source) is shown. Unreadable frames are counted
  and ignored; malformed REST bodies are an error text. Late events for an ended call
  the page has already pruned are skipped (no phantom card). A call revived after
  `idle_expired` reopens its alerts open at the expiry, as the registry does.
- Server side: `WS /v1/events` subscribes to the hub before accepting the upgrade, so a
  snapshot read as soon as the socket opens cannot miss an event.

Tests (Claude-authored, 16 Nov 2026): `tests/browser/dashboard-live.spec.cjs` (12
cases, LB01-LB12) use the real API with a real `EventHub`; events enter through the
hub's validation from a loopback test control server (`serve_dashboard_api.py
--control-port`); `routeWebSocket` drops, withholds, replays or injects frames.
`tests/browser/live-state.spec.ts` (LS01-LS10) tests the pure merge rules in
`src/live.ts` with Playwright's runner (no browser; runs in the `foundation` project,
so CI runs it). Mutation checks: without the `seq` rule LB04 fails; without the gap
re-read LB06 fails; resetting the back-off on `open` fails LB10.

Found while building it: `requirements-api.txt` had no WebSocket library, so a real
uvicorn refused every `WS /v1/events` upgrade (earlier tests used the in-process
TestClient). `websockets==17.4` is now pinned there.

Known limits: the live timeline uses publish time, not audio time (the event carries a
window index, not its audio seconds); bars of both directions share one axis. The
board's clock skew comes from the newest envelope's `ts`, so relayed or backlogged
events can make durations jump. Reconciliation after a disconnection marks feed items
cleared only when the call has no open alert (alert A cleared and B raised meanwhile
leaves A shown open until the next detail read). A re-raise of an alert whose details
were never read (more than 16 calls with alerts at load) counts and sounds as new. The
page's total alert count can pass the registry's 256-alert cap until the next
snapshot. A revived call's open count is rebuilt from the 50-item feed, so it can be
low until the next snapshot. No live view in another language or right-to-left text yet.

## Not yet

Call detail with codec and snippets (FR-U5, P1), history and search (FR-U6), TanStack
Query, MP3/AMR/GSM input (the API accepts PCM16 WAV only), job cancellation,
authentication (7.3), Urdu/RTL text.
