"""Live AVA service process: tap -> scoring -> events (backlog 8.1 wiring, 6.QA;
PRD FR-O4, FR-I1-FR-I11, FR-T2-FR-T5, FR-A4, FR-A8 readiness; architecture §2.1-§2.4,
§2.3 model loading).

`load_detector(card, onnx, scoring_document, threads=)` is the start-up gate: the model
card must match the ONNX file and the scoring config (`model_card.check_card`, FR-O4),
the session is opened from the hash the card names (CPU only, bounded threads), the
detector mode follows the graph (`per_window` for dynamic INT8, declared by the
caller) and one warm-up batch must give finite logits. Any failure raises; the service
does not start.

`LiveService` owns one EventHub, LiveCalls (with a CallGate), MicroBatcher,
LivePipeline, SpeechFeed (one VAD per call direction from `vad_factory`),
TapScoringSink and TapApp, all driven by ONE thread in `run(stop_event)`:
connect the ARI event stream, `resume()` (sweep leftovers, tap answered calls),
then `run_once` + `pump` until stopped. When the ARI connection drops, every open tap
ends `monitor_lost`, the sink keeps pumping so those calls end cleanly, and the loop
reconnects with bounded backoff (1, 2, 4 ... 30 s). Optional `tick_hooks` run after
each turn on this thread, faults counted: cheap non-blocking work only. Database and
Redis pumps can block on their clients, so they run on their own threads
(`background.PumpThread`, wired by the command line). `ready` is set once the stream is connected (the detector was
warmed before construction). Stopping ends every call `monitor_lost` (architecture
§2.4) and leaves calls themselves untouched (the tap never holds them).
"""
import threading
import time

import numpy as np

from services.events.fanout import EventHub
from services.inference.capacity import CallGate, MicroBatcher
from services.scorer.config import ConfigStore
from services.scorer.live import LiveCalls
from services.scorer.pipeline import LivePipeline
from services.scorer.speech_feed import SpeechFeed
from services.tap.scoring_sink import TapScoringSink
from services.tap.tap_app import TapApp

MAX_BACKOFF_S = 30.0


class StartupError(RuntimeError):
    """The service must not start (model card, model, config or warm-up)."""


class _CallConfigs:
    """`LiveCalls` takes a source whose `for_new_call()` returns a `ScoringConfig`;
    `ConfigStore.for_new_call()` returns a `ConfigSnapshot` (version, config,
    fingerprint). Found when the two first met here (15 Nov); this adapter keeps the
    store's atomic reload semantics and hands each new call its snapshot's config."""

    def __init__(self, store):
        self.store = store

    def for_new_call(self):
        return self.store.for_new_call().config


def dynamic_quantizers(onnx_path):
    """Number of DynamicQuantizeLinear nodes in the graph (dynamic INT8 makes a window's
    score depend on its batch partners). Needs the `onnx` package."""
    import onnx
    return sum(node.op_type == 'DynamicQuantizeLinear' for node in onnx.load(str(onnx_path)).graph.node)


def load_detector(card_path, onnx_path, scoring_document, *, threads=1, per_window, session_factory=None,
                  graph_inspector=dynamic_quantizers):
    """Returns (detector, card, ScoringConfig). Raises StartupError."""
    from models.export.model_card import ModelCardError, check_card
    from services.inference.onnx_detector import OnnxBatchDetector
    from services.scorer.config import ConfigError, parse_config
    try:
        card = check_card(card_path, onnx_path, scoring_document)
        config = parse_config(scoring_document, model_window_s=card['window_s'])
    except (ModelCardError, ConfigError) as error:
        raise StartupError(f'refusing to start: {error}') from None
    if card['calibration_status'] == 'calibrated':
        # The serving path does not apply a calibration temperature yet: a calibrated
        # card would serve raw scores against calibrated thresholds (review H2).
        raise StartupError('refusing to start: calibrated cards need the temperature applied in serving (not built)')
    try:
        dynamic = graph_inspector(onnx_path)
    except Exception as error:
        raise StartupError(f'refusing to start: cannot inspect the graph: {type(error).__name__}') from None
    if dynamic and not per_window:
        # Review H1: dynamic INT8 output depends on batch partners (14 Nov finding).
        raise StartupError('refusing to start: a dynamic-INT8 graph must be served per window (per_window=True)')
    if session_factory is None:
        from models.export.onnx_export import open_session

        def session_factory(path, sha256):
            return open_session(path, threads=threads, expected_sha256=sha256)
    try:
        session = session_factory(onnx_path, card['onnx']['sha256'])
        detector = OnnxBatchDetector(session, per_window=per_window)
        logits = np.asarray(detector(np.zeros((1, card['window_samples']), np.float32)))
    except Exception as error:  # load or warm-up failure: never start half-ready
        raise StartupError(f'refusing to start: model load or warm-up failed: {type(error).__name__}: {error}') from None
    if logits.shape != (1, 2) or not np.isfinite(logits).all():
        raise StartupError('refusing to start: warm-up did not give one finite pair of logits')
    return detector, card, config


class LiveService:
    def __init__(self, ari, detector, config, vad_factory, *, receiver_host, bind_host='0.0.0.0', max_calls=8,
                 capacity_windows_per_s=4.0, tap_extensions=None, rtp_source=None, hub=None, clock=time.monotonic,
                 tick_hooks=(), sleep=time.sleep):
        self.ari, self.clock, self._sleep = ari, clock, sleep
        self.hub = hub or EventHub()
        self.configs = ConfigStore(config)
        self.live = LiveCalls(_CallConfigs(self.configs), self.hub, CallGate(max_calls))
        batcher = MicroBatcher(capacity_windows_per_s=capacity_windows_per_s, max_pending_live=64)
        self.pipeline = LivePipeline(self.live, batcher, detector, config)
        self.feed = SpeechFeed(self.pipeline, vad_factory)
        self.sink = TapScoringSink(self.live, self.feed, self.pipeline, clock, hub=self.hub)
        # The tap admits no more calls than scoring can take (CallGate = max_calls).
        self.tap = TapApp(ari, self.sink, receiver_host=receiver_host, bind_host=bind_host, max_calls=max_calls,
                          tap_extensions=tap_extensions, rtp_source=rtp_source, clock=clock)
        self.tick_hooks = tuple(tick_hooks)
        self.ready = threading.Event()
        self.counts = {'connects': 0, 'disconnects': 0, 'hook_errors': 0, 'loop_errors': 0, 'shutdown_errors': 0}

    def _tick(self):
        try:
            self.sink.pump()
        except Exception:  # the sink contains its own faults; this is a last guard
            self.counts['loop_errors'] += 1
        for hook in self.tick_hooks:
            try:
                hook()
            except Exception:
                self.counts['hook_errors'] += 1

    def run(self, stop_event, *, poll_s=0.05):
        """Serve until `stop_event` is set. Never raises for connection or loop faults.
        One service object serves one run."""
        backoff = 1.0
        try:
            while not stop_event.is_set():
                try:
                    with self.ari.events() as connection:
                        self._connection = connection
                        self.counts['connects'] += 1
                        self.tap.resume()
                        self.ready.set()
                        backoff = 1.0
                        while not stop_event.is_set():
                            self.tap.run_once(connection, timeout=poll_s)
                            self._tick()
                except (ConnectionError, OSError, TimeoutError):  # refused or dropped
                    if not stop_event.is_set():  # `stop()` closing the stream is not a drop
                        self.counts['disconnects'] += 1
                except Exception as error:  # websockets ConnectionClosed lands here too
                    name = type(error).__name__
                    if 'Connection' in name or 'Closed' in name:
                        if not stop_event.is_set():
                            self.counts['disconnects'] += 1
                    else:
                        self.counts['loop_errors'] += 1
                finally:
                    self._connection = None
                    self.ready.clear()
                    self._guarded(self.tap.close)  # open taps end monitor_lost; calls go on
                    self._tick()
                if stop_event.is_set():
                    break
                deadline = self.clock() + backoff
                while not stop_event.is_set() and self.clock() < deadline:
                    self._tick()  # keep ending calls while reconnecting
                    self._sleep(min(poll_s * 4, 0.2))
                backoff = min(backoff * 2, MAX_BACKOFF_S)
        finally:
            # Calls that ended normally keep their own reason (review M3); the rest end
            # monitor_lost (architecture §2.4).
            self._guarded(self.sink.flush_ends)
            self._guarded(lambda: self.live.end_all('monitor_lost', speech_seconds_for=self.feed.speech_seconds_for))

    def _guarded(self, action):
        try:
            action()
        except Exception:
            self.counts['shutdown_errors'] += 1

    def stop(self, stop_event):
        """Set the stop event and close the live connection so `run` returns promptly."""
        stop_event.set()
        connection = getattr(self, '_connection', None)
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
