"""Scoring configuration validation and call-scoped reload (backlog 4.1; PRD FR-I4,
FR-A9 file-plus-reload subset; architecture §2.4 rules and §7.3 keys and defaults).

Pure standard library. `parse_config` reads the `audio` window/hop keys and the
`scoring` section of an already-parsed `ava.yaml`-shaped mapping; the contents of the
other §7.3 sections (capacity, tap, retention, inference) belong to other components
and are not checked here, but an unknown top-level section is refused. No YAML parser is pinned in this repository, so `load_json_config` reads the
same structure from JSON. Every problem is collected and raised together as
ConfigError; nothing is partially applied.

`ConfigStore` hands each new call an immutable snapshot. A reload validates the whole
document first and swaps the snapshot atomically; calls already running keep the
snapshot they started with (FR-I4 "reloads apply to calls started after the reload"),
and a rejected reload leaves the current snapshot in place.
"""
import dataclasses
from dataclasses import dataclass, field
import hashlib
import json
import math
import os
import threading

DIRECTIONS = ('caller', 'agent')
MAX_CONFIG_BYTES = 64 * 1024
TOP_LEVEL_KEYS = {'audio', 'scoring', 'capacity', 'tap', 'retention', 'inference'}  # §7.3
# Fractions from 0 to 1. max_concealed_fraction is kept (FR-I2 quality gate); the two
# VAD keys are only range-checked until the VAD exists (backlog 3.2).
AUDIO_FRACTION_KEYS = ('vad_threshold', 'min_speech_fraction', 'max_concealed_fraction')
AUDIO_KEYS = {'window_s', 'hop_s', *AUDIO_FRACTION_KEYS}
SCORING_KEYS = {'ema_alpha', 'min_windows', 'thresholds', 'score_directions',
                'degraded_max_stride_factor', 'degraded_recovery_s'}
THRESHOLD_KEYS = ('suspicious_enter', 'alert_enter', 'alert_exit', 'suspicious_exit')


class ConfigError(ValueError):
    """Invalid scoring configuration; `problems` lists every rule that failed."""

    def __init__(self, problems):
        self.problems = tuple(problems)
        super().__init__('invalid scoring configuration: ' + '; '.join(self.problems))


@dataclass(frozen=True)
class Thresholds:
    suspicious_enter: float = 0.60
    alert_enter: float = 0.75
    alert_exit: float = 0.55
    suspicious_exit: float = 0.40


@dataclass(frozen=True)
class ScoringConfig:
    """Validated, immutable scoring settings (architecture §7.3 defaults)."""
    window_s: float = 4.0
    hop_s: float = 2.0
    ema_alpha: float = 0.4
    min_windows: int = 2
    thresholds: Thresholds = field(default_factory=Thresholds)
    score_directions: tuple = ('caller',)
    degraded_max_stride_factor: int = 4
    degraded_recovery_s: float = 30.0
    max_concealed_fraction: float = 0.10  # FR-I2 quality gate (audio section)


@dataclass(frozen=True)
class ConfigSnapshot:
    """`version` counts reloads in this process only; `fingerprint` (SHA-256 of the
    canonical JSON of the config) identifies the settings across restarts."""
    version: int
    config: ScoringConfig
    fingerprint: str


def fingerprint(config):
    canonical = json.dumps(dataclasses.asdict(config), sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:  # an integer too large for a float is not a usable setting
        return False


def _section(document, name, allowed, problems):
    section = document.get(name, {})
    if not isinstance(section, dict):
        problems.append(f'{name} must be a mapping')
        return {}
    unknown = sorted(set(section) - set(allowed), key=str)
    if unknown:
        problems.append(f'{name} has unknown keys {unknown}')
    return section


def parse_config(document, *, model_window_s):
    """Validate an ava.yaml-shaped mapping against FR-I4 and architecture §2.4.
    Missing keys take the §7.3 defaults. Returns ScoringConfig or raises ConfigError."""
    if not _number(model_window_s) or model_window_s <= 0:
        raise ConfigError(['model_window_s must be a positive finite number from the model card'])
    if not isinstance(document, dict):
        raise ConfigError(['configuration must be a mapping'])
    problems, defaults = [], ScoringConfig()
    unknown_sections = sorted(set(document) - TOP_LEVEL_KEYS, key=str)
    if unknown_sections:
        problems.append(f'unknown top-level sections {unknown_sections}')
    audio = _section(document, 'audio', AUDIO_KEYS, problems)
    scoring = _section(document, 'scoring', SCORING_KEYS, problems)
    raw_thresholds = scoring.get('thresholds', {})
    if not isinstance(raw_thresholds, dict):
        problems.append('scoring.thresholds must be a mapping')
        raw_thresholds = {}
    unknown = sorted(set(raw_thresholds) - set(THRESHOLD_KEYS), key=str)
    if unknown:
        problems.append(f'scoring.thresholds has unknown keys {unknown}')

    def number(section, name, key, default):
        value = section.get(key, default)
        if not _number(value):
            problems.append(f'{name}.{key} must be a finite number')
            return None
        return float(value)

    def integer(section, name, key, default, minimum):
        value = section.get(key, default)
        if type(value) is not int or value < minimum:
            problems.append(f'{name}.{key} must be an integer of at least {minimum}')
            return None
        return value

    window = number(audio, 'audio', 'window_s', defaults.window_s)
    hop = number(audio, 'audio', 'hop_s', defaults.hop_s)
    alpha = number(scoring, 'scoring', 'ema_alpha', defaults.ema_alpha)
    min_windows = integer(scoring, 'scoring', 'min_windows', defaults.min_windows, 1)
    stride = integer(scoring, 'scoring', 'degraded_max_stride_factor',
                     defaults.degraded_max_stride_factor, 1)
    recovery = number(scoring, 'scoring', 'degraded_recovery_s', defaults.degraded_recovery_s)
    levels = {key: number(raw_thresholds, 'scoring.thresholds', key, getattr(defaults.thresholds, key))
              for key in THRESHOLD_KEYS}

    if window is not None and hop is not None and not 0 < hop <= window:
        problems.append('audio requires 0 < hop_s <= window_s')
    if window is not None and not math.isclose(window, model_window_s, rel_tol=0, abs_tol=1e-9):
        problems.append(f'audio.window_s {window} differs from the model card window {model_window_s}')
    for key in AUDIO_FRACTION_KEYS:  # range check; see AUDIO_FRACTION_KEYS for which are used
        if key in audio and (not _number(audio[key]) or not 0 <= audio[key] <= 1):
            problems.append(f'audio.{key} must be a finite number from 0 to 1')
    if alpha is not None and not 0 < alpha <= 1:
        problems.append('scoring.ema_alpha must be in (0, 1]')
    if recovery is not None and recovery <= 0:
        problems.append('scoring.degraded_recovery_s must be positive')
    for prefix in ('suspicious', 'alert'):
        enter, exit_ = levels[f'{prefix}_enter'], levels[f'{prefix}_exit']
        if enter is not None and exit_ is not None and not 0 < exit_ < enter <= 1:
            problems.append(f'scoring.thresholds requires 0 < {prefix}_exit < {prefix}_enter <= 1')

    directions = scoring.get('score_directions', list(defaults.score_directions))
    if (not isinstance(directions, (list, tuple)) or not directions
            or any(d not in DIRECTIONS for d in directions) or len(set(directions)) != len(directions)):
        problems.append(f'scoring.score_directions must be a nonempty list of distinct values from {DIRECTIONS}')
        directions = None

    if problems:
        raise ConfigError(problems)
    return ScoringConfig(window_s=window, hop_s=hop, ema_alpha=alpha, min_windows=min_windows,
                         thresholds=Thresholds(**levels), score_directions=tuple(directions),
                         degraded_max_stride_factor=stride, degraded_recovery_s=recovery,
                         max_concealed_fraction=float(audio.get('max_concealed_fraction',
                                                                defaults.max_concealed_fraction)))


def read_json_document(path):
    """Parse a JSON document (at most MAX_CONFIG_BYTES, UTF-8, no NaN/Infinity, no
    duplicate keys, bounded nesting). Raises ConfigError; a missing file is OSError."""
    name = os.path.basename(path)
    with open(path, 'rb') as handle:
        payload = handle.read(MAX_CONFIG_BYTES + 1)
    if len(payload) > MAX_CONFIG_BYTES:
        raise ConfigError([f'{name} exceeds {MAX_CONFIG_BYTES} bytes'])
    try:
        return json.loads(payload.decode('utf-8'), parse_constant=_refuse_constant,
                          object_pairs_hook=_refuse_duplicates)
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise ConfigError([f'{name} is not valid UTF-8 JSON: {type(error).__name__}: {error}']) from None


def load_json_config(path, *, model_window_s):
    """Read and validate a JSON document with the ava.yaml structure."""
    return parse_config(read_json_document(path), model_window_s=model_window_s)


def _refuse_constant(name):
    raise ValueError(f'non-standard JSON constant {name}')


def _refuse_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'duplicate key {key!r}')
        result[key] = value
    return result


class ConfigStore:
    """Current scoring snapshot for new calls; reloads are serialized and atomic."""

    def __init__(self, config):
        if not isinstance(config, ScoringConfig):
            raise TypeError('ConfigStore needs a validated ScoringConfig')
        self._lock = threading.Lock()  # guards the snapshot
        self._reload_lock = threading.Lock()  # serializes read-validate-swap
        self._snapshot = ConfigSnapshot(1, config, fingerprint(config))

    def for_new_call(self):
        """Snapshot a call keeps for its whole life."""
        with self._lock:
            return self._snapshot

    def reload(self, document, *, model_window_s):
        """Validate then swap; on ConfigError the current snapshot is unchanged."""
        with self._reload_lock:
            return self._swap(parse_config(document, model_window_s=model_window_s))

    def reload_file(self, path, *, model_window_s):
        """Reload from a JSON file through the hardened reader (FR-A9 file plus reload).
        Raises ConfigError for invalid content and OSError (e.g. FileNotFoundError) when
        the file cannot be read; either way the current snapshot is unchanged."""
        with self._reload_lock:
            return self._swap(load_json_config(path, model_window_s=model_window_s))

    def _swap(self, config):
        with self._lock:
            self._snapshot = ConfigSnapshot(self._snapshot.version + 1, config, fingerprint(config))
            return self._snapshot
