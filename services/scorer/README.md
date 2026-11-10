# Scorer (backlog 4.x)

Pure, dependency-free domain logic for live scoring. No I/O beyond reading a
configuration file; no Redis, PostgreSQL or events yet (backlog 5).

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

Not specified by the PRD and therefore not enforced in configuration: ordering between the
suspicious and alert thresholds (for example `alert_enter >= suspicious_enter`), an
upper bound on `min_windows`, or whether `agent` may be scored without `caller`.
