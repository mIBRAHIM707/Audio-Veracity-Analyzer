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

Not specified by the PRD and therefore not enforced: ordering between the
suspicious and alert thresholds (for example `alert_enter >= suspicious_enter`), an
upper bound on `min_windows`, or whether `agent` may be scored without `caller`.
