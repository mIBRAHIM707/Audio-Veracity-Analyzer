# Scorer (backlog 4.x)

Pure, dependency-free domain logic for live scoring. No I/O beyond reading a
configuration file; no Redis, PostgreSQL or events yet (backlog 5).

Concurrency contract: `ConfigStore` is thread-safe. `DirectionSession` and
`CallScorer` are not: each call has one owner, `add_window`/`end` must not overlap,
and windows must arrive in order per direction. Their "invalid input changes nothing"
guarantee holds within that single owner. `OrderedCallScorer` (below) is the boundary
that serializes per call, rejects reordered or duplicate inference results and returns
copied event `data` (the dicts are not frozen); integrations must go through it. Independent QA:
`docs/qa/phase-4-qa.md`.

## Configuration (backlog 4.1; PRD FR-I4, FR-A9 subset; architecture §2.4, §7.3)

`config.parse_config(document, model_window_s=...)` validates an `ava.yaml`-shaped
mapping: `audio.window_s`/`hop_s` and the `scoring` section. Missing keys take the
architecture §7.3 defaults.

Rules from PRD FR-I4 and architecture §2.4:

- `0 < suspicious_exit < suspicious_enter <= 1` and `0 < alert_exit < alert_enter <= 1`
- `0 < hop_s <= window_s` and `window_s` equal to the model card window
- reloads apply only to calls started after the reload

Sanity rules added by this module (not PRD requirements):

- `0 < ema_alpha <= 1`; `min_windows`, `degraded_max_stride_factor` integers >= 1;
  `degraded_recovery_s > 0`; `score_directions` a nonempty list of distinct values from
  `caller`, `agent`; numbers must be finite and not booleans or strings
- top-level sections must be ones named in §7.3, and unknown keys inside `audio`,
  `scoring` and `scoring.thresholds` are refused (typo protection); the contents of
  `capacity`, `tap`, `retention` and `inference` belong to other components
- the audio VAD keys must be finite numbers from 0 to 1; their meaning is validated
  with the VAD (backlog 3.2, blocked)

Every failed rule is reported together in `ConfigError.problems`. `load_json_config`
reads the same structure from JSON (at most 64 KiB, UTF-8, no NaN/Infinity, no
duplicate keys, bounded nesting) because no YAML parser is pinned; architecture §7.3
names Pydantic, which this module does not use.

`ConfigStore` gives each new call an immutable `ConfigSnapshot(version, config,
fingerprint)`. `reload(document)` and `reload_file(path)` validate first and swap
atomically; reloads are serialized, so the later of two overlapping reloads wins;
running calls keep their snapshot and a rejected reload changes nothing. `version`
counts reloads in this process only; `fingerprint` (SHA-256 of the canonical JSON of
the settings) identifies them across restarts and is what persisted call state should
store (backlog 5).

## Session state machine (backlog 4.2; PRD FR-I5, FR-I7; architecture §2.4, ADR-11)

`session.DirectionSession(config, call_id=, direction=)` holds one direction's EMA
(first window seeds it), window count, max score, last 30 scores and alert state.
`add_window(p_synthetic)` returns `score.updated` (with the state after the window,
`provisional` while fewer than `min_windows` windows) followed by any `state.changed`
and `alert.raised`/`alert.cleared`. `end(reason, speech_seconds=)` returns
`call.ended` with `final_state`, `verdict` (`undetermined` below `min_windows`,
otherwise the final state; never "genuine") and `ended_open_alert_id` when the call
ends in `alert` (no `alert.cleared` is emitted). Alert IDs come from an injected
generator (UUID4 by default). Scores may be any finite real number from 0 to 1,
including NumPy scalars. Invalid scores, end arguments or a failing ID generator
change nothing (the new state is committed only after everything succeeds).

The transitions are the §2.4 list. Interpretation choices the PRD leaves open:

- at most one transition per window (alert to monitoring takes two windows);
- in `suspicious` the alert check runs before the exit check;
- the "2 consecutive windows at or above alert_enter" streak counts every window from
  `min_windows` on, whatever the state. With the defaults the earliest alert is
  window 3. The streak can also build up while still in `monitoring`, so with
  `alert_enter` below `suspicious_enter` an alert follows one window after entering
  `suspicious`; with `alert_enter <= suspicious_exit` (allowed by FR-I4) a session can
  alert while its EMA is below `suspicious_exit`. A threshold-ordering rule is a PRD
  question.
- `ended_open`: PRD FR-I7 says a call ending in `suspicious` or `alert` lists "the
  alert" as `ended_open`; architecture §2.4 covers only an open alert. This follows
  §2.4: ending in `suspicious` persists the state with no open alert. PRD
  clarification requested.
- `verdict` reuses the state names (`monitoring`, `suspicious`, `alert`) or
  `undetermined`; the PRD defines no end-of-call verdict vocabulary.
- `state.changed` is an internal domain event, not an addition to the FR-A4
  published list (which carries the state in `score.updated`).
- `score.updated` does not yet carry `degraded` and the effective stride (FR-I6,
  FR-I10; backlog 8.2).

## Call-level scoring (backlog 4.3; PRD FR-I8, FR-I2 gate, FR-U1 flags)

`call.CallScorer(config, call_id=)` routes windows to per-direction sessions:

- only directions in `score_directions` are scored (caller by default); others are
  counted in `ignored['direction_not_scored']`;
- the agent direction starts at `human_agent_answered()`; agent windows before it
  (IVR prompts) are counted in `ignored['before_human_answer']` and are not evidence;
- a window whose `concealed_fraction` is above `audio.max_concealed_fraction` (0.10,
  FR-I2) is not scored, and the call shows `quality=low` (one `call.quality` event);
- the call state is the more severe direction state, with `driving_direction`; a
  `call.state` event (with the `window_direction` that caused it) is emitted when
  either changes;
- `end(reason, speech_seconds={direction: seconds})` (a plain dict) validates the
  reason and every direction before ending any, and returns each `call.ended` plus a
  `call.summary` (final state, driving direction, verdict, quality, drop and ignore
  counts, open alert IDs per direction).

Interpretation choices (PRD clarification requested under FR-I2/FR-I5/FR-I8):

- `quality=low` stays low for the rest of the call and is call-level (architecture
  §2.4 lists quality per direction; drop counts are kept per direction); only windows
  that would otherwise be scored are gated;
- on a tie the caller drives; there is no driving direction while every direction is
  `monitoring`;
- call verdict: `suspicious`/`alert` when some direction is in that state (only a
  direction with enough evidence can be); `monitoring` only when every scored
  direction reached `min_windows`; otherwise `undetermined`, so a clean agent side
  never makes an under-evidenced caller look genuine;
- event window numbers count that direction's scored windows; the two directions do
  not share one timeline.

`call.quality`, `call.state` and `call.summary` are internal domain events like
`state.changed`. Not here: `no_speech` (needs the VAD, backlog 3.2), `unmonitored`
and the degraded flag (backlog 8.2), the `system.health` media-quality event for
sustained loss (FR-I2, needs the live jitter buffer, backlog 6), and detecting the
human answer itself (telephony, backlog 6).

## Degraded mode (backlog 8.2 partial; PRD FR-I10, FR-I6)

`degraded.StrideController(config)` turns "behind / under budget" reports into the
effective stride: each `behind` report adds one hop up to
`degraded_max_stride_factor` x hop, a `behind` report at the cap sets `unmonitored`,
and only `degraded_recovery_s` under budget (with no `behind` report) returns to one
hop. `status()` gives `degraded` and `effective_stride_s` for `score.updated`. What
"behind" means is the caller's (the documents do not define it). The caller must keep
reporting at the capped stride while unmonitored, and catch the ValueError for a
backwards clock. Open: unmonitored versus an open alert on the call card (PRD) and the
see-saw risk under sustained load (8.QA).

`pending.PendingWindows(controller, max_pending=)` is the bounded per-call queue in
front of inference. The window builder offers every window by strictly increasing
`index`; windows closer than the current stride are skipped (counted, not reported);
an accepted window is reported to the controller as `behind` when an earlier accepted
window is still waiting (in-flight windows taken by `take()` do not count); a full
queue drops its oldest window (counted). `take()` is first in, first out so the EMA
sees windows in time order. Memory is at most `max_pending` windows, which the caller
must choose (no documented value). These are interpretation choices and give one
concrete meaning to "behind" for FR-I10; the PRD should confirm or replace it.
Remaining 8.2 work: wiring the queue into a live inference loop (needs backlog 6/8.1).

## Ordered boundary (backlog 5; phase-4 QA M1 and L3)

`ordered.OrderedCallScorer(call_scorer)` is the boundary where micro-batched
inference results re-enter a call. One lock per call serializes `add_result`,
`human_agent_answered`, `end` and `snapshot`. Each result carries the window
builder's per-direction `window_idx`, which must strictly increase: a duplicate or
stale (late) result is not applied and is counted in `rejected`; gaps are allowed
because the stride and the pending queue skip or drop windows. Interpretation
choices: late results are dropped, not re-ordered; a window the scorer ignores (agent
before the human answer, direction not scored) still consumes its index; a scorer
error leaves the index unchanged so the result may be retried. Returned events are
deep copies, so consumers cannot change the stored `call.ended`/`call.summary`.

## Publishing FR-A4 events (backlog 5.2 partial; PRD FR-A4)

`publish.py` maps scorer events to the published types (`services/events`):
`score.updated` carries the window's score/EMA/provisional flag, its direction and
`direction_state`, the call-level `state` and `driving_direction` after the window,
`quality`, and the caller-supplied `degraded`/`effective_stride_s`; `alert.raised` /
`alert.cleared` carry `alert_id`, `direction`, `ema`; `state.changed` and `call.state`
stay internal. Choices made here: the first quality-gated window is published as an
unscored `score.updated` (`score: null`, `scored: false`, `quality: low`);
`status()` publishes `no_speech`/`unmonitored` the same way; the call-level
`call.ended` sums the scored directions' speech seconds and adds the verdict,
per-direction speech/windows and `ended_open_alert_ids`. `CallEventPublisher` never
raises into scoring: hub, contract and mapping failures are counted in `failures`.
It is not thread-safe; publish a call's results in the order the scorer returned them.
Unknown event types are skipped and counted; the tracked call state is folded before
mapping, so a mapping failure cannot leave it stale. `status()` accepts only
`no_speech` and `unmonitored`.

## Live call table (backlog 5.2/5.3 partial; PRD FR-I4, FR-I9, FR-I11)

`live.LiveCalls(config_store, hub, gate)` is the live-path owner of calls:
`start` admits through the `CallGate` (a refused call is not tapped and gets
`system.health reason=capacity`; the call continues) and builds the scorer from the
config snapshot current at the start; `result` applies and publishes one inference
result under the call's own lock, so events reach the hub in scorer order with
several workers; `end` removes the call first (results racing with it are discarded
and counted), ends the scorer, publishes `call.ended` and frees the gate slot; an
invalid end raises and leaves the call running; `end_all('monitor_lost', ...)` ends
every open call (if its speech seconds cannot be obtained it still ends, with zero
and `speech_seconds_known: false`, counted); `start` holds the call's lock until
`call.started` is published and starts are serialized; publisher failures are summed
into `counters['publish_failures']` at the end; `is_active` lets the inference
loop skip queued windows of an ended call. `end_all` counts a call whose end failed
(`end_all_failures`) and still releases its slot. Known limit: the start lock is held
through the config read and the capacity health publish, so a slow config store
would delay every admission (fine for the in-memory store). Spec question: unknown
speech seconds are published as 0.0 with a flag, because the contract needs a
number. Not done: Redis/PostgreSQL state and
restart resume (FR-I11, needs 5.1) and wiring to a real tap/inference loop (6.x/8.1).

## Live inference loop (backlog 8.1/8.2 wiring; PRD FR-I9-FR-I11)

`pipeline.LivePipeline(live, batcher, detector, config, max_pending=4)` is a
single-owner loop with an explicit monotonic `now`: `offer` puts a built 4 s float32
window into its direction's `PendingWindows` (stride, drop-oldest, `StrideController`)
and publishes `unmonitored` once when the stride cap is reached; at most one window
per direction is in the batcher or in inference, so a window still waiting when the
next one arrives is "behind" and the stride grows instead of a queue (FR-I10);
`schedule` serves idle directions from a rotating start while the `MicroBatcher` has
live room; `now` must never go backwards (checked before any change); `step` runs one due
batch through the detector (`[B, 64000]` -> `[B, 2]` logits) and applies the scores
through `LiveCalls` with the direction's degraded flag and stride. `end_call` ends the
call and cancels its waiting windows; batched windows of ended calls are skipped and
calls ended elsewhere are cleaned up at the next `schedule`; a batched window from an
earlier call with the same ID is skipped. A detector fault loses that batch's scores
only, and a failing result is counted without stopping the rest of the batch. Windows
are copied on offer. File jobs and the 25 % share are not driven here yet (a file
batch handed out is counted, not run); no threads, no real detector or tap wired.
Spec questions: whether the call card keeps `unmonitored` while scoring continues at
the capped stride, and a time limit for the detector call.

## Speech windows (backlog 3.2 partial; PRD FR-I3, FR-I4; architecture §2.2 steps 3-4)

`speech_windows.SpeechWindower(vad, threshold=0.5)` per call direction: 16 kHz int16 PCM
in any chunk sizes (at most 10 s per `feed`) is split into 512-sample frames; frames
with VAD probability below the threshold are excluded but advance time; the
speech-only stream is cut with the file-mode plan (`plan_windows`: 4 s every 2 s, and
at `finish()` a partial window when at least 1.5 s of speech is uncovered). Windows
carry speech and stream coordinates, `partial`, `scorable` (at least 2.5 s of speech;
only partial windows can fall below it) and float32 [64000] PCM (int16/32768,
zero-padded). `no_speech`, `speech_seconds` and `seconds_since_speech` feed the
`no_speech` state. A VAD fault raises `VadFailure` after dropping that audio as
non-speech and calling `vad.reset()` (do not retry the audio); at `finish()` it drops
only the last part-frame. The real VAD is `services/inference/silero_vad.SileroVad`
(ONNX on CPU, `requirements-vad.txt`, model silero-vad 6.2.3 `silero_vad.onnx`, MIT,
SHA-256 pinned, provisioned locally at e.g.
`D:/AVA-Data/models/silero-vad/6.2.3/`; one instance per direction; parity with the
reference wrapper in `tests/model/test_silero_vad_onnx.py`). Not done: connecting
windows to `LivePipeline.offer` (drop non-scorable windows, carry `partial` and
times), configurable window/hop, peak normalization, and a Linux resolution check of
`requirements-vad.txt` (`uv pip compile` was not permitted in the 14 Nov session).
Spec questions: file mode scores a lone 1.5-2.5 s partial window while this marks it
not scorable; the meaning of "2.5 s is speech" when windows hold speech only.

Not specified by the PRD and therefore not enforced in configuration: ordering between the
suspicious and alert thresholds (for example `alert_enter >= suspicious_enter`), an
upper bound on `min_windows`, or whether `agent` may be scored without `caller`.
