# Phase 4 QA: streaming decisions (FR-I4 to FR-I8)

**Provenance.** Independent QA by ava-reviewer (Claude), 10 November 2026. Read-only
pass over `services/scorer/{config,session,call}.py`, `services/scorer/README.md` and
`tests/unit/test_scorer_{config,session,call}.py` on `feat/4-daily-development`
(commits 1cf33c9, 2ebd2fc, 18710c5). The reviewer's shell was unavailable, so the
tests were **not re-run here**. The pass results are the runner's recorded ones.
Test counts were checked by reading: 22 config, 25 session, 17 call tests, all
Claude-authored on 10 November 2026.

**Verdict: conditional pass as infrastructure.** The review found no blocking defect.
Conditions: state the single-owner, in-order contract (M1), and get the open PRD
decisions below. These are pure-logic unit tests. They are **not** evidence of
detection accuracy, live or telephony behaviour, latency (FR-I6), persistence or
restart recovery (backlog 5) or degraded mode (backlog 8.2).

## Requirement coverage

| Req | Code | Tests | Status |
|---|---|---|---|
| FR-I2 quality gate | `call.py:73-80` (strictly more than `max_concealed_fraction`, window not scored, `quality=low`) | `QualityGateTests` (0.10 vs 0.1000001, configured 0.2), `test_quality_gate_applies_only_to_windows_that_would_be_scored` | Partial: the gate is covered; computing the concealed fraction live is 6.3 (offline library in 3.3); the `system.health` sustained-loss event is 5.2/6 |
| FR-I3 VAD/partial/no_speech | none | none | Not in scope: 3.2 (blocked) |
| FR-I4 config rules, call-scoped reload | `config.py:96-169`, `ConfigStore` `:205-233` | `ParseConfigTests` (12), `ConfigStoreTests` (7), `test_session_keeps_config_snapshot_across_reload` | Covered for rules and reload. The model-card window is injected by the caller (no card reader; inference/8.1). The JSON loader replaces YAML/Pydantic (§7.3 deviation) |
| FR-I5 EMA, min evidence, undetermined | `session.py:97-133`, `:144-156`; call verdict `call.py:116-121` | `EmaAndEvidenceTests`, `test_clean_agent_does_not_make_short_caller_genuine`, `test_all_directions_short_is_undetermined` | Covered for live per-direction scoring. N comes from FR-M9 (2.9, unmeasured). The file/batch verdict is in `services/inference` (2.1; mean vs EMA is an open question) |
| FR-I6 provisional latency, degraded flag/stride | provisional flag only | `test_first_window_is_provisional_and_changes_no_state` | Partial: latency is unmeasured (8.QA); degraded flag and stride are 8.2 |
| FR-I7 hysteresis FSM, alert IDs, ended_open | `session.py:55-62`, `:97-133`, `:144-156` | `StateMachineTests` (incl. a 300-trial reference model), `EndAndFailureTests` | Covered for §2.4. Persisting the final state is 5.1/5.3. `ended_open` for `suspicious` awaits the PRD (Q1) |
| FR-I8 directions, agent after answer, severity/driver | `call.py:35-55`, `:67-72`, `:90-98` | `DirectionSelectionTests` (hand-worked timeline) | Covered as logic. Detecting the human answer and the second snoop channel are 6.2 |
| FR-A4 published events | `score.updated`, `alert.raised`, `alert.cleared`, `call.ended` produced as domain events | session/call tests | Partial: `call.started`, `system.health`, the WebSocket and Redis are 5.1/5.2 |
| FR-A9 file plus reload | `ConfigStore.reload_file` `config.py:225-228` | `test_reload_file_uses_hardened_reader`, `LoadJsonConfigTests` | Partial: no reload trigger, endpoint or audit (5.2, 7.3) |
| FR-U1 flags/states | `monitoring`, `suspicious`, `alert`, `quality` | as above | Partial: `no_speech` is 3.2, `unmonitored`/degraded is 8.2, the board is 7.1 |
| D10 | config-driven EMA and three-state machine | as above | Covered (logic) |

## Boundary, state-transition and concurrency assessment

**Boundaries (genuinely tested).** Thresholds at equality on both sides of enter and
exit. Values tested: `hop == window`, `hop` of 0 or negative, `window` 1e-6 off the
model card, alpha 0, 1 and 1.01, `min_windows` 0, 1, 2.0 and `True`. Also scores
0, 1, -0.01, 1.01, NaN, inf, bool, str and complex; the concealed gate at 0.10
versus 0.1000001; `speech_seconds` 0 versus -1, NaN and bool; JSON at
MAX_CONFIG_BYTES+1, NaN, duplicate keys, Latin-1 and 60 000-deep nesting.
Untested: a file of exactly MAX_CONFIG_BYTES accepted; the configured gate exactly
at 0.2; huge integers (L1).

**State transitions (genuinely tested).** Every §2.4 transition has a literal test:
`monitoring -> alert` is refused, re-alert gets a new ID, alert to monitoring takes
two windows, the streak must be consecutive, and the end states are `alert`
(`ended_open`), `suspicious`, `monitoring` and `undetermined`. A 300-trial seeded
comparison checks event pairing and state invariants against a table-driven
reference. The reference repeats the same interpretation choices (streak counted from
`min_windows` in any state, one transition per window). So it proves the code matches
its own reading of §2.4, not that the open points are settled. Failure atomicity is
tested: invalid input, a failing or bad alert-ID generator, and invalid `end` change
nothing, at both session and call level.

**Concurrency.** `ConfigStore` has one deterministic overlap test (the newer reload
wins; it fails without the reload lock) and one multi-thread smoke test that is
honestly labelled as unable to prove the lock is needed under the GIL.
`DirectionSession` and `CallScorer` are **not thread-safe**: `add_window` is an
unguarded read-modify-write, and windows are applied in arrival order with no
`window_idx`. Their "atomic" claim means exception atomicity within one thread only.
Nothing in the code or README says this; see M1. They have no concurrency tests.
That is acceptable for per-call, single-owner objects only once the contract is
written down.

## Findings

| ID | Sev | Location | Finding and fix |
|---|---|---|---|
| M1 | Medium | `session.py:1-8`, `:72-73`; `call.py:27`; `README.md:3-4` | **Trigger:** backlog 5 wiring delivers micro-batched inference results for one call on more than one worker, or out of order. **Consequence:** lost or duplicated `windows`/EMA updates, or a streak computed in the wrong order, giving wrong alerts. Nothing warns the integrator. **Fix:** state in the docstrings and README that a call's `CallScorer` must have one owner and that `add_window`/`end` must be serialized and fed in `window_idx` order per direction. Add a 5.x integration test that reorders or duplicates results, or a monotonic `window_idx` check at the boundary. |
| L1 | Low | `config.py:80-82` (`_number`), `session.py:49-52` (`_real`), `config.py:225-228` | `math.isfinite` on a huge integer (for example a 400-digit JSON integer for `ema_alpha`, below the 4300-digit int limit) raises OverflowError instead of ConfigError or ValueError. `reload_file` on a missing file raises OSError, which its docstring does not mention. The snapshot stays unchanged, but a caller that catches only ConfigError would crash. **Fix:** treat OverflowError as not finite. Document OSError on `reload_file`. Add tests with `10**400` as a config value and as `p_synthetic`. |
| L2 | Low | `test_scorer_call.py` | Missing cases: agent scoring enabled but the agent never answers, then `end` with a caller-only dict; dropped windows in both directions (one `call.quality` event, per-direction counts); gated windows leaving a call `undetermined`; a file of exactly MAX_CONFIG_BYTES. |
| L3 | Low | `session.py:65-69`, `call.py:122-129` | `Event` is frozen, but `data` is a mutable dict, and the stored `ended` events can be changed by any consumer. **Fix:** copy on publish in 5.x, or use `MappingProxyType`. |

No high-severity findings. No privacy, egress or call-path problem was found in this
scope: the code is pure logic with no I/O beyond reading the config file.

## Interpretation choices needing PRD decisions

1. FR-I7: whether a call ending in `suspicious` lists anything as `ended_open`. The
   code follows §2.4: only an open alert.
2. Threshold ordering between the pairs (`alert_enter >= suspicious_enter`?
   `alert_enter > suspicious_exit`?). Currently unordered pairs are allowed, so an
   alert can occur with the EMA below `suspicious_exit`.
3. The alert streak counts from `min_windows` in any state (it can build in
   `monitoring`). At most one transition per window. In `suspicious` the alert check
   runs before the exit check.
4. End-of-call verdict vocabulary: state names plus `undetermined`. A `monitoring`
   verdict could be read as "genuine".
5. Call verdict with two directions: `undetermined` unless every scored direction has
   N windows or one is suspicious/alert.
6. FR-I2: `quality=low` stays low for the rest of the call and is call-level (§2.4
   lists it per direction). Gated windows do not count toward N.
7. FR-I8: ties go to the caller; there is no driving direction while everything is
   `monitoring`; agent-only scoring is allowed; each direction has its own window
   numbering.
8. `state.changed`, `call.quality`, `call.state` and `call.summary` are internal
   events, outside the FR-A4 published list.
9. No upper bound on `min_windows`. File-mode mean versus EMA (2.1).
10. Architecture §7.3: JSON loader instead of YAML/Pydantic until a parser is pinned.

## Not assessed

Running the tests (shell unavailable), mutation testing, performance and memory under
load, and any integration with inference, Redis, PostgreSQL or Asterisk.

## Resolution (coordinator, 10 November 2026)

- M1: the single-owner, serialized, in-order contract is stated in `session.py`,
  `call.py` and the README; the boundary check and reorder/duplicate tests are
  assigned to backlog 5.
- L1: `_number` and `_real` treat integers too large for a float as not finite;
  `reload_file` documents OSError; tests use `10**400` for config values, scores and
  concealed fractions.
- L2: added the never-answered agent, both-direction drops, gated-undetermined and
  exactly-MAX_CONFIG_BYTES cases.
- L3: deferred to backlog 5 (copy event data on publish), recorded in the README.
- After the fixes the coordinator ran the tests: scorer modules 69 OK (23 config, 25
  session, 21 call); full unit suite recorded in the daily report.
- The ten interpretation choices above are sent to Tughral for PRD decisions; none is
  presented as a requirement.
