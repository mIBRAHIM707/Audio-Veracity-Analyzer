# Acceptance separation: complete software versus data-dependent and unmeasured

Backlog 11.QA. Claude-authored, 22 November 2026, at commit adaaf2f on
`feat/4-daily-development`. This document answers one question for every acceptance
criterion in the canonical PRD (`_bmad-output/planning-artifacts/prd.md`): is the
software for it finished, and if not, what stands in the way? It covers the §7
acceptance evidence per objective (BO-1 to BO-8, RQ-A to RQ-C), every §6
non-functional item, and every open box in `docs/context/daily-backlog.md`. The
per-requirement view of the §5 functional requirements is
`docs/qa/requirements-evidence-audit.md`; this document does not repeat it.

Nothing here is a model accuracy or performance result. Every model number recorded
so far comes from an untrained head or from the uncalibrated pretrained baseline, on a
development laptop rather than reference box A1. Lab tests show that the plumbing
works (calls are tapped, scored, stored and shown), not that AVA detects deepfakes.

`scripts/check_acceptance_separation.py` (run in CI by
`tests/unit/test_acceptance_separation.py`) keeps this file honest against its sources:
every §7 objective and §6 item has exactly one row whose quoted text still starts the
PRD item, every open backlog box has exactly one row and no closed box has one, every
status is one of the values below, every cited path exists, every `met` row cites a
test and every other row names what remains, and the summary counts match. It also
rejects placeholders, `met` notes that admit an unmet part, and unreadable backlog
boxes. Rows are tied to the PRD by their opening words only, so an edit to the end of
a PRD item, to a target value or to an objective's evidence text does not force a
revisit: re-read the affected rows whenever PRD §6 or §7 changes. Update a row in the
same change that alters its evidence or closes its backlog box.

## Status values

A row takes the first status in this list that applies, so `software-gap` means there
is work the daily runner could do now, without data, a decision or a person:

- `met`: the criterion as the PRD words it is satisfied by software and tests in this
  repository. Infrastructure evidence only, never a claim about detection quality.
- `software-gap`: software or a test is still missing and needs no participant data,
  trained model, decision or person.
- `decision`: the next step needs a PRD, architecture, scope or operator decision.
- `unmeasured`: the software exists; the number must be measured on reference box A1
  with the trained INT8 model.
- `data-dependent`: the software that can be built without data is built; what remains
  is a run, a measurement or a result that needs the dataset or the trained model.
- `human`: needs a person, an institution or a repository-settings change outside the
  runner (recording the demo, recruiting, supervisor review, CI workflow edits).

Backlog rows instead list what holds the box open, one or more of `software`,
`decision`, `data`, `human` and `deferred` (P2 work without a scope decision).

## Summary

Objectives and non-functional rows together (11 + 29 = 40 criteria):

| Status | Rows |
|---|---|
| met | 4 |
| software-gap | 14 |
| decision | 1 |
| unmeasured | 6 |
| data-dependent | 11 |
| human | 4 |

What this means:

- **Complete software.** The four criteria marked `met` below (6.3.4, 6.3.6, 6.3.7,
  6.6.2), and the 25 functional requirements marked `implemented` in the audit, are
  finished as software. They still need the trained model to be shown working as a
  detector.
- **Software still to build without data** (the `software-gap` rows and the `software`
  holds), roughly in order of PRD weight:
  - security: TLS between the dashboard and the API; the outside-name DNS gap in the
    k3s egress policy; a check that images hold no credentials and a decision on the
    snippet key held on a volume; setting reloads written to the audit log as
    `actor=system`;
  - evaluation: a bootstrap interval for EER differences (§6.2 gap targets), the
    FR-D5 `code_switched` field and rule, and a live-lab campaign driver (6.2.6);
  - operations and performance: monitoring on k3s; batched file jobs; a per-worker
    memory measurement; a browser-side event latency check; MLflow tracking (unit
    coverage is measured at 95%, see 6.5.2; keeping it needs a CI gate, a human edit);
  - the backlog items listed under "Open backlog tasks" (9.QA has not started), and
    issue #28 (AMR augmentation waveform SNR about 3 dB), which is investigation work
    outside any open box.
- **Data-dependent or unmeasured.** Most of §6.2, BO-1, BO-2, RQ-A, RQ-B, and the §6.1
  latency, cadence and concurrency figures. Their harnesses exist
  (`models/evaluation/metrics.py`, `models/export/benchmark_onnx.py`); the numbers need
  `ava-data-v1`, the trained head and box A1.
- **Decision.** Resuming a call's scoring session after a live restart (6.5.1, FR-I11).
- **Human.** The recorded BO-4/BO-8 demo, the listening study (RQ-C), the usability
  glance test, and the CI workflow edits that would move the lab, dashboard and egress
  tests into CI.

## Objectives

PRD §7. Evidence lists files only; the Remaining column says what is left.

| ID | Status | Evidence | Remaining |
|---|---|---|---|
| BO-1 | data-dependent | `models/evaluation/metrics.py`, `models/evaluation/report.py`, `models/baselines/rawnet2.py`, `models/baselines/aasist.py`, `tests/unit/test_eval_metrics.py`, `tests/model/test_eval_report.py` | The evaluation report on real scores for AVA, AASIST, RawNet2 and XLS-R+AASIST; the XLS-R+AASIST adapter waits for XLS-R weights, which the runner cannot provision (no write access to `D:/AVA-Data`). |
| BO-2 | data-dependent | `data/manifest/propose_splits.py`, `data/manifest/validate_splits.py`, `tests/unit/test_split_proposals.py` | The corpus itself (generation per D5, 60 to 80 recorded speakers by the team) and the datasheet (FR-D10); the speaker-overlap check runs today on the public pilots only. |
| BO-3 | unmeasured | `models/export/benchmark_onnx.py`, `models/ava/benchmark_cpu.py`, `tests/unit/test_benchmark_onnx.py`, `tests/model/test_benchmark_cpu.py` | A benchmark log on box A1 with the trained INT8 model and the CPU named; the laptop proxy (dynamic INT8 median 0.43 s per 4 s window, untrained head) is not §6.1 evidence. |
| BO-4 | human | `tests/lab/test_tap_end_to_end.py`, `tests/lab/test_compose_demo_lab.py`, `docs/operations/demo.md` | Recording the demo (a person); the end-to-end test runs in the Docker lab, not in CI (a human workflow edit); the live verdict comes from an untrained model until training. |
| BO-5 | software-gap | `deploy/k8s/base/networkpolicy.yaml`, `tests/unit/test_k8s_manifests.py`, `tests/lab/test_k3s_stack_lab.py`, `tests/lab/test_compose_demo_lab.py` | CoreDNS still forwards outside names (FR-O3); the egress tests are lab tests, not CI (human); the §6.4 alignment table needs the supervisor's review (human). |
| BO-6 | software-gap | `models/evaluation/metrics.py`, `tests/unit/test_eval_metrics.py` | "Within the §6.2 gap" is judged on a bootstrap interval of the Urdu minus Punjabi difference, which is not built (`bootstrap_ci` covers one set only); then the per-language EER and per-dialect FAR tables need the dialect sets and real scores (the FR-M10 floor rule is built). |
| BO-7 | software-gap | `deploy/k8s/base/kustomization.yaml`, `deploy/compose/compose.yaml`, `tests/lab/test_k3s_stack_lab.py`, `tests/lab/test_compose_stack_lab.py` | The k3s stack lacks the monitoring services that Compose has and an Ingress with TLS for the dashboard; the lab runs k3s in Docker, not on a clean physical node. |
| BO-8 | human | `dashboard/src/LiveBoard.tsx`, `dashboard/src/Timeline.tsx`, `dashboard/src/App.tsx`, `tests/browser/dashboard-live.spec.cjs`, `tests/browser/dashboard-upload.spec.cjs`, `tests/lab/test_compose_demo_lab.py` | Showing the live board, timeline, alert feed and batch upload in the recorded demo (a person); the rehearsal runs on the real stack. |
| RQ-A | data-dependent | `models/evaluation/metrics.py`, `tests/unit/test_eval_metrics.py` | The time-versus-accuracy curve per codec needs real scores (FR-M9); the curve builder with scorer-EMA parity is built. |
| RQ-B | data-dependent | `models/evaluation/metrics.py`, `tests/unit/test_eval_metrics.py` | The per-dialect FAR table needs dialect sets and real scores (FR-M10). |
| RQ-C | human | `data/review/build_review.py`, `tests/unit/test_review_builder.py` | The listening study (FR-S1, 30 participants) or a documented reason it was not run; the review page is QA tooling, not the study. |

## Non-functional requirements

PRD §6.1, §6.2, §6.3, §6.5 and §6.6, numbered in PRD order (6.3.2 is the second bullet
of §6.3). §6.4 is the regulatory alignment table; it is accepted through BO-5.

| Ref | PRD text | Status | Evidence | Remaining |
|---|---|---|---|---|
| 6.1.1 | Per-window inference latency | unmeasured | `models/export/benchmark_onnx.py`, `tests/unit/test_benchmark_onnx.py` | p95 of at least 20 timings on box A1 with the trained INT8 model, 4 threads, batch 1. |
| 6.1.2 | RTF per call | unmeasured | `models/export/benchmark_onnx.py`, `tests/unit/test_benchmark_onnx.py` | Same run as 6.1.1; RTF is derived only at batch 1. |
| 6.1.3 | Time to first provisional verdict | unmeasured | `services/scorer/speech_windows.py`, `tests/unit/test_speech_windows.py`, `tests/unit/test_live_stage_latency.py` | Speech onset to first `score.updated` on box A1 with the trained model; the speech-window rule and per-stage latency histograms are built. |
| 6.1.4 | Verdict update cadence | unmeasured | `services/scorer/speech_windows.py`, `tests/unit/test_speech_windows.py` | The 2 s speech-window rule is built (a constant hop, see FR-I4 in the audit); whether box A1 keeps that cadence under concurrent load with the trained model is not measured. |
| 6.1.5 | Concurrent calls per reference box | unmeasured | `services/inference/capacity.py`, `tests/unit/test_capacity.py`, `tests/lab/test_compose_stack_lab.py` | Concurrency on box A1 with the trained INT8 model; the lab soak (4 concurrent calls, 10 rounds) shows no lost windows or growth, not capacity. |
| 6.1.6 | Batch: 60 s file end-to-end | software-gap | `services/inference/analysis.py`, `tests/unit/test_analysis_service.py` | File jobs do not go through the micro-batcher, so batch size 8 with 16 threads is not built (FR-I9); then a measurement on box A1. |
| 6.1.7 | Memory per inference worker | software-gap | none | No instrument records a serving worker's RSS (`models/export/benchmark_onnx.py` times only; the lab soak samples memory growth, not a per-worker figure); then a run on box A1 with the INT8 model. The only figure, 2.772 GB sampled peak, is the FP32 PyTorch baseline smoke including loading. |
| 6.1.8 | Dashboard event latency | software-gap | `services/api/app.py`, `tests/unit/test_api_ws_delivery.py` | `ava_ws_delivery_seconds` times publish to WebSocket hand-off (one lab run, mean 151 ms); emit-to-render in the browser is not measured, which needs no data. |
| 6.2.1 | EER on the full held-out telephony test set | data-dependent | `models/evaluation/metrics.py`, `tests/unit/test_eval_metrics.py` | Real scores from the trained model on `ava-data-v1` with bootstrap intervals. |
| 6.2.2 | EER on the held-out-generator partition | data-dependent | `models/evaluation/metrics.py`, `tests/unit/test_eval_metrics.py` | The held-out-generator partition (FR-D9) and real scores. |
| 6.2.3 | EER on the code-switched subset | software-gap | `models/evaluation/metrics.py`, `tests/unit/test_eval_metrics.py` | The FR-D5 `code_switched` manifest field and rule, which the PRD specifies fully, and a bootstrap interval of the subset minus full-set difference are not built; then the subset and real scores. |
| 6.2.4 | EER gap between Urdu and Punjabi | software-gap | `models/evaluation/report.py`, `tests/model/test_eval_report.py` | A bootstrap interval of the Urdu minus Punjabi EER difference is not built (§6.2 judges gaps on it); then per-language scores from the trained model. |
| 6.2.5 | AVA versus best baseline | data-dependent | `models/baselines/rawnet2.py`, `models/baselines/aasist.py`, `tests/model/test_rawnet2.py`, `tests/model/test_aasist.py` | Training AVA and all three baselines on the same data; XLS-R+AASIST also needs XLS-R weights provisioned. |
| 6.2.6 | False alert rate at the operating point in the live lab | software-gap | `tests/lab/test_tap_end_to_end.py` | The trained model and a live campaign of at least 100 genuine and 100 synthetic calls per live codec (PRD task 5.3). First, a campaign driver that places N lab calls per codec and counts alerts is not built; it can be built and tested now with the untrained model, though its numbers mean nothing until the synthetic call set and the trained model exist. |
| 6.2.7 | Per-dialect FAR (RQ-B) | data-dependent | `models/evaluation/metrics.py`, `tests/unit/test_eval_metrics.py` | Dialect sets meeting the FR-M10 floor and real scores. |
| 6.2.8 | Expected Calibration Error on dev after temperature scaling | data-dependent | `models/evaluation/metrics.py`, `tests/unit/test_eval_metrics.py` | The dev set and the trained model; serving also refuses calibrated cards until it applies the temperature (FR-M3). |
| 6.3.1 | No network egress from inference, ingestion or storage services | software-gap | `deploy/k8s/base/networkpolicy.yaml`, `tests/unit/test_k8s_manifests.py`, `tests/lab/test_k3s_stack_lab.py`, `tests/lab/test_compose_stack_lab.py` | CoreDNS forwards outside names; the blocked-network tests run in the lab, not in CI (a human workflow edit). |
| 6.3.2 | TLS between dashboard and API | software-gap | none | No TLS anywhere between browser and API: the k3s base has no Ingress and the Compose demo serves plain HTTP on 127.0.0.1. Local accounts exist since 19 November (FR-U7), so this no longer waits on an authentication decision. SIP-TLS and SRTP are P2 with FR-T6. |
| 6.3.3 | Secrets via Kubernetes Secrets; no credentials in images | software-gap | `deploy/k8s/base/api.yaml`, `deploy/k8s/base/live.yaml`, `tests/unit/test_k8s_manifests.py`, `tests/lab/test_k3s_stack_lab.py` | Passwords come from `secretKeyRef` and no Secret object is committed, but no test shows that the images hold no credentials (a build-context or layer check), and the FR-O5 snippet key is generated onto a volume claim by an init container rather than held in a Secret (keep it as a recorded deviation or move it). Most of `tests/unit/test_k8s_manifests.py` skips in CI, which lacks PyYAML (a human workflow edit). |
| 6.3.4 | Audio is processed in memory and discarded by default | met | `services/retention/snippets.py`, `tests/unit/test_api_no_audio_retention.py`, `tests/unit/test_snippet_retention.py`, `tests/lab/test_snippet_retention_lab.py` | none (key rotation and switching retention without a live restart belong to FR-O5) |
| 6.3.5 | Role-based access and an audit log | software-gap | `services/auth/service.py`, `services/store/migrations/0003_local_auth.sql`, `tests/unit/test_local_auth.py` | Logins, feedback, CSV exports and snippet playback are audited. Setting changes made by file reload are never written to `audit_log`, although the PRD settles the file-only case (`actor=system`); the admin settings page (FR-U7) is also unbuilt. |
| 6.3.6 | No third-party analytics, fonts or telemetry | met | `tests/browser/dashboard-recovery.spec.cjs`, `tests/unit/test_ort_runtime.py`, `tests/unit/test_monitoring_dashboard.py`, `tests/lab/test_compose_stack_lab.py` | none (the dashboard makes no off-origin request, ONNX Runtime telemetry is forced off, Grafana's call-home settings are off, and the Compose containers cannot reach the internet; the dashboard Playwright project runs locally, outside CI) |
| 6.3.7 | Raw audio, consent forms and speaker-level manifests never enter a repository | met | `scripts/repository_guard.py`, `tests/unit/test_repository_guard_captures.py` | none (FR-E6) |
| 6.5.1 | Any single service restart must not lose in-progress call state | decision | `services/store/events_pg.py`, `tests/unit/test_store_postgres.py`, `tests/lab/test_compose_stack_lab.py` | Scores flush every hop and survive restarts of live, PostgreSQL, Redis and the API in the lab, but after a live restart the call continues as a new session with fresh EMA and alert state; resuming the same session needs the scorer topology decision (FR-I11). |
| 6.5.2 | Code coverage ≥ 70% on ingestion, scoring and API modules | data-dependent | `requirements-test.txt`, `tests/lab/test_tap_end_to_end.py` | Coverage measured 22 Nov (coverage.py 7.10.7, unit suite on Windows with the API, store, auth, Redis and retention packages; 98 tests skipped): 95% of lines in `services/tap`, `services/scorer` and `services/api` together (tap 88-99% per module, scorer 95-100%, `services/api/app.py` 94%, `services/api/__main__.py` 78%); "ingestion" is read as the tap, and the neighbouring `services/live`, `services/events` and `services/store` together reach 83%. Command and output are in the 22 Nov daily report. With only `requirements-test.txt` the API tests skip and the API reads 1%, so the measurement needs those packages. No CI gate keeps it (a human workflow edit). The second half, an end-to-end test that plays a known synthetic file through Asterisk and asserts an alert, and a genuine one and asserts none, needs the trained model. |
| 6.5.3 | Reproducible training: pinned dependencies, seeded runs | software-gap | `models/ava/train.py`, `requirements-model.txt`, `tests/model/test_ava_sls.py` | Local MLflow tracking is not built; hashed lock files need a human (`uv pip compile` is outside the runner's allowlist). |
| 6.5.4 | Reproducible dataset: all collection, generation and processing scripts | data-dependent | `data/collect/csalt_pilot.py`, `data/prepare/prepare_audio.py`, `data/manifest/propose_splits.py`, `tests/unit/test_csalt_pilot.py`, `tests/unit/test_audio_preparation.py` | Generation scripts (FR-D2, FR-D3, FR-D9) need the generators and compute; a rebuild from sources and the manifest can be shown only once the dataset exists. |
| 6.6.1 | An analyst can identify a flagged call and its confidence within 3 seconds | human | `dashboard/src/LiveBoard.tsx`, `tests/browser/dashboard-live.spec.cjs` | A timed glance test with analysts. |
| 6.6.2 | The dashboard renders correctly at 1366×768 and 1920×1080 | met | `dashboard/src/App.tsx`, `dashboard/src/LiveBoard.tsx`, `tests/browser/dashboard-viewport.spec.cjs` | none (added by this QA: at both sizes the live board with eight calls, a batch result and the history page without a store scroll no wider than the window, keep every tested element inside it with no clipped text, and show the alert banner and feed item on the first screen; VP00 shows the check catches overflow and clipping; history with stored rows, sign-in, feedback and snippet views were left out at these sizes, and the dashboard project runs locally, outside CI) |

## Open backlog tasks

Every unchecked box in `docs/context/daily-backlog.md` and what holds it open.

| Task | Held by | Remaining |
|---|---|---|
| 3.1 | decision | The speaker-turn boundary contract, which neither the PRD nor the architecture defines. |
| 3.4 | human, software, decision | Noise corpora and SLR28 RIRs need provisioning in `D:/AVA-Data` (no runner write access), then RIR preparation (software); handset versus speakerphone labelling is a PRD question. The VoIP codec is done (FR-D6 asks for Opus or G.729; Opus is built). |
| 3.QA | decision | Closes with 3.1 and 3.4; 3.QA M1 (8 kHz batch rule and normalization unit) awaits a decision. |
| 5.1 | decision | Scorer resume from persisted state (tap and scorer share one process) and a durable job table. |
| 5.2 | decision | Job cancellation and idempotency, which the PRD does not ask for. |
| 5.3 | decision | Restart recovery of in-flight jobs (needs durable jobs, 5.1) and snippet-key rotation (operator decision). |
| 5.QA | decision | No restart gap is known; scorer resume stays the 5.1 topology decision. |
| 6.1 | human | A real softphone application, which needs a person and a published port. |
| 6.2 | decision | The agent direction on the tap path (FR-I8) needs a "human agent answered" event design. |
| 6.3 | deferred, decision | Live AMR over RTP (P2, no open Asterisk codec); whether skipped holes count as FR-I2 loss. |
| 6.QA | data | End-to-end alerts need the trained model; all live codecs already run end to end with the untrained one. |
| 7.3 | decision, software | FR-A5 language identification needs a model choice; the optional FR-A9 settings endpoint (admin) is software. |
| 7.QA | human, software | The dashboard Playwright project in CI (a human workflow edit) and a catalogue review of the browser cases. |
| 8.1 | software, data | File jobs through the micro-batcher (software); measured capacity on box A1 (8.QA). |
| 8.2 | data, decision | Running the stride and unmonitored path with the trained model; the PRD to confirm "behind" and EMA per window under stride. |
| 8.QA | data | Box A1 with the trained model: concurrency, load and RSS per worker. |
| 9.2 | human | XLS-R + AASIST needs XLS-R weights in `D:/AVA-Data`. |
| 9.3 | data, decision | Running the report on real scores, actDCF (needs LLR scores); speaker-clustered intervals and the EER method need a decision. |
| 9.QA | software | Not started: an independent review of the known-answer metric tests, insufficient-data cases and training smoke evidence; needs no data. |
| 10.1 | data | The AVA-trained head, parity on real dev windows, INT8 latency and dev EER with the batching policy. |
| 10.2 | software, human | CoreDNS outside names, the dashboard Ingress with TLS (no longer waiting on authentication, built 19 November), monitoring on k3s and the PBX network path (software); hashed lock files and an SBOM CI job (human). |
| 10.QA | software, data | Image upgrade and rollback with a database restore, and the same on k3s (software); CPU parity with the trained head (data). |
| 11.1 | software, decision | The ARI 422 join retry seen working on the stack, a longer soak, switching retention without a live restart, a snippet backup test and FR-U5 playback in the stacks (software); snippet-key rotation needs the operator decision named under 5.3. |
| 11.2 | human | The recorded BO-4 demo video. |
