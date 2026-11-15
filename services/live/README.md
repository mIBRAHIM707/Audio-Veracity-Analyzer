# Live service (backlog 8.1 partial, 6.QA; PRD FR-O4, FR-I1-FR-I11, FR-T2-FR-T5, FR-A4)

`service.py` composes the live path into one service object driven by one thread.

- `load_detector(card, onnx, scoring_document, threads=, per_window=)` is the start-up
  gate (FR-O4): `models/export/model_card.check_card` (card, served file name, size,
  hash, scoring config against the card's window), an onnxruntime CPU session opened
  from the hash the card names (`onnx_export.open_session`), `OnnxBatchDetector`, and
  one warm-up window that must give finite logits. The gate also reads the graph: a
  graph with `DynamicQuantizeLinear` nodes (dynamic INT8, whose output depends on batch
  partners) is refused unless `per_window=True`; this needs the `onnx` package
  (`requirements-export.txt`) next to onnxruntime. A `calibrated` card is refused until
  the serving path applies the calibration temperature (not built; spec question). Any
  failure raises `StartupError`: the service does not start half-ready.
- `LiveService(ari, detector, config, vad_factory, receiver_host=, max_calls=, ...)`
  builds EventHub, LiveCalls (CallGate of `max_calls`; the tap admits the same number),
  MicroBatcher, LivePipeline, SpeechFeed, TapScoringSink and TapApp. `run(stop_event)`
  connects the ARI event stream, calls `resume()` (sweep leftovers, tap answered calls),
  then loops `run_once` + `pump` + optional `tick_hooks` (e.g. `StorePump.pump`,
  `RedisForwarder.pump` on their own unfiltered subscriptions). When the stream drops,
  every open tap ends `monitor_lost`, the sink keeps ending those calls, and the loop
  reconnects with backoff 1-30 s. `ready` is set while connected. `stop()` sets the event
  and closes the stream; on exit queued ends complete with their own reason
  (`TapScoringSink.flush_ends`, e.g. `bye`) and every other call ends `monitor_lost`.
  Connection faults count as `disconnects`, other loop faults as `loop_errors`, and
  faults while shutting down as `shutdown_errors`; none stops the remaining steps.

Integration finding (15 Nov): `LiveCalls` expects `for_new_call()` to return a
`ScoringConfig`, but the real `ConfigStore` returns a `ConfigSnapshot`; earlier tests
used a fake source. `_CallConfigs` adapts it here (each new call gets the snapshot's
config; reloads stay atomic). The PRD/architecture do not need a change; a later
refactor could make `LiveCalls` take the store directly.

## Evidence (15 Nov 2026)

Unit `tests/unit/test_live_service.py` (Claude-authored): start-up gate (card hash used
to open the session; config disagreement, session failure, non-finite warm-up, wrong
graph and a missing file all refuse to start; a dynamic-INT8 graph without per-window
serving, an uninspectable graph and a calibrated card refuse); run loop with a fake ARI
whose connection drops once (taps end `monitor_lost`, re-tapped after the reconnect,
every call ended on stop, no hook errors); a `bye` end still queued at stop is
published `bye`; a loop fault and a failing tap close are counted, not raised.
An independent review (15 Nov) found the two start-up gaps and the three loop gaps
above; all are fixed with these tests.

Lab `tests/lab/test_live_service_lab.py` (opt-in, local files only): the 14 Nov static
INT8 8-layer export with an UNTRAINED head, a card written for it in a temporary folder,
real Silero VAD, Asterisk 20.6 call to extension 700 carrying three public CSALT genuine
clips. Observed on this laptop: card, hash, graph inspection and warm-up gate 2.6 s
(1.8 s before graph inspection); 5 windows scored live (finite scores in 0-1), exactly
one `call.ended reason=bye` including after shutdown, the service loop stopped cleanly.
Command: `uv run --no-project --python 3.11 --env-file .local/lab-test.env
--with-requirements requirements-tap.txt --with-requirements requirements-export.txt
python -m unittest tests.lab.test_live_service_lab -v` (env file sets
`AVA_TEST_DOCKER_LAB=1`).
The scores are real ONNX computations with an untrained head: no detection claim.

## Command line

```text
python -m services.live --card model_card.json --onnx ava.onnx --vad-model silero_vad.onnx \
    --ari-url http://127.0.0.1:8088 --ari-user ava --receiver-host <address Asterisk can reach> \
    [--scoring-config ava.json] [--per-window] [--threads 1] [--max-calls 8] \
    [--capacity-windows-per-s 4.0] [--tap-extension 700 ...] [--ari-app ava-tap] [--bind-host 0.0.0.0]
```

Needs the `requirements-tap.txt` and `requirements-export.txt` environments (onnx for the
graph inspection). The ARI password is read from `AVA_ARI_PASSWORD` (or the variable
named by `--ari-password-env`); it is never accepted on the command line or in the URL
(`AriClient` refuses a URL with a user, password, path, query or fragment) and never
printed. Start-up checks, before any call is tapped: password; receiver host (IPv4
address or host name, not 0.0.0.0/multicast; IPv6 not supported), bind host (local IPv4,
one test bind) and tap extensions (1-32 of `0-9A-Za-z*#+_-`); scoring config; model
card, model and warm-up; Silero VAD hash and load; one authenticated ARI request (HTTP
401/403 refuses; an unreachable Asterisk does not, the loop keeps reconnecting and the
start record says `ari_reachable: false`). Each refusal prints one JSON line
`{"event": "refused", ...}` to stderr and exits 2; usage errors exit 2 through argparse;
Ctrl+C during start-up exits 130. A running service prints `{"event": "starting", ...}`
(model version and hash, config fingerprint, limits; readiness itself is the ARI stream)
and, after Ctrl+C/SIGTERM/Ctrl+Break, `{"event": "stopped", ...}` with the service, tap,
sink and pipeline counts, exit 0. A second Ctrl+C during a stalled shutdown forces exit
130 (`"forced": true`).

Evidence (15 Nov, Claude-authored): `tests/unit/test_live_cli.py` (12: wiring, every
refusal path without a traceback or the password, usage errors, real bind check, Ctrl+C
during start-up, first and second SIGINT, a real `python -m services.live` process);
`tests/unit/test_tap.py` URL/user refusals; lab `test_command_line_serves_a_real_call`
(`main()` with the real factories on a real Asterisk call: `starting`, at least 3 windows
scored, `call.ended bye`, `stopped`, exit 0, no disconnect, password not printed).
Independent review: conditional pass; M1 password in the URL, M2 wrong ARI credentials
started "healthy", M3 Ctrl+C during start-up and a stalled shutdown, M4 unchecked hosts,
and lows (VAD runtime errors, imports before refusal handling, extension/user/env-name
validation, start record name, subprocess working directory) all fixed with tests.

## Detector faults on a real call (6.QA)

`tests/lab/test_detector_fault_lab.py` (Claude-authored, 15 Nov): with a detector that
raises on every batch, a lab call with three public clips completes normally (SIPp exit
0), one `call.ended reason=bye`, no scored update, every lost window counted, no
reconnect, nothing left in Asterisk. With a detector that blocks the service thread
from 5.0 s into the call until after the call ended, the call still ran its scripted
15.7 s for 13.9 s of audio and completed normally; after release the service ended the
same call `bye` without a reconnect. This test found that `stop()` closing the stream
was counted as a disconnect (fixed, unit-tested).

## Not done

A deployment configuration file (beyond the scoring JSON); `/readyz`/metrics exposure
of this process (the analysis API has its own); store/Redis wiring by default; agent
direction; capacity measurement on box A1 (8.QA); file-mode jobs through the same
batcher.
