# Scorer (backlog 4.x)

Pure, dependency-free domain logic for live scoring. No I/O beyond reading a
configuration file; no Redis, PostgreSQL or events yet (backlog 5).

Concurrency contract: `ConfigStore` is thread-safe. `DirectionSession` and
`CallScorer` are not: each call has one owner, `add_window`/`end` must not overlap,
and windows must arrive in order per direction. Their "invalid input changes nothing"
guarantee holds within that single owner. Backlog 5 must serialize per call, reject
reordered or duplicate inference results (monotonic window index at the boundary) and
copy event `data` before publishing (the dicts are not frozen). Independent QA:
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
backwards clock. Open: unmonitored versus an open alert on the call card (PRD), the
see-saw risk under sustained load (8.QA), and the bounded per-call pending queue
(remaining 8.2 work).

Not specified by the PRD and therefore not enforced in configuration: ordering between the
suspicious and alert thresholds (for example `alert_enter >= suspicious_enter`), an
upper bound on `min_windows`, or whether `agent` may be scored without `caller`.
