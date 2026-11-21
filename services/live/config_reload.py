"""Scoring-config file plus reload for the live process (backlog 7.3; PRD FR-A9 "config
file plus reload ... validated per FR-I4"; FR-I4 "reloads apply to calls started after
the reload"; architecture §2.4, §7.3).

`ConfigFileWatcher(path, store, fixed=, model_window_s=)` rereads the scoring JSON
document that `--scoring-config` named, on its own `PumpThread` (never the scoring
thread), every `--scoring-config-watch-s` seconds:

- The whole file is read (at most `MAX_CONFIG_BYTES`, the hardened reader of
  `services/scorer/config.py`) and compared by SHA-256 with the last read, so edits
  within one file-system timestamp tick and atomic replacements (a Kubernetes ConfigMap
  swaps a symlink) are both seen; nothing happens while the bytes stay the same.
- Changed bytes are validated as at start-up (`parse_config` with the model card's
  window). A valid document whose settings equal the running ones (only spacing or key
  order changed) is `unchanged`: no new version.
- Settings the running process applies to every call at once cannot follow a reload
  per call: `audio.hop_s` (the stride controller and the reported effective stride; live
  windowing itself advances by the constant 2 s `HOP_SAMPLES`), `scoring.
  degraded_max_stride_factor` and `scoring.degraded_recovery_s`. A document changing any
  of them is `rejected` with "needs a restart"; `audio.window_s` must equal the model
  card anyway. A document without `caller` in `score_directions` is rejected too: the
  tap hears only the caller, so it would silently stop scoring new calls (review M1).
- Keys that are accepted but used by nothing yet (`audio.vad_threshold`,
  `min_speech_fraction`, the other components' sections) do not change the settings
  fingerprint, so editing only them reports `unchanged`.
- A poll during shutdown (the pump thread's final turn) can still apply a reload whose
  log line is lost; the stopped record's counts show it. Everything else (thresholds, EMA alpha, minimum windows, scored directions,
  `max_concealed_fraction`) applies per call, so the store swaps its snapshot atomically
  (`ConfigStore.reload`) and only calls started after the swap use it; running calls
  keep the snapshot they started with.
- An invalid document is `rejected`, an unreadable or missing file `unreadable`; either
  way the running settings stay. Each distinct state is reported once (not every poll),
  as an outcome count and a record for the operations log: `config_reloaded`,
  `config_unchanged`, `config_rejected`, `config_unreadable`. Records carry the rule
  messages of `ConfigError` (key names and values from the document) or an OS error's
  type and `strerror`, never the path.

The first read sets the baseline: if the file still holds the start-up settings nothing
is reported; if it changed between start-up and that read, the change is handled as
any other. `pump()` never raises for file or validation problems; it returns 0 so the
`PumpThread` waits its interval between reads.
"""
from collections import deque
import hashlib
import threading

from services.scorer.config import MAX_CONFIG_BYTES, ConfigError, fingerprint, parse_config, parse_json_bytes

# (section, key, ScoringConfig attribute): applied process-wide, so a reload may not change them.
PROCESS_KEYS = (('audio', 'hop_s', 'hop_s'),
                ('scoring', 'degraded_max_stride_factor', 'degraded_max_stride_factor'),
                ('scoring', 'degraded_recovery_s', 'degraded_recovery_s'))
OUTCOMES = ('applied', 'unchanged', 'rejected', 'unreadable')
MAX_RECORDS = 32  # undelivered log records kept (oldest dropped, counted)
MAX_PROBLEMS = 8  # rule messages per record
_TEXT = 200


def _read_bytes(path):
    with open(path, 'rb') as handle:
        return handle.read(MAX_CONFIG_BYTES + 1)


class ConfigFileWatcher:
    def __init__(self, path, store, *, fixed, model_window_s, reader=_read_bytes):
        """`store` is the service's `ConfigStore`; `fixed` the `ScoringConfig` the
        process-wide parts (pipeline, stride) were built with."""
        if not isinstance(path, str) or not path:
            raise ValueError('path must be a nonempty string')
        if not callable(reader):
            raise ValueError('reader must be callable')
        self.path, self.store, self.fixed, self.model_window_s = path, store, fixed, model_window_s
        self._reader = reader
        self._lock = threading.Lock()  # counts/records are read by the health and log threads
        self._last = None  # sha256 of the last bytes read, or ('error', type name)
        self._records = deque()
        self.counts = {outcome: 0 for outcome in OUTCOMES}
        self.counts.update(polls=0, records_dropped=0)
        self.last_outcome = None

    # -- one poll ---------------------------------------------------------------------
    def pump(self):
        """Read once; act only when the bytes (or the read error) changed. Returns 0."""
        with self._lock:
            self.counts['polls'] += 1
        try:
            payload = self._reader(self.path)
        except OSError as error:
            state = ('error', type(error).__name__)
            if state != self._last:
                self._last = state
                # The start-up read worked, so even a failing first poll is a change to report.
                self._report('unreadable', 'warning', error=type(error).__name__,
                             detail=(error.strerror or '')[:_TEXT])
            return 0
        digest = hashlib.sha256(payload).hexdigest()
        if digest == self._last:
            return 0
        baseline = self._last is None
        self._last = digest
        try:
            document = parse_json_bytes(payload)
            config = parse_config(document, model_window_s=self.model_window_s)
        except ConfigError as error:
            self._report('rejected', 'warning', problems=[p[:_TEXT] for p in error.problems[:MAX_PROBLEMS]])
            return 0
        restart = [f'{section}.{key}' for section, key, attribute in PROCESS_KEYS
                   if getattr(config, attribute) != getattr(self.fixed, attribute)]
        if restart:
            self._report('rejected', 'warning',
                         problems=[f'{name} applies to every call and needs a restart' for name in restart])
            return 0
        if 'caller' not in config.score_directions:
            # Review M1: the tap hears only the caller, so this would silently stop all
            # scoring on new calls while reporting success.
            self._report('rejected', 'warning',
                         problems=['scoring.score_directions must include caller: only the caller is tapped'])
            return 0
        current = self.store.for_new_call()
        if fingerprint(config) == current.fingerprint:
            if not baseline:  # the start-up settings are not news
                self._report('unchanged', 'info', version=current.version, fingerprint=current.fingerprint)
            return 0
        try:
            snapshot = self.store.reload(document, model_window_s=self.model_window_s)
        except ConfigError as error:  # validated above; a last guard keeps the old snapshot
            self._report('rejected', 'warning', problems=[p[:_TEXT] for p in error.problems[:MAX_PROBLEMS]])
            return 0
        self._report('applied', 'info', version=snapshot.version, fingerprint=snapshot.fingerprint)
        return 0

    def _report(self, outcome, level, **fields):
        record = {'event': f'config_{"reloaded" if outcome == "applied" else outcome}', 'level': level, **fields}
        with self._lock:
            self.counts[outcome] += 1
            self.last_outcome = outcome
            if len(self._records) >= MAX_RECORDS:
                self._records.popleft()
                self.counts['records_dropped'] += 1
            self._records.append(record)

    # -- other threads ----------------------------------------------------------------
    def take_records(self):
        """Records not yet logged, oldest first (the operations log drains them)."""
        with self._lock:
            records, self._records = list(self._records), deque()
        return records

    def pending(self):
        return 0

    def report(self):
        snapshot = self.store.for_new_call()
        with self._lock:
            counts = dict(self.counts)
            last = self.last_outcome
        return {'config_reloads': {outcome: counts[outcome] for outcome in OUTCOMES},
                'config_version': snapshot.version, 'config_fingerprint': snapshot.fingerprint,
                'polls': counts['polls'], 'records_dropped': counts['records_dropped'], 'last_outcome': last}
