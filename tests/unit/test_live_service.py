"""Claude-authored tests (15 November 2026) for services/live/service.py (backlog 8.1
wiring; PRD FR-O4 start-up refusal, FR-T2/FR-T4 reconnect, FR-A4). Authored by Claude
(Claude Code), not by GPT-6 Astra. A fake ONNX session (from test_onnx_detector), a
fake ARI and the amplitude VAD of test_speech_feed; no model, no Asterisk.
"""
import json
from pathlib import Path
import tempfile
import threading
import unittest

import numpy as np

from models.export import model_card as mc
from services.live.service import LiveService, StartupError, load_detector
from tests.unit.test_model_card import fields
from tests.unit.test_onnx_detector import FakeSession
from tests.unit.test_speech_feed import AmplitudeVad


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.onnx = Path(self.temp.name) / 'ava-test.onnx'
        self.onnx.write_bytes(b'stand-in' * 64)
        self.card = mc.write_card(mc.build_card(self.onnx, **fields()), Path(self.temp.name) / 'model_card.json')

    def test_card_hash_and_warm_up_gate_the_start(self):
        seen = []

        def factory(path, sha256):
            seen.append(sha256)
            return FakeSession()
        detector, card, config = load_detector(self.card, self.onnx, {}, per_window=True, session_factory=factory,
                                               graph_inspector=STATIC)
        self.assertEqual(seen, [card['onnx']['sha256']])  # opened from the hash the card names
        self.assertEqual(config.window_s, 4.0)
        self.assertEqual(detector(np.zeros((2, 64000), np.float32)).shape, (2, 2))

    def test_every_failure_refuses_to_start(self):
        cases = {
            'config disagrees': dict(scoring_document={'audio': {'window_s': 3.0, 'hop_s': 1.0}}),
            'session fails': dict(session_factory=lambda p, s: (_ for _ in ()).throw(RuntimeError('no ort'))),
            'non-finite warm-up': dict(session_factory=lambda p, s: FakeSession(
                result=lambda part: np.full((len(part), 2), np.nan, np.float32))),
            'wrong graph': dict(session_factory=lambda p, s: FakeSession(inputs=[])),
        }
        for label, overrides in cases.items():
            options = {'scoring_document': {}, 'session_factory': lambda p, s: FakeSession(),
                       'graph_inspector': STATIC, **overrides}
            with self.assertRaises(StartupError, msg=label):
                load_detector(self.card, self.onnx, options.pop('scoring_document'), per_window=False, **options)
        with self.assertRaisesRegex(StartupError, 'not found'):
            load_detector(self.card, Path(self.temp.name) / 'absent.onnx', {}, per_window=False,
                          session_factory=lambda p, s: FakeSession(), graph_inspector=STATIC)

    def test_dynamic_int8_graph_must_be_served_per_window(self):
        # Review H1: the gate reads the graph instead of trusting the caller's flag.
        dynamic = lambda path: 3  # three DynamicQuantizeLinear nodes
        with self.assertRaisesRegex(StartupError, 'per window'):
            load_detector(self.card, self.onnx, {}, per_window=False, session_factory=lambda p, s: FakeSession(),
                          graph_inspector=dynamic)
        load_detector(self.card, self.onnx, {}, per_window=True, session_factory=lambda p, s: FakeSession(),
                      graph_inspector=dynamic)  # allowed per window
        with self.assertRaisesRegex(StartupError, 'cannot inspect'):
            load_detector(self.card, self.onnx, {}, per_window=True, session_factory=lambda p, s: FakeSession(),
                          graph_inspector=lambda path: (_ for _ in ()).throw(ValueError('not onnx')))

    def test_calibrated_card_is_refused_until_serving_applies_the_temperature(self):
        # Review H2: a calibrated card would pair raw scores with calibrated thresholds.
        card = mc.write_card(mc.build_card(self.onnx, **fields(), calibration_temperature=1.7, ece=0.031),
                             Path(self.temp.name) / 'calibrated.json')
        with self.assertRaisesRegex(StartupError, 'temperature'):
            load_detector(card, self.onnx, {}, per_window=True, session_factory=lambda p, s: FakeSession(),
                          graph_inspector=STATIC)


STATIC = lambda path: 0  # a graph with no DynamicQuantizeLinear (no onnx package needed)


class Connection:
    def __init__(self, events, then_fail=True):
        self.events, self.then_fail, self.closed = [json.dumps(e) for e in events], then_fail, False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False

    def recv(self, timeout=None):
        if self.events:
            return self.events.pop(0)
        if self.then_fail:
            raise ConnectionError('ARI websocket dropped')
        raise TimeoutError()

    def close(self):
        self.closed = True


class FakeAri:
    def __init__(self, connections):
        self.connections, self.calls, self.counter = list(connections), [], 0

    def events(self):
        if not self.connections:
            raise ConnectionError('refused')
        return self.connections.pop(0)

    def _id(self, name):
        self.counter += 1
        self.calls.append(name)
        return {'id': f'{name}-{self.counter}'}

    def snoop(self, channel_id, spy='in', app_args=''):
        return self._id('snoop')

    def external_media(self, host_port, fmt='ulaw'):
        return self._id('xm')

    def create_bridge(self, name=''):
        return self._id('bridge')

    def add_to_bridge(self, bridge_id, channels):
        self.calls.append('add')

    def hangup(self, channel_id):
        self.calls.append('hangup')

    def destroy_bridge(self, bridge_id):
        self.calls.append('destroy')

    def list_bridges(self):
        return []

    def list_channels(self):
        return []

    def get_variable(self, channel_id, name):
        return None


def marked_up(channel_id):
    return {'type': 'ChannelVarset', 'variable': 'AVA_TAP', 'value': 'caller',
            'channel': {'id': channel_id, 'name': f'PJSIP/a-{channel_id}', 'state': 'Up',
                        'caller': {'number': 'tok'}, 'dialplan': {'exten': '700'}}}


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        self.t += 0.01
        return self.t


class RunLoopTests(unittest.TestCase):
    def test_drop_ends_taps_reconnects_and_stop_ends_every_call(self):
        from services.scorer.config import ScoringConfig
        from services.inference.onnx_detector import OnnxBatchDetector
        ari = FakeAri([Connection([marked_up('c1')]), Connection([marked_up('c1')], then_fail=False)])
        clock = Clock()
        stop = threading.Event()
        service = LiveService(ari, OnnxBatchDetector(FakeSession(), per_window=True), ScoringConfig(), AmplitudeVad,
                              receiver_host='127.0.0.1', bind_host='127.0.0.1', clock=clock,
                              sleep=lambda s: setattr(clock, 't', clock.t + s))
        listener = service.hub.subscribe(buffer=1024)
        ticks = []

        def hook():
            ticks.append(service.counts['connects'])
            if service.counts['connects'] == 2 and len([t for t in ticks if t == 2]) > 20:
                stop.set()
        service.tick_hooks = (hook,)
        service.run(stop, poll_s=0.01)
        self.assertEqual((service.counts['connects'], service.counts['disconnects']), (2, 1))
        self.assertFalse(service.ready.is_set())
        events, _ = listener.drain()
        types = [(e.type, e.data.get('reason')) for e in events if e.type in ('call.started', 'call.ended')]
        # First tap lost with the connection (monitor_lost), re-tapped after the
        # reconnect, ended monitor_lost again when the service stops.
        self.assertEqual(types, [('call.started', None), ('call.ended', 'monitor_lost'),
                                 ('call.started', None), ('call.ended', 'monitor_lost')],
                         (service.tap.stats, service.sink.counts, [e.type for e in events]))
        self.assertEqual(service.live.snapshot()['calls'], {})
        self.assertEqual(service.counts['hook_errors'], 0)

    def service(self, connections):
        from services.scorer.config import ScoringConfig
        from services.inference.onnx_detector import OnnxBatchDetector
        clock = Clock()
        return LiveService(FakeAri(connections), OnnxBatchDetector(FakeSession(), per_window=True), ScoringConfig(),
                           AmplitudeVad, receiver_host='127.0.0.1', bind_host='127.0.0.1', clock=clock,
                           sleep=lambda s: setattr(clock, 't', clock.t + s))

    def test_a_queued_end_keeps_its_own_reason_on_stop(self):
        # Review M3: a call that ended `bye` but still had windows queued when the
        # service stopped is reported `bye`, not `monitor_lost`.
        hangup = {'type': 'ChannelHangupRequest', 'channel': {'id': 'c1'}}
        service = self.service([Connection([marked_up('c1'), hangup], then_fail=False)])
        listener = service.hub.subscribe(buffer=1024)
        stop = threading.Event()
        service.tick_hooks = (lambda: stop.set() if 'c1' in service.sink._ending else None,)
        service.sink.end_grace_s = 1e9  # the end stays queued until stop
        service.pipeline.has_work = lambda call_id: True  # windows still waiting
        service.run(stop, poll_s=0.01)
        events, _ = listener.drain()
        ended = [e.data['reason'] for e in events if e.type == 'call.ended']
        self.assertEqual(ended, ['bye'], (service.sink.counts, service.tap.stats))

    def test_stop_closing_the_stream_is_not_a_disconnect(self):
        # Found by the 15 Nov detector-fault lab test: `stop()` closes the websocket, the
        # blocked read then raises, and that was counted as a dropped connection.
        class ClosedByStop(Exception):
            pass
        ClosedByStop.__name__ = 'ConnectionClosedOK'  # what websockets raises after close()
        for error in (ConnectionError('closed'), ClosedByStop()):
            with self.subTest(type(error).__name__):
                connection = Connection([], then_fail=False)
                service = self.service([connection])
                stop = threading.Event()

                def read_after_stop(timeout=None, error=error):
                    if service.ready.is_set():
                        service.stop(stop)
                        raise error
                    raise TimeoutError()
                connection.recv = read_after_stop
                service.run(stop, poll_s=0.01)
                self.assertEqual((service.counts['connects'], service.counts['disconnects'],
                                  service.counts['loop_errors']), (1, 0, 0))

    def test_loop_faults_and_shutdown_faults_are_counted_not_raised(self):
        # Review M1/M2: a programming fault is a loop error, not a disconnect, and a
        # fault while closing taps does not stop the remaining shutdown steps.
        service = self.service([Connection([], then_fail=False)])
        stop = threading.Event()

        def broken(connection, timeout):
            stop.set()
            raise KeyError('bug')
        service.tap.run_once = broken
        service.tap.close = lambda: (_ for _ in ()).throw(RuntimeError('close failed'))
        service.run(stop, poll_s=0.01)
        self.assertEqual((service.counts['loop_errors'], service.counts['disconnects']), (1, 0))
        self.assertGreaterEqual(service.counts['shutdown_errors'], 1)
        self.assertEqual(service.live.snapshot()['calls'], {})


if __name__ == '__main__':
    unittest.main()
