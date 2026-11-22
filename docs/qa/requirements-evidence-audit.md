# Requirements and evidence audit

Backlog 11.2 (first part). Claude-authored, 22 November 2026, at commit 91faaa6 on
`feat/4-daily-development`. One row per functional requirement in the canonical PRD
(`_bmad-output/planning-artifacts/prd.md` §5). It records what the repository holds as
evidence today, not what the PRD promises. Nothing here is a model accuracy claim:
every model number so far comes from an untrained head or the uncalibrated pretrained
baseline.

`tests/unit/test_requirements_audit.py` checks this file against the PRD: every PRD ID
appears exactly once with the PRD's priority, every status is one of the values below,
every cited path exists, every row that is not `implemented` names its gap, every
`implemented` or `partial` row cites a test, and the summary counts match the rows.
Update a row in the same change that alters the evidence.

## Status values

- `implemented`: the software the requirement asks for is built and tested in this
  repository (unit, browser, model or lab tests). Infrastructure evidence only.
- `partial`: some of it is built and tested; the gap column names what is missing.
- `data-dependent`: the software that can be built without the participant dataset or
  the trained model is built; what remains is a run, a measurement or a result that
  needs them (backlog "Not autonomous acceptance").
- `not-started`: no software yet, and none of it depends on data.
- `human`: needs a person, an institution, a licence or a scope decision outside the
  daily runner (recruitment, approvals, workflow edits).
- `deferred`: P2 stretch work with no scope decision yet.

## Summary

| Status | Rows |
|---|---|
| implemented | 25 |
| partial | 32 |
| data-dependent | 14 |
| not-started | 2 |
| human | 8 |
| deferred | 2 |

83 PRD requirements in total. The P0 software gaps that need no data are listed under
"Findings" below.

## Dataset (FR-D)

| ID | P | Status | Code and documents | Tests | Gap |
|---|---|---|---|---|---|
| FR-D1 | P0 | data-dependent | `data/collect/csalt_pilot.py`, `data/collect/meta_pnb_pilot.py`, `data/collect/source_register.json` | `tests/unit/test_csalt_pilot.py`, `tests/pnb/test_meta_pnb_pilot.py` | Only CSALT and Meta pnb_Arab pilots are imported; Common Voice, UrduSpeech and Habib (Q8 access) and team recordings are not. |
| FR-D2 | P0 | data-dependent | `data/collect/csalt_synthetic.py` | `tests/unit/test_csalt_synthetic.py` | The CSALT synthetic partition is imported; generation with the D5 tools (about 12,000 clips per language) has not run. |
| FR-D3 | P0 | data-dependent | none | none | Seed-VC and RVC conversion needs permitted target voices and generation compute. |
| FR-D4 | P0 | human | `docs/data-collection/collection-plan.md`, `docs/data-collection/session-script.md`, `data/collect/validate_intake.py` | `tests/unit/test_intake.py` | Recruitment and recording of 60 to 80 speakers by the team; approval reference still to be recorded. |
| FR-D5 | P0 | not-started | none | none | No `code_switched` manifest field or rule; the evaluation side accepts a code-switched subset (`models/evaluation/metrics.py`). |
| FR-D6 | P0 | partial | `data/prepare/augment.py`, `data/prepare/codecs.py`, `data/prepare/ffmpeg_codecs.py` | `tests/unit/test_augment_chain.py`, `tests/unit/test_augment_codec_mix.py`, `tests/unit/test_ffmpeg_codecs.py`, `tests/unit/test_ffmpeg_opus.py`, `tests/unit/test_packet_loss.py` | G.711, GSM, AMR-NB and Opus with loss and jitter are built with per-clip lineage; G.729 has no free encoder; issue #28 (AMR waveform SNR about 3 dB) blocks AMR-augmented results; applying it to every clip is data work. |
| FR-D7 | P1 | partial | `data/prepare/noise.py`, `data/prepare/reverb.py` | `tests/unit/test_noise_mixing.py`, `tests/unit/test_noise_speech_active.py`, `tests/unit/test_reverb.py`, `tests/unit/test_augment_reverb.py` | Noise corpora and RIR sets are not provisioned (no write access to `D:/AVA-Data` from the runner); handset versus speakerphone labelling is a PRD question. |
| FR-D8 | P0 | partial | `data/manifest/propose_splits.py`, `data/manifest/validate_splits.py`, `data/manifest/migrate_policy.py` | `tests/unit/test_split_proposals.py`, `tests/unit/test_split_proposals_cases.py`, `tests/unit/test_migrate_policy.py` | Speaker-disjoint proposals and the overlap check run on pilots; partitions (b) to (d) need the held-out generator, dialect sets and phone-call set. |
| FR-D9 | P0 | data-dependent | none | none | Fish Audio S2 generation and the D4-checkpoint source check have not run. |
| FR-D10 | P0 | human | `data/collect/README.md` | none | No datasheet yet; it needs the dataset statistics and the approval reference. |
| FR-D11 | P0 | partial | `data/collect/source_register.json`, `data/manifest/propose_splits.py` | `tests/unit/test_split_proposals_cases.py`, `tests/unit/test_csalt_duplicates.py` | The permitted-pool rule is recorded and duplicate identities are annotated; enforcement on generated clips waits for generation (FR-D2). |
| FR-D12 | P0 | partial | `data/prepare/prepare_audio.py`, `data/prepare/conditioning.py`, `scripts/verify_prepared_output.py` | `tests/unit/test_audio_preparation.py`, `tests/unit/test_conditioning_parity.py`, `tests/unit/test_verify_prepared_output.py` | One preparation script for both classes, with per-clip metrics; the per-partition overlap report and the silence-only and duration-only leakage probes are not built. |
| FR-D13 | P0 | partial | `scripts/verify_prepared_output.py`, `data/review/build_review.py`, `data/collect/csalt_pilot.py` | `tests/unit/test_verify_prepared_output.py`, `tests/unit/test_review_builder.py`, `tests/browser/listening-review.spec.cjs` | Length, silence, loudness and listening-sample tooling exist; the noise score, language check and ASR check are not built. |
| FR-D14 | P0 | data-dependent | `data/manifest/README.md` | none | `ava-data-v0` and `ava-data-v1` releases need the generated and recorded data. |
| FR-D15 | P0 | human | none | none | Generator smoke test needs listeners and a named owner. |

## Ethics and data governance (FR-E)

| ID | P | Status | Code and documents | Tests | Gap |
|---|---|---|---|---|---|
| FR-E1 | P0 | human | `docs/data-collection/approval-pack.md` | none | Approval reported on 4 October 2026; its reference number is not yet recorded. |
| FR-E2 | P0 | human | `docs/data-collection/approval-pack.md`, `data/collect/validate_intake.py` | `tests/unit/test_intake.py` | Signed forms are held offline by the team; the intake check reads consent flags only. |
| FR-E3 | P0 | human | `.gitignore`, `scripts/repository_guard.py` | `tests/unit/test_repository_guard_captures.py` | Storage location and backup are an operator duty; the repository side is the guard (FR-E6). |
| FR-E4 | P0 | human | `models/export/model_card.py` | `tests/unit/test_model_card.py` | A release decision; nothing is published from this repository. |
| FR-E5 | P0 | partial | `data/collect/source_register.json`, `models/source_register.json`, `models/export/model_card.py` | `tests/unit/test_model_card.py`, `tests/unit/test_foundation_tools.py` | Registers cover corpora and model sources; generators and every tool, and the datasheet restriction, are not complete. |
| FR-E6 | P0 | implemented | `scripts/repository_guard.py`, `.githooks/pre-commit`, `scripts/install_hooks.py`, `.github/workflows/foundation.yml` | `tests/unit/test_repository_guard_captures.py`, `tests/unit/test_foundation_tools.py` | none |

## Model (FR-M)

| ID | P | Status | Code and documents | Tests | Gap |
|---|---|---|---|---|---|
| FR-M1 | P0 | data-dependent | `models/ava/backbone.py`, `models/export/checkpoint_parity.py` | `tests/model/test_ava_sls.py`, `tests/model/test_checkpoint_parity.py` | Strict truncation is built and run on the real checkpoint; the layer sweep and fine-tuning need `ava-data-v1`. |
| FR-M2 | P0 | data-dependent | `models/ava/sls.py`, `models/ava/train.py` | `tests/model/test_ava_sls.py` | SLS head and sweep harness built (tiny smoke runs); the sweep itself needs data. |
| FR-M3 | P0 | partial | `models/ava/model.py`, `services/live/service.py` | `tests/model/test_ava_sls.py`, `tests/unit/test_live_service.py` | Probability and 256-d embedding contract built; serving refuses calibrated cards until it applies the temperature. |
| FR-M4 | P0 | partial | `models/baselines/rawnet2.py`, `models/baselines/aasist.py`, `models/augmentation/rawboost.py` | `tests/model/test_rawnet2.py`, `tests/model/test_aasist.py`, `tests/unit/test_rawboost.py` | XLS-R + AASIST is missing (XLS-R weights not provisioned); training all three needs data. |
| FR-M5 | P0 | data-dependent | `models/evaluation/metrics.py`, `models/evaluation/report.py` | `tests/unit/test_eval_metrics.py`, `tests/unit/test_eval_asvspoof5_costs.py`, `tests/model/test_eval_report.py` | Metrics, bootstrap intervals and Track 1 costs built; the report needs real scores; speaker-clustered intervals and the EER method need a decision. |
| FR-M6 | P0 | data-dependent | `models/evaluation/metrics.py` | `tests/unit/test_eval_metrics.py` | Temperature fitting, ECE and reliability bins built; the dev set and the ECE target need data. |
| FR-M7 | P0 | data-dependent | `models/export/onnx_export.py`, `models/export/checkpoint_parity.py`, `models/export/benchmark_onnx.py` | `tests/model/test_onnx_export.py`, `tests/unit/test_onnx_export_rules.py`, `tests/unit/test_benchmark_onnx.py` | Export, 1e-3 parity, dynamic and static INT8 and the fallback rule are built; dev-EER re-measurement needs the trained head. |
| FR-M8 | P1 | data-dependent | `models/ava/model.py` | `tests/model/test_ava_sls.py` | Mean-pool head built; the ablation needs training. |
| FR-M9 | P0 | data-dependent | `models/evaluation/metrics.py` | `tests/unit/test_eval_metrics.py` | Evidence-length curve with scorer-EMA parity built; the curve and the chosen N need real scores. |
| FR-M10 | P0 | data-dependent | `models/evaluation/metrics.py` | `tests/unit/test_eval_metrics.py` | Per-dialect FAR with the 10-speaker and 500-clip rule built; dialect sets need data. |
| FR-M11 | P0 | partial | `models/ava/windows.py`, `models/evaluation/metrics.py` | `tests/model/test_ava_windows.py`, `tests/unit/test_eval_metrics.py` | One zero-padded window per clip is built; the selection of the live-call campaign and concatenated clips of at least 8 s for the minimum-evidence evaluation is left to the caller and not built; truncating clips over 4 s awaits a PRD decision. |
| FR-M12 | P0 | data-dependent | `models/export/model_card.py` | `tests/unit/test_model_card.py` | Card builder with `not_measured` metrics; training data, metrics and failure modes need the trained model. |

## Inference and scoring (FR-I)

| ID | P | Status | Code and documents | Tests | Gap |
|---|---|---|---|---|---|
| FR-I1 | P0 | partial | `services/tap/rtp_receiver.py`, `services/tap/tap_app.py`, `data/prepare/ffmpeg_codecs.py`, `lab/asterisk/config/pjsip.conf` | `tests/unit/test_tap_hostile_rtp.py`, `tests/lab/test_asterisk_lab.py`, `tests/lab/test_tap_end_to_end.py`, `tests/lab/test_amr_replay_lab.py` | G.711 and GSM callers reach the tap (Asterisk transcodes to mu-law); unsupported offers get 488; RTCP and DTMF are ignored. AMR-NB RFC 4867 payload decode from RTP captures is not built; issue #28. |
| FR-I2 | P0 | implemented | `services/tap/jitter.py`, `services/tap/scoring_sink.py`, `services/scorer/speech_windows.py`, `services/scorer/call.py` | `tests/unit/test_tap_jitter.py`, `tests/unit/test_concealment_gate.py`, `tests/unit/test_concealment_limit.py`, `tests/unit/test_tap_hostile_rtp.py`, `tests/unit/test_tap_scoring_sink.py` | none (whether skipped holes count as loss is a PRD question) |
| FR-I3 | P0 | partial | `services/tap/scoring_sink.py`, `services/inference/silero_vad.py`, `services/scorer/speech_windows.py`, `services/scorer/speech_feed.py` | `tests/unit/test_speech_windows.py`, `tests/unit/test_speech_feed.py`, `tests/unit/test_tap_scoring_sink.py`, `tests/model/test_silero_vad_onnx.py` | Resampling, VAD, speech-only windows, the 2.5 s rule and `no_speech` are built. A partial tail is marked only inside the scorer; published events do not carry `partial`. Resampling is a separate soxr stream proven equal to the file path by a parity test, not one shared `ingest` module. |
| FR-I4 | P0 | partial | `services/scorer/config.py`, `services/live/config_reload.py`, `models/export/model_card.py` | `tests/unit/test_scorer_config.py`, `tests/unit/test_live_config_reload.py`, `tests/unit/test_deploy_scoring_config.py` | Validation rules and reloads for new calls are built; live windowing advances by the constant 2 s `HOP_SAMPLES` whatever `audio.hop_s` says, and a hop change is refused as needing a restart, so the hop is not configurable in practice. |
| FR-I5 | P0 | implemented | `services/scorer/session.py`, `services/inference/analysis.py` | `tests/unit/test_scorer_session.py`, `tests/unit/test_analysis_service.py` | none (N is set from FR-M9 once real scores exist) |
| FR-I6 | P0 | partial | `services/scorer/publish.py`, `services/scorer/degraded.py` | `tests/unit/test_scorer_publish.py`, `tests/unit/test_scorer_degraded.py`, `tests/unit/test_live_stage_latency.py` | `score.updated` carries the degraded flag and stride; the 7.5 s target is not measured on the reference box (8.QA, trained model). |
| FR-I7 | P0 | implemented | `services/scorer/session.py`, `services/scorer/publish.py`, `services/store/events_pg.py` | `tests/unit/test_scorer_session.py`, `tests/unit/test_scorer_publish.py`, `tests/unit/test_store_postgres.py` | none |
| FR-I8 | P1 | partial | `services/scorer/call.py`, `services/tap/tap_app.py` | `tests/unit/test_scorer_call.py`, `tests/lab/test_ari_tap.py` | Caller-only tapping and the two-direction rule are built; the agent direction on the tap path (second snoop, "human answered" event) is not. |
| FR-I9 | P0 | partial | `services/inference/capacity.py`, `services/scorer/live.py`, `services/live/service.py` | `tests/unit/test_capacity.py`, `tests/unit/test_scorer_live.py`, `tests/unit/test_live_service.py` | Capacity refusal and the 25 % batch share are built; 8 concurrent calls are not measured on the reference box; file jobs do not go through the batcher. |
| FR-I10 | P1 | implemented | `services/scorer/degraded.py`, `services/scorer/pending.py`, `services/scorer/pipeline.py` | `tests/unit/test_scorer_degraded.py`, `tests/unit/test_scorer_pending.py`, `tests/unit/test_scorer_pipeline.py` | none (the 4x cap follows `hop_s`, which matches the windowing only at the default 2 s; see FR-I4) |
| FR-I11 | P0 | partial | `services/scorer/live.py`, `services/store/events_pg.py`, `services/store/pump.py` | `tests/unit/test_scorer_live.py`, `tests/unit/test_store_postgres.py`, `tests/unit/test_store_pump.py`, `tests/lab/test_compose_stack_lab.py` | Cleanup, late-result discard and per-hop flush are built; resume from persisted state needs a topology decision (tap and scorer share one process). |

## Telephony (FR-T)

| ID | P | Status | Code and documents | Tests | Gap |
|---|---|---|---|---|---|
| FR-T1 | P0 | partial | `lab/asterisk/Dockerfile`, `lab/asterisk/config/pjsip.conf`, `lab/asterisk/config/pjsip_softphone.conf`, `lab/asterisk/harness.py`, `lab/asterisk/rtp_pcap.py` | `tests/lab/test_asterisk_lab.py`, `tests/lab/test_bridged_call_lab.py`, `tests/lab/test_softphone_lab.py`, `tests/unit/test_lab_rtp_pcap.py` | Asterisk, SIPp endpoints, codec forcing and an authenticated softphone endpoint are built; a real softphone application needs a person and a published port. |
| FR-T2 | P0 | implemented | `services/tap/ari.py`, `services/tap/tap_app.py`, `services/tap/rtp_receiver.py` | `tests/unit/test_tap.py`, `tests/lab/test_ari_tap.py` | none |
| FR-T3 | P0 | partial | `services/tap/tap_app.py`, `services/store/history.py`, `dashboard/src/HistoryPage.tsx` | `tests/unit/test_call_codec.py`, `tests/unit/test_store_history.py`, `tests/browser/dashboard-history.spec.cjs` | Call ID, timestamps and codec reach the dashboard; from and to are read by the tap but not served (the store README holds caller numbers back pending a privacy and retention decision). |
| FR-T4 | P0 | implemented | `services/tap/tap_app.py`, `docs/operations/deployment-guide.md` | `tests/lab/test_ari_tap.py`, `tests/lab/test_detector_fault_lab.py`, `tests/lab/test_compose_stack_lab.py` | none (calls survive a stopped, hung, crashed or killed AVA in opt-in lab tests; the guide's section 3 requires "continue the call"; running the AVA-stopped case in CI, PRD task 4.7 and §6.5, needs a human workflow edit) |
| FR-T5 | P0 | partial | `services/tap/tap_app.py`, `services/tap/scoring_sink.py`, `services/scorer/live.py` | `tests/lab/test_ari_tap.py`, `tests/unit/test_tap_scoring_sink.py`, `tests/unit/test_scorer_live.py` | The tap releases its Asterisk resources within 5 s (lab), but the scoring sink keeps a pending end for up to `end_grace_s` = 10 s and no test times scoring-state removal; lower the grace or record a deviation (a code or PRD decision). |
| FR-T6 | P2 | deferred | none | none | SIPREC needs a scope decision. |
| FR-T7 | P0 | implemented | `services/tap/tap_app.py`, `lab/asterisk/config/extensions.conf` | `tests/lab/test_ari_tap.py` | none (condition not triggered: the snoop plus ExternalMedia tap works in the lab since 15 Nov, so no fallback was built) |

## API (FR-A)

| ID | P | Status | Code and documents | Tests | Gap |
|---|---|---|---|---|---|
| FR-A1 | P0 | partial | `services/api/app.py`, `services/inference/analysis.py` | `tests/unit/test_analysis_api.py`, `tests/unit/test_analysis_service.py`, `tests/browser/real-model-dashboard.spec.cjs` | WAV upload, 413, 422, stereo downmix, wideband flag and the 202 job split are built; MP3, AMR and GSM input, the 8 kHz path, language hint and codec detection for files are not. |
| FR-A2 | P0 | implemented | `services/api/app.py` | `tests/unit/test_analysis_api.py` | none (jobs are in memory only) |
| FR-A3 | P0 | implemented | `services/calls/registry.py`, `services/store/history.py`, `services/api/app.py` | `tests/unit/test_call_registry.py`, `tests/unit/test_api_calls_events.py`, `tests/unit/test_store_history.py` | none (stored history is served at `GET /v1/history/calls` and `GET /v1/calls/{id}/history` rather than inside `GET /v1/calls`) |
| FR-A4 | P0 | implemented | `services/events/contract.py`, `services/events/fanout.py`, `services/events/redis_bridge.py`, `services/events/relay_loop.py` | `tests/unit/test_event_fanout.py`, `tests/unit/test_api_calls_events.py`, `tests/unit/test_redis_bridge.py`, `tests/unit/test_event_relay_api.py` | none |
| FR-A5 | P1 | not-started | none | none | No language-identification stage; needs a model choice (offline, CPU, Urdu and Punjabi). |
| FR-A6 | P1 | implemented | `services/store/history.py`, `services/api/alert_csv.py` | `tests/unit/test_alert_search.py` | none ("confidence" is the uncalibrated peak EMA; PRD wording open) |
| FR-A7 | P1 | partial | `services/store/feedback.py`, `services/api/app.py`, `dashboard/src/AlertFeedback.tsx` | `tests/unit/test_alert_feedback.py`, `tests/browser/dashboard-feedback.spec.cjs` | Labels are stored with the window scores; embeddings are not stored. |
| FR-A8 | P0 | implemented | `services/api/app.py`, `services/live/health.py` | `tests/unit/test_api_readiness_metrics.py`, `tests/unit/test_live_health.py` | none |
| FR-A9 | P1 | partial | `services/live/config_reload.py`, `deploy/compose/config/scoring.json`, `deploy/k8s/base/scoring.json` | `tests/unit/test_live_config_reload.py`, `tests/unit/test_deploy_scoring_config.py` | File plus reload is built in Compose and k3s for thresholds and direction; window and hop changes are refused as needing a restart (see FR-I4); the optional endpoint (admin, with accounts) is not. |
| FR-A10 | P2 | deferred | none | none | Webhook needs a scope decision. |
| FR-A11 | P0 | partial | `services/api/app.py` | `tests/unit/test_analysis_api.py` | `/openapi.json` is served; `/docs` stays off because Swagger UI loads CDN assets (a recorded deviation; bundling the assets is not done). |

## Dashboard (FR-U)

| ID | P | Status | Code and documents | Tests | Gap |
|---|---|---|---|---|---|
| FR-U1 | P0 | implemented | `dashboard/src/LiveBoard.tsx`, `dashboard/src/live.ts` | `tests/browser/dashboard-live.spec.cjs`, `tests/browser/live-state.spec.ts`, `tests/browser/dashboard-recovery.spec.cjs` | none |
| FR-U2 | P0 | implemented | `dashboard/src/LiveBoard.tsx` | `tests/browser/dashboard-live.spec.cjs` | none |
| FR-U3 | P0 | implemented | `dashboard/src/Timeline.tsx` | `tests/browser/dashboard-upload.spec.cjs`, `tests/browser/dashboard-live.spec.cjs` | none |
| FR-U4 | P0 | implemented | `dashboard/src/App.tsx`, `dashboard/src/api.ts` | `tests/browser/dashboard-upload.spec.cjs`, `tests/browser/real-model-dashboard.spec.cjs` | none |
| FR-U5 | P1 | partial | `dashboard/src/AlertSnippet.tsx`, `services/retention/playback.py` | `tests/browser/dashboard-snippet.spec.cjs`, `tests/unit/test_snippet_playback.py` | Codec and playback are built; playback is not wired in Compose or k3s (key and `cryptography` in the API image). |
| FR-U6 | P1 | implemented | `dashboard/src/HistoryPage.tsx`, `dashboard/src/history.ts` | `tests/browser/dashboard-history.spec.cjs`, `tests/browser/history-state.spec.ts` | none (times shown in UTC only) |
| FR-U7 | P1 | partial | `services/auth/service.py`, `services/auth/store.py`, `dashboard/src/AuthGate.tsx` | `tests/unit/test_local_auth.py`, `tests/browser/dashboard-auth.spec.cjs`, `tests/browser/dashboard-auth-recovery.spec.cjs` | Login, both roles and the audit log are built; the admin settings page for thresholds and retention is not. |
| FR-U8 | P0 | implemented | `dashboard/src/main.tsx`, `services/api/app.py` | `tests/browser/dashboard-recovery.spec.cjs` | none |

## Listening study (FR-S)

| ID | P | Status | Code and documents | Tests | Gap |
|---|---|---|---|---|---|
| FR-S1 | P1 | human | `data/review/build_review.py` | `tests/unit/test_review_builder.py` | Study with 30 participants, protocol and stimulus set; the review page is QA tooling, not the study. |

## Operations (FR-O)

| ID | P | Status | Code and documents | Tests | Gap |
|---|---|---|---|---|---|
| FR-O1 | P0 | implemented | `deploy/live/Dockerfile`, `deploy/api/Dockerfile`, `deploy/compose/compose.yaml` | `tests/unit/test_deploy_images.py`, `tests/lab/test_compose_stack_lab.py` | none |
| FR-O2 | P0 | implemented | `deploy/k8s/base/kustomization.yaml`, `deploy/k8s/base/live.yaml`, `deploy/k8s/base/api.yaml` | `tests/unit/test_k8s_manifests.py`, `tests/lab/test_k3s_stack_lab.py` | none |
| FR-O3 | P0 | partial | `deploy/k8s/base/networkpolicy.yaml` | `tests/unit/test_k8s_manifests.py`, `tests/lab/test_k3s_stack_lab.py` | Default-deny egress is built and probed in the k3s lab; CoreDNS still forwards outside names; the blocked-network run is a lab test, not CI (workflow edit is human). |
| FR-O4 | P0 | partial | `services/live/service.py`, `models/export/model_card.py`, `services/inference/baseline.py` | `tests/unit/test_live_service.py`, `tests/unit/test_model_card.py`, `tests/lab/test_compose_stack_lab.py` | The live service refuses a missing or disagreeing model or card (lab-tested); the analysis API loads its hash-pinned baseline checkpoint without a model card check. |
| FR-O5 | P0 | partial | `services/retention/snippets.py`, `services/live/retention.py` | `tests/unit/test_snippet_retention.py`, `tests/unit/test_snippet_aes_gcm.py`, `tests/unit/test_live_retention.py`, `tests/unit/test_api_no_audio_retention.py`, `tests/lab/test_snippet_retention_lab.py` | Default off, ring buffer, one sealed snippet per alert, AES-256-GCM, TTL and purge are built in the live process; the switch is a command-line flag (a change needs a restart) and Compose and k3s do not expose it; key provisioning and rotation are an operator procedure. |
| FR-O6 | P1 | implemented | `services/live/eventlog.py`, `services/api/oplog.py`, `services/scorer/metrics_text.py`, `deploy/compose/monitoring/grafana/dashboards/ava-overview.json` | `tests/unit/test_live_eventlog.py`, `tests/unit/test_api_oplog.py`, `tests/unit/test_monitoring_dashboard.py`, `tests/unit/test_api_metrics_fr_o6.py` | none |
| FR-O7 | P1 | partial | `scripts/image_sbom.py`, `deploy/live/Dockerfile`, `deploy/api/Dockerfile` | `tests/unit/test_image_sbom.py`, `tests/unit/test_deploy_images.py`, `tests/lab/test_image_sbom_lab.py` | Digest pins and the SBOM generator are built; hashed requirements and an SBOM CI job are not (both need a human). |
| FR-O8 | P0 | partial | `docs/operations/deployment-guide.md`, `deploy/compose/README.md`, `deploy/k8s/README.md` | `tests/unit/test_deployment_guide.py`, `tests/lab/test_compose_stack_lab.py` | The guide (22 Nov) covers every FR-O8 item, and its Compose backup and restore is lab-tested; the image upgrade with a migration, backup on k3s and the network to a site PBX are described but not tested. |

## Shared workflow (FR-W)

| ID | P | Status | Code and documents | Tests | Gap |
|---|---|---|---|---|---|
| FR-W1 | P0 | implemented | `AGENTS.md`, `CLAUDE.md`, `docs/context/current-state.md`, `scripts/check_foundation.py` | `tests/unit/test_foundation_tools.py` | none |
| FR-W2 | P0 | partial | `docs/qa/browser-test-cases.md`, `scripts/browser/crawl.cjs` | `tests/browser/foundation.spec.cjs` | Playwright and layered tests are in use; the PRD still names GPT-6 Astra, but since 8 November 2026 Claude designs test cases (AGENTS.md); the PRD wording needs an owner update. |
| FR-W3 | P0 | implemented | `docs/engineering/workflow.md`, `AGENTS.md` | `tests/unit/test_foundation_tools.py` | none |
| FR-W4 | P0 | implemented | `scripts/sync_agents.py`, `scripts/sync_bmad.py`, `scripts/export_context.py` | `tests/unit/test_foundation_tools.py` | none |

## Findings from this audit

- P0 rows with software still to build that needs no data: FR-I4 (live windowing to follow `audio.hop_s`), FR-T5 (scoring-state cleanup within
  5 s, or a recorded deviation), FR-I3 (`partial` in published events), FR-O4 (card
  check for the API's model), FR-O5 (retention switch in Compose and k3s), FR-D5
  (code-switch rule), FR-D12 (per-partition overlap report; the probes need data),
  FR-A1 (MP3/AMR/GSM input and the 8 kHz path), FR-I1 (AMR RFC 4867 payloads), FR-A11
  (local API docs). FR-T3 (from and to) waits on a privacy decision.
- 22 Nov (later the same day): the FR-O8 deployment guide
  (`docs/operations/deployment-guide.md`) moved FR-O8 to `partial` (untested parts named)
  and FR-T4 to `implemented`.
- The independent review of this audit (22 Nov) moved FR-I3, FR-I4, FR-T3, FR-T5,
  FR-M11, FR-O4 and FR-O5 from `implemented` to `partial`; the backlog had recorded
  each as built because the pieces existed, while a PRD clause was unmet.
- `services/api/README.md` said authentication and audit were "not yet"; corrected in
  this change (built 19 November, `services/auth/`).
- The `dashboard` Playwright project, the PyYAML-based k8s tests and the SBOM are not in
  CI; each needs a human workflow edit (`.github/workflows/foundation.yml`).
