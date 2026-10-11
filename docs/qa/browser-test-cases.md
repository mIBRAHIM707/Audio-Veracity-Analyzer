# Browser automation foundation test design

Designed on 2026-10-02 by the delegated **gpt-6-astra** agent. Actual model provenance applies to this design session; it is not an automatic model-routing guarantee.

There is **no AVA web application in this repository yet**. These cases exercise a controlled local HTML fixture, Playwright configuration, and a bounded crawler. The fixture must visibly identify itself as infrastructure testing. It must not imitate working audio ingestion, authentication, verdicts, model inference, live calls, or compliance evidence.

`INF-*` identifiers below are local infrastructure requirements, not invented BMAD product requirements. The [PRD](../../_bmad-output/planning-artifacts/prd.md) BO-5/FR-O3 motivate future zero-egress testing, and FR-U1–FR-U8 define future dashboard work. A fixture crawl cannot satisfy those product requirements. P0 means essential foundation behavior; P1 means additional resilience. All rows are **designed cases**; the executable suite and generated report determine which are implemented and passed. A catalog entry alone is not test evidence.

Preconditions: installed pinned browser tooling; deterministic server bound to loopback; fictional pages and identifiers; fresh browser context; explicit maximum page count and navigation timeout; outputs in an ignored run directory. Use reserved fake external URLs with interception or an alternate local test server for negative cases; do not probe real external sites. No production credentials or audio are needed.

| Case | Infrastructure requirement | Input / additional precondition | Expected result | Priority | Applicability |
|---|---|---|---|---|---|
| BR-01 | INF-START: deterministic startup | Start fixture and open root | Root loads; expected fixture heading; ready URL matches configured origin; no claim that AVA runs | P0 | Browser + server lifecycle |
| BR-02 | INF-CRAWL: same-origin discovery | Root → about → root cycle; repeated links; fragments to existing element | Each canonical page visited once; fragments do not create infinite revisits; bounded successful report | P0 | Browser + URL unit tests |
| BR-03 | INF-BOUNDS: finite work | More distinct local URLs than maximum; query variations; cyclic graph | Visits never exceed configured maximum; report identifies truncation so coverage is not overstated | P0 | Browser + crawler unit tests |
| BR-04 | INF-ORIGIN: explicit permitted origin | Missing or mismatched origin opt-in; malformed URL; non-HTTP scheme | Reject before launching navigation; useful error; no outbound request. Fixture suite uses loopback only; separately authorized deployments may be explicitly selected by crawler users | P0 | URL validation unit/CLI |
| BR-05 | INF-EGRESS: links | External host, different port, alternate loopback hostname, subdomain, protocol-relative URL, mailto/javascript links | Crawler skips destinations outside exact permitted HTTP(S) origin; records meaningful exclusions | P0 | Browser + URL unit tests |
| BR-06 | INF-EGRESS: redirects | Local endpoint redirects to disallowed origin | Request blocked before destination contact; navigation failure is attributable; crawler does not silently widen origin | P0 | Browser + controlled second local server request counter |
| BR-07 | INF-EGRESS: resources | Fixture attempts image, script and fetch loads at disallowed origin | Requests aborted; blocked URLs recorded; external network is never contacted | P0 | Browser interception + controlled second server |
| BR-08 | INF-HTTP: broken links | Reachable local links return 404 and 500 | Crawl records failing URL and status; failure propagates to command/report, without aborting all useful diagnostics | P0 | Browser |
| BR-09 | INF-NAV: timeout/refusal | Local hanging response or closed local port; short configured timeout | Bounded failure; diagnostic identifies navigation/connection failure; remaining lifecycle cleanup executes | P0 | Browser + CLI |
| BR-10 | INF-JS: runtime diagnostics | Local page throws exception and emits console error; healthy comparison page | Page exception and console error captured separately from HTTP errors; healthy page stays clean | P0 | Browser |
| BR-11 | INF-FRAGMENT: anchors | Link targets missing fragment on reachable page | Report missing target if anchor checking is implemented; otherwise explicitly state fragment validity is untested | P1 | Browser; optional crawler feature |
| BR-12 | INF-CLEANUP: success and failure | Run repeatedly; inject navigation or assertion failure | Context/pages/browser created by harness and fixture server close; no orphan listener; repeated run can reuse port | P0 | Browser + process lifecycle integration |
| BR-13 | INF-ARTIFACT: diagnostics | Successful run and deliberate failed run in temporary/ignored output directories | Parseable structured report; fixture/run attribution; screenshot and trace retained according to configured policy; artifact paths exist | P0 | Browser reporter + filesystem checks |
| BR-14 | INF-EXIT: meaningful automation | Healthy fixture versus deliberately broken fixture | Healthy run exits zero; detected failure exits nonzero; reporter lists failure rather than presenting a successful product test | P0 | CLI integration |
| BR-15 | INF-ISOLATION: independent cases | Test A writes cookie/local storage; test B opens fixture in a new context | B sees clean state; order and retries do not hide failures | P1 | Browser |
| BR-16 | INF-SAFETY: observation only | Fixture includes form, upload input, destructive-action button and download link | Generic crawl does not submit forms, upload files, click action buttons or execute downloads; navigation-only limits documented | P0 | Browser |
| BR-17 | INF-BASE: URL resolution | Relative links, normalized path segments, same-page query and hostile external base tag | Resolve using browser URL semantics; enforce allowed origin after resolution; no bypass through base tag | P1 | URL unit + browser |
| BR-18 | INF-LIMITS: honest coverage | Local page opens popup, service worker or WebSocket | Either supported blocking/cleanup is tested, or limitation is documented; ordinary route interception alone is not claimed as system-wide zero-egress proof | P1 | Browser; future hardening |
| BR-19 | INF-RESOURCE-HTTP: page success cannot hide resource failure | Local HTML returns 200 but same-origin script returns 404; count script requests | Page status remains 200; resource failure records URL/status and ideally type; report fails; counter confirms request reached local fixture | P0 | Browser regression |
| BR-20 | INF-RESOURCE-TIMEOUT: asynchronous failures are bounded | HTML returns 200 and starts same-origin fetch whose server never responds; short explicit timeout | Crawl completes within configured observation/fetch budget plus bounded teardown; report fails with request timeout or outstanding-request diagnostic; no silent success after page load | P0 | Browser regression |
| BR-21 | INF-RESOURCE-SUCCESS: avoid false failures | Local HTML, script and immediate fetch all return successful responses | All requested resources complete; report succeeds; no resource/timeout errors | P0 | Browser positive control |
| BR-22 | INF-TRACE-FAILURE: evidence failure prevents success | Healthy local page; make trace destination unwritable or inject trace-stop failure | `traceError` recorded; `ok=false`; report JSON persists if report destination remains writable; browser cleanup executes | P0 | Browser or focused fault-injection integration |

## Evidence and product handoff

Store command, tool/browser versions, exit status, report path and run scope together. Distinguish planned, implemented, passed, failed and skipped cases. Screenshots and traces prove what the fixture browser saw; they do not establish memory stability, production egress controls, ML accuracy, or full user-flow coverage.

BR-19–BR-22 were added after review identified false success when the main document returns HTTP 200 but its same-origin resources fail. Observe requests during the documented finite crawl window; do not claim to detect arbitrary future timers, polling failures after that window, or complete application behavior. Preserve a healthy-resource control so error handling does not turn every dynamic page into a failure. Servers and browser contexts must close in failure paths; a completed test alone does not establish memory-leak freedom.

When an AVA frontend exists, add separate requirement-linked acceptance tests for FR-U1–FR-U8 and relevant API requirements using actual accessible controls and test backends. Keep this fixture suite to test the automation itself. The future product suite must cover the real limitations text, live events, timeline and batch flow rather than treating this fixture as their implementation. BO-5/FR-O3 also require system/network-level verification outside this crawler.

## Implemented coverage map

Source inspection and the browser agent's follow-up for [foundation.spec.cjs](../../tests/browser/foundation.spec.cjs) map the current ten executable tests to the design. Combined IDs `QA-FND-001/002` count as one Playwright test. This map does not imply every variation in a design row is covered.

| Executable ID | Design coverage | Limits of asserted evidence |
|---|---|---|
| QA-FND-001/002 | BR-02, BR-03, BR-13 | Asserts cycle/fragment deduplication, max-pages truncation, structured report kind, nonempty screenshot and trace; does not check fragment targets |
| QA-FND-003 | BR-05, BR-06, BR-07 | Alternate port/hostname links skipped; image/fetch/redirect blocked; controlled second local server receives zero requests; not every scheme/resource type is exercised |
| QA-FND-004 | BR-08, BR-10 (partial) | Distinct 404/500 responses and page exception; does not directly assert a custom console-error event |
| QA-FND-005 | BR-09, BR-12 (partial), BR-13 (partial) | Timeout, failed report and finished timestamp; finally blocks close fixtures; no active-handle, repeated-port-reuse or memory-leak assertion |
| QA-FND-006 | BR-04 (partial), BR-03 (validation) | Explicit matching origin, invalid scheme, invalid page count and nonfinite timeout rejection; consult current test for additional root restrictions |
| QA-FND-007 | BR-16 (partial), BR-18 (partial) | Scripted POST and WebSocket attempts blocked with zero server hit counters; does not cover every interactive control, popup or service-worker variation |
| QA-FND-008 | BR-19 | HTML remains 200 with no page-level exception; script hit count is one; crawl-level error identifies script URL/type/status 404; report fails |
| QA-FND-009 | BR-20 | Same-origin fetch endpoints disconnect or never respond; bounded route timeout; both failures retained in crawl errors and persisted report; no claim about requests initiated after the observation window |
| QA-FND-010 | BR-22 | Directory at `trace.zip` induces actual trace write failure; `traceError` recorded and report fails while JSON is retained; does not establish leak freedom |
| QA-FND-011 | BR-21 | Successful local script and fetch hit counters; empty crawl errors and successful report |

Other cases remain planned unless additional executable evidence is linked. In particular, a cleanup code path and a passing timeout test do not prove the absence of resource leaks. The crawler conservatively blocks all redirects, including same-origin redirects; missing-fragment validation is outside its current scope.

The browser foundation agent reported all **ten tests passed in 9.9 seconds** using Chromium and `@playwright/test` 1.63.0 on 2026-10-02 after the resource-error fixes. Its implementation now drains in-flight route handlers before serializing the report, records resource failures at crawl level and treats trace failure as unsuccessful execution. This is that agent's execution evidence, not a second run by the test-design agent. Consult the [browser tooling README](../../scripts/browser/README.md) and latest generated report for exact run commands and artifacts. IN-01–IN-21 execution is separately recorded in [intake-test-cases.md](intake-test-cases.md).
