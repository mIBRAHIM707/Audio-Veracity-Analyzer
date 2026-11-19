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
  `idle_expired` reopens its alerts open at the expiry, as the registry does. A
  `call.started` later than a call's real end (or for a pruned call) opens a new
  monitoring session card, as the registry does since 18 Nov: the live service ends
  calls `monitor_lost` when it restarts and re-taps them with seqs from 1 (LS11).
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

## History and search (backlog 7.3, FR-U6, 19 Nov 2026)

`#history` (link "History"). Read only, from PostgreSQL through the API
(`services/store/history.py`): `GET /v1/history/calls`, `GET /v1/calls/{id}/history`
and `GET /v1/alerts` (`src/history.ts` pure parsing and queries, `src/HistoryPage.tsx`).
The page opens no WebSocket. Without a history store the API answers 503 and the page
says that no history database is configured.

- Stored calls: one row per stored monitoring session, newest first (a call killed and
  re-tapped has two rows; the killed one shows `monitor_lost (inferred: monitor lost)`),
  with start/end, final state, open/total alerts, scored/stored windows and the highest
  window score. Filter: all, without a stored end, ended. "Load older calls" pages with
  the API's `next_before` (50 a page).
- Alert search (FR-A6): state, raised-time range (from inclusive, to exclusive) and
  minimum confidence, newest raised first, 100 a page with the API's opaque cursor
  ("Load more alerts" reuses the filters the shown results were searched with).
  Confidence is the alert's peak EMA (uncalibrated; an open alert's value can still
  rise); the page says so. Filters are checked in the browser first (impossible dates,
  from not before to, confidence outside 0-1, and a field the browser holds as
  unfinished, such as a date without its time or `1e`, which reads as empty) and a
  refused filter is never sent. The alert search starts after the calls list has
  answered: the API's history reader serves one read at a time.
- CSV export ("Export these results as CSV"): the filters of the search shown (not
  unsearched form edits), newest 10,000 matches, fetched and saved as `ava-alerts.csv`
  (a same-origin `blob:` download); when the API reports more matches
  (`X-AVA-Next-Cursor`), the page says to narrow the time range.
- Call detail (choose a call ID in either list): every stored session of that call ID,
  newest first, with end (inferred or not), speech seconds, verdict, alerts, and the
  stored window scores as the shared timeline (bars span the stride before each score
  time, seconds since the session started; unscored windows left out; table newest 120
  rows). Its score column is "Score (uncalibrated)" and windows at 0.5 or above are
  marked "at or above 0.5", never as probabilities or "synthetic". Truncation by the
  API's bounds (16 sessions, 256 alerts, 10,800 windows) is stated. Closing the detail
  returns focus to the button that opened it.
- Bounds and failures: each read is limited to 30 s (the store's own bound is about
  25 s plus a reconnect; CSV 60 s); a newer search or page
  replaces one still running and the older answer is dropped; a list keeps at most 1,000
  rows (then it says to narrow the filters); store failures are explained by reason
  (unreachable, busy, timeout, not migrated), malformed answers are an error text, and
  server text is shown as text only. Never shown: start/end data of a call (the API does
  not serve it) or audio.

Known limits: times are entered and shown in UTC (`YYYY-MM-DD HH:MM:SS UTC`), not the
browser's zone, so an analyst in Pakistan (UTC+5) must convert. A read the page abandons
(time limit, newer search) keeps running on the server and holds the reader's single
connection, so a quick retry can be answered "busy". The detail chart draws every stored
window (up to 10,800 bars; only the table is capped); its render time at that size is
not measured. The cost of the calls page's per-row counts on a large store is not
measured either.

Tests (Claude-authored, 19 Nov 2026): `tests/browser/history-state.spec.ts` (HS01-HS10,
pure, `foundation` project, so CI runs them) and `tests/browser/dashboard-history.spec.cjs`
(project `dashboard`). HB01-HB07 use the real API reading a real PostgreSQL database
whose rows the production `PostgresEventStore` wrote from FR-A4 envelopes
(`serve_dashboard_api.py --history-dsn-env`, a fresh schema per run, dropped at the end);
they need `AVA_TEST_POSTGRES_DSN` (see `services/store/README.md`) and are skipped
without it:

```text
uv run --no-project --env-file .local/store-test.env npx.cmd playwright test --project=dashboard tests/browser/dashboard-history.spec.cjs
```

HM01 uses the real API without a store; HM02-HM06 are [mock] (failures by reason,
malformed answers, network loss, an older search answering last, export of the search
shown, export truncation and refusal, the 1,000-row cap, a vanished call). Mutation
check: without aborting the older search, HM03 fails. Independent review (19 Nov):
conditional pass; its M1 (unfinished inputs read as no filter, first confirmed failing
by HB03), M2 (probability wording), M3 (HB07 could not see the status filter), L1, L2,
L4-L7 and four nits are fixed; L3 is fixed for page load (sequential start) and
otherwise a known limit above, as is L8. Re-review: its N1 (a search made while the
calls list loads was replaced by the deferred default search, and the export with it)
is fixed with HM06, which fails without the fix; N3 fixed; N2 (the hook's "exactly 1,000
rows and no further page" branch has no browser case) is open.

## Sign in (backlog 7.3, FR-U7, 19 Nov 2026)

`AuthGate.tsx` and `auth.ts`. On load the dashboard asks `GET v1/auth/me`: 404 means the
API runs without accounts and the pages show as before; 401 shows the sign-in form; 200
shows the pages with "Signed in as NAME (role)" and a Sign out button. Pages are mounted
only while signed in: nothing is read and no event socket opens before sign-in, and
they are removed at sign-out, when a check finds the session ended, and at its absolute
end (12 h; timed from the server's `now_ts`, so a wrong PC clock cannot end or stretch
it). The session is an HttpOnly cookie the page never reads.
It is checked at once when any page gets a 401 or the live socket closes, when the tab
regains focus, and on a timer just after the server's idle limit (`idle_s` from
`/v1/auth/me`, 30 min + 5 s). Nothing polls more often, so these checks never keep an
idle session alive (review H1): batch and history pages left open with no request end at
the next timer check. An open live board stays signed in, because the server re-checks
its socket every minute and that counts as activity (a watched board is in use; record
as a decision); so does a board whose socket keeps failing to connect, since each close
asks for a session check. A server without accounts is asked again only after a 401 (it
was restarted with accounts on). A failed first check shows its reason and Try again, never the pages; a
failed re-check keeps the page with a "could not be verified" warning (the same store
failure refuses every data read). Refusals never say whether a name exists. A different
user signing in from another tab remounts the pages. There is no settings page yet
(admin-only routes are not built), so both roles see the same pages.

Tests (Claude-authored, 19 Nov): `tests/browser/auth-state.spec.ts` AS01-AS05 (pure, CI
`foundation` project) and `tests/browser/dashboard-auth.spec.cjs`: AB01-AB07 on the real
API with the production accounts in PostgreSQL (`serve_dashboard_api.py --auth`, cheap
Argon2 parameters for speed; fictional users `ana`/`boss`): nothing but the session check
before sign-in, refusals, cookie flags (HttpOnly, SameSite=Strict, Secure, not visible to
scripts), audit rows for logins, export and upload, sign-out revoking the token on the
server, a session revoked elsewhere, lockout text, cross-site login refused, the idle
re-check (Playwright clock; sessions aged on the server), a live board whose socket the
server closes after a revoke (test server re-checks sockets every 3 s), and a PC clock
13 h ahead (AB08). AN01 real API without accounts; AN02-AN05 [mock]: a failed first
check, a failed sign-out and re-check, a late check after sign-in, a server restarted
with accounts on. Mutation checks: without the 401 report in history reads AB04 fails;
polling every 60 s fails AB06; not dropping the in-flight check at sign-in fails AN04;
timing the end from the PC's date fails AB08.

```text
uv run --no-project --env-file .local/store-test.env npx.cmd playwright test --project=dashboard tests/browser/dashboard-auth.spec.cjs
```

## Alert feedback (backlog 7.3, FR-A7, 19 Nov 2026)

`AlertFeedback.tsx` and `feedback.ts`. In the stored call detail of `#history`, each
alert has a "Label this alert" control, shown only to a signed-in user (`SessionContext`
from `AuthGate.tsx`; a server without accounts has no feedback routes and shows none).
It stays collapsed until opened, so opening a call sends no feedback read. Opened, it
reads `GET v1/alerts/{id}/feedback` and shows the current label (who, when, UTC), the
earlier labels, and two buttons: "Mark as true positive (synthetic speech)" and "Mark as
false positive (not synthetic speech)". Each click sends one `POST` with
`{"label": ...}`, never retried; further clicks are ignored until it answers, and the
panel cannot be hidden meanwhile (buttons are `aria-disabled`, so focus stays). A write
is never cancelled by the page: leaving it mid-write still stores the label. A write
whose answer never arrives, or a 503 `unavailable`/`error` or other 5xx (possibly a
connection lost at COMMIT), says the label may or may not be stored and offers "Reload
labels". Known limit: a panel reopened before an earlier answer arrives does not know
about that write (a second label is the worst case: append-only, capped, audited). Labels are appended on the server: a new label becomes current, the earlier
ones stay. At 20 labels (the server's cap) the buttons are disabled; a 409 from a label
added elsewhere is shown as such. The server stores each label with a copy of the
alert's window scores and an audit row; both roles may label (decision of 19 Nov).
Store failures are named by reason (busy, unavailable, timeout, schema, error).

Tests (Claude-authored, 19 Nov): `tests/browser/feedback-state.spec.ts` FS01-FS05 (pure,
CI `foundation` project) and `tests/browser/dashboard-feedback.spec.cjs`: FB01-FB04 on the
real API with the production accounts and `AlertFeedbackStore` in PostgreSQL
(`serve_dashboard_api.py --auth`; the test-only control path `/feedback-rows` reads the
stored rows with their window copies): label and relabel by an analyst with the stored
window copy and audit rows checked, an admin labelling an open alert, clicks (and an
attempt to hide the panel) during a held write sending one label, the 20-label cap and
its audited 409, leaving the page mid-write (FB05). FN01 real API without accounts
(history [mock]); FN02-FN03 [mock]: read/write failures, an unanswered write, an
uncertain 503, a 201 with an unreadable body, a 401. Mutation checks: without the
double-submit guard FB03 fails; a toggle that hides the panel mid-write fails FB03;
aborting the write on unmount fails FB05; controls shown on a server without accounts
fail FN01. FB03 first found that a state-only guard let two clicks in one task send two
labels; the guard is now a ref. FB05 relies on FB02's label (serial spec).

```text
uv run --no-project --env-file .local/store-test.env npx.cmd playwright test --project=dashboard tests/browser/dashboard-feedback.spec.cjs
```

## Retained snippet playback (backlog 7.3, FR-U5, 19 Nov 2026)

`AlertSnippet.tsx` and `snippet.ts`. In the stored call detail each alert has a "Load
retained audio" control, shown only to a signed-in user (the route exists only with
accounts). Nothing is fetched until it is pressed, because each playback is an audited
audio export. One request per press (a ref guard, never retried): `GET
v1/alerts/{id}/snippet` with `cache: 'no-store'`; the answer must be `audio/wav` and a
PCM16 mono RIFF/WAVE whose header matches its length (8-48 kHz) before the browser gets
it. The audio is held as an in-memory blob URL in an `<audio controls>` player, revoked
when the player is closed, the alert changes or the page goes. Missing (404), expired
(410), unreadable (500), not set up, not audited, busy and unreachable each have their
own message; a 401 returns to sign-in. The page's Content-Security-Policy gained
`media-src 'self' blob:` (without it the browser refused the blob audio, found by SB01);
`connect-src` still refuses `blob:`.

Tests (Claude-authored, 19 Nov): `tests/browser/snippet-state.spec.ts` SS01-SS04 (pure,
CI `foundation` project) and `tests/browser/dashboard-snippet.spec.cjs`: SB01-SB03 on the
real API with the production accounts in PostgreSQL and the production AES-256-GCM
store and reader (`serve_dashboard_api.py --auth --snippets`: synthetic tones, random
key, temp folder): a kept snippet played by the browser's own decoder (duration 2.5 s),
audited before it is sent, `no-store`, the blob revoked on close; missing, expired and
changed snippets with their messages and no audit row; two clicks in one task send one
request. SN01 real API without accounts (history [mock]); SN02 [mock]: not set up, a
non-WAV body, an unreachable server, a 401.

```text
uv run --no-project --env-file .local/store-test.env npx.cmd playwright test tests/browser/snippet-state.spec.ts tests/browser/dashboard-snippet.spec.cjs
```

## Not yet

Codec detected in the call detail (FR-U5, P1), feedback from the alert search table
(feedback is given from the call detail), retraining from feedback (FR-A7, 5.6), TanStack
Query, MP3/AMR/GSM input (the API accepts PCM16 WAV only), job cancellation, the admin
settings page (FR-U7/FR-A9), Urdu/RTL text.
