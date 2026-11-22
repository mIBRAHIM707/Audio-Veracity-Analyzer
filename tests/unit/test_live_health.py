"""Claude-authored tests (16 November 2026) for services/live/health.py, the live
process's /healthz, /readyz and /metrics (backlog 7.2/8.1; PRD FR-A8, FR-O6).
Authored by Claude (Claude Code), not by GPT-6 Astra. A real LiveService with the fake
ARI, fake ONNX session and amplitude VAD of test_live_service, a manual clock, and the
real standard-library HTTP server on a loopback port.
"""
import json
import re
import threading
import unittest
from urllib import error as urlerror, request

from services.events.fanout import EventHub
from services.inference.onnx_detector import OnnxBatchDetector
from services.live.background import PumpThread
from services.live.health import HealthServer
from services.live.service import LiveService
from services.scorer.config import ScoringConfig
from services.store.events_pg import StoreError
from services.store.pump import StorePump
from tests.unit.test_live_service import Connection, FakeAri, marked_up
from tests.unit.test_onnx_detector import FakeSession
from tests.unit.test_speech_feed import AmplitudeVad

SAMPLE = re.compile(r'^[a-zA-Z_:][a-zA-Z0-9_:]*(\{[a-zA-Z_][a-zA-Z0-9_]*="[^"\\\n]*"'
                    r'(,[a-zA-Z_][a-zA-Z0-9_]*="[^"\\\n]*")*\})? -?[0-9.e+-]+$')


class ManualClock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def make_service(clock, max_calls=3):
    return LiveService(FakeAri([]), OnnxBatchDetector(FakeSession(), per_window=True), ScoringConfig(), AmplitudeVad,
                       receiver_host='127.0.0.1', bind_host='127.0.0.1', clock=clock, max_calls=max_calls)


def samples(text):
    """Parse exposition text: every non-comment line must be a valid sample."""
    values = {}
    for line in text.splitlines():
        if not line or line.startswith('#'):
            continue
        assert SAMPLE.match(line), line
        name, value = line.rsplit(' ', 1)
        values[name] = float(value)
    return values


class ReadinessTests(unittest.TestCase):
    def test_ready_needs_the_ari_stream_and_a_turning_loop(self):
        clock = ManualClock()
        service = make_service(clock)
        health = HealthServer(service, stale_after_s=5, port=0)
        self.addCleanup(health.stop)
        self.assertEqual(health.readiness()[:2], (False, 'ari_disconnected'))
        service.ready.set()
        self.assertEqual(health.readiness()[:2], (False, 'loop_not_started'))
        service._tick()  # one loop turn publishes the pipeline snapshot
        ready, reason, details = health.readiness()
        self.assertEqual((ready, reason, details['loop_snapshot_age_s'], details['detector_failure_run']),
                         (True, None, 0.0, 0))
        clock.t += 5.0
        self.assertTrue(health.readiness()[0])  # exactly at the limit: still ready
        clock.t += 0.001
        self.assertEqual(health.readiness()[:2], (False, 'loop_stalled'))  # e.g. a hung detector
        service._tick()
        self.assertTrue(health.readiness()[0])
        service.ready.clear()  # stream dropped
        self.assertEqual(health.readiness()[:2], (False, 'ari_disconnected'))

    def test_a_real_loop_turning_keeps_it_ready_and_stopping_makes_it_unready(self):
        clock = ManualClock()
        service = LiveService(FakeAri([Connection([marked_up('c1')], then_fail=False)]),
                              OnnxBatchDetector(FakeSession(), per_window=True), ScoringConfig(), AmplitudeVad,
                              receiver_host='127.0.0.1', bind_host='127.0.0.1', clock=clock)
        health = HealthServer(service, port=0)
        self.addCleanup(health.stop)
        stop, seen = threading.Event(), []

        def hook():
            seen.append(health.readiness()[:2])
            clock.t += 0.05
            if len(seen) > 30:
                stop.set()
        service.tick_hooks = (hook,)
        service.run(stop, poll_s=0.01)
        self.assertIn((True, None), seen[2:])  # connected and turning
        self.assertEqual(health.readiness()[:2], (False, 'ari_disconnected'))  # after stop

    def test_a_detector_failing_every_batch_makes_it_unready(self):
        # Review M1: a detector that fails fast keeps the loop turning; readiness must see it.
        clock = ManualClock()
        service = make_service(clock)
        service.ready.set()
        health = HealthServer(service, port=0)
        self.addCleanup(health.stop)
        counts = service.pipeline.counts
        service._tick()
        self.assertTrue(health.readiness()[0])
        for failed in range(1, 4):  # three batches, each failing
            counts['batches'] += 1
            counts['detector_failures'] += 1
            service._tick()
            expected = (True, None) if failed < 3 else (False, 'detector_failing')
            self.assertEqual(health.readiness()[:2], expected, failed)
        self.assertEqual(samples(health.metrics())['ava_live_detector_failure_run'], 3)
        counts['batches'] += 2  # two batches, one failing: a success resets the run
        counts['detector_failures'] += 1
        service._tick()
        self.assertEqual((health.readiness()[:2], service.detector_failure_run), ((True, None), 0))
        service._tick()  # no new batches: nothing changes
        self.assertEqual(service.detector_failure_run, 0)

    def test_a_real_detector_failure_counts_through_the_pipeline(self):
        # The run is driven by the pipeline's own counters: a session that raises.
        import numpy as np

        def broken(part):
            raise RuntimeError('onnxruntime fault')
        clock = ManualClock()
        service = LiveService(FakeAri([]), OnnxBatchDetector(FakeSession(result=broken), per_window=True),
                              ScoringConfig(), AmplitudeVad, receiver_host='127.0.0.1', bind_host='127.0.0.1',
                              clock=clock)
        service.ready.set()
        health = HealthServer(service, port=0)
        self.addCleanup(health.stop)
        self.assertTrue(service.live.start('c1'))
        window = np.full(64000, 0.1, np.float32)
        for index in range(12):
            service.pipeline.offer('c1', 'caller', index, window, clock.t)
            clock.t += 2.0
            service.pipeline.step(clock.t)
            service._tick()
        self.assertGreaterEqual(service.pipeline.counts['detector_failures'], 3)
        self.assertEqual(service.pipeline.counts['scored'], 0)
        self.assertEqual(health.readiness()[:2], (False, 'detector_failing'))

    def test_validation(self):
        service = make_service(ManualClock())
        for kwargs in ({'stale_after_s': 0}, {'stale_after_s': 301}, {'stale_after_s': True}, {'port': -1},
                       {'port': 70000}, {'port': '80'}, {'max_connections': 0}, {'client_timeout_s': 0}):
            with self.assertRaises(ValueError, msg=kwargs):
                HealthServer(service, **kwargs)


class MetricsTests(unittest.TestCase):
    def test_metrics_are_valid_prometheus_text_without_call_ids(self):
        clock = ManualClock()
        service = make_service(clock, max_calls=3)
        service.ready.set()
        self.assertTrue(service.live.start('caller-0300-1234567', **{'from': '03001234567'}))
        service._tick()
        health = HealthServer(service, port=0)
        self.addCleanup(health.stop)
        text = health.metrics()
        self.assertNotIn('0300', text)  # no call IDs or numbers
        values = samples(text)
        self.assertEqual(values['ava_live_ready'], 1)
        self.assertEqual((values['ava_live_calls_active'], values['ava_live_calls_capacity']), (1, 3))
        self.assertEqual(values['ava_live_pipeline_snapshot_valid'], 1)
        self.assertIn('ava_live_windows_offered_total{outcome="accepted"}', values)
        self.assertIn('ava_live_inference_seconds_count{outcome="ok"}', values)
        self.assertEqual(values['ava_live_ari_disconnects_total'], 0)
        self.assertIn('ava_live_tap_events_total{kind="tapped"}', values)
        self.assertIn('ava_live_sink_events_total{kind="audio_unfed"}', values)
        self.assertEqual(values['ava_live_metrics_render_errors_total'], 0)
        self.assertNotIn('ava_live_pump_alive{pump="store"}', values)  # no pumps configured
        # Each family has exactly one HELP and TYPE.
        names = re.findall(r'^# TYPE (\S+) ', text, re.M)
        self.assertEqual(len(names), len(set(names)))

    def test_exported_kinds_are_exactly_the_receiver_and_jitter_stats_keys(self):
        # Claude-authored (16 Nov 2026, review finding): a renamed or new stats key must
        # fail here rather than export a stale 0 forever.
        # 18 Nov: the hold-time stats are exported by their own histogram family instead.
        from services.live.health import HOLD_STATS, JITTER_COUNTERS, RECEIVER_COUNTERS
        from services.tap.jitter import JitterBuffer
        from services.tap.rtp_receiver import RtpSession
        session = RtpSession(host='127.0.0.1', jitter=JitterBuffer(depth_s=0.06))
        self.addCleanup(session.close)
        stats = session.receiver_stats()
        self.assertEqual(set(RECEIVER_COUNTERS), {key for key in stats if not key.startswith('jitter_')})
        self.assertFalse(set(JITTER_COUNTERS) & set(HOLD_STATS))
        self.assertEqual({f'jitter_{key}' for key in JITTER_COUNTERS + HOLD_STATS},
                         {key for key in stats if key.startswith('jitter_')})
        self.assertEqual(len(RECEIVER_COUNTERS) + len(JITTER_COUNTERS) + len(HOLD_STATS), len(stats))

    def test_rtp_receiver_and_jitter_totals_are_exported(self):
        # Claude-authored (16 Nov 2026): every known kind is present (0 until a call ends),
        # then the tap's totals, incl. dropped_handover (re-review suggestion, 6.3).
        service = make_service(ManualClock())
        health = HealthServer(service, port=0)
        self.addCleanup(health.stop)
        values = samples(health.metrics())
        self.assertEqual(values['ava_live_rtp_receiver_total{kind="packets"}'], 0)
        self.assertEqual(values['ava_live_jitter_buffer_total{kind="dropped_handover"}'], 0)
        service.tap._add_receiver_totals({'packets': 500, 'gaps': 3, 'jitter_concealed_frames': 40,
                                          'jitter_dropped_handover': 7, 'unknown_new_key': 9})
        service.tap._add_receiver_totals({'packets': 20, 'jitter_dropped_handover': 1})
        text = health.metrics()
        values = samples(text)
        self.assertEqual((values['ava_live_rtp_receiver_total{kind="packets"}'],
                          values['ava_live_rtp_receiver_total{kind="gaps"}'],
                          values['ava_live_jitter_buffer_total{kind="concealed_frames"}'],
                          values['ava_live_jitter_buffer_total{kind="dropped_handover"}']), (520, 3, 40, 8))
        self.assertNotIn('unknown_new_key', text)  # only the fixed kinds are exported
        self.assertEqual(values['ava_live_metrics_render_errors_total'], 0)
        # Corrupted: the family alone is dropped. Since 18 Nov /metrics reads `receiver_live`
        # (ended plus in-call counts; `_add_receiver_totals` republishes it).
        service.tap.receiver_live = {'packets': float('nan')}
        values = samples(health.metrics())
        self.assertNotIn('ava_live_rtp_receiver_total{kind="packets"}', values)
        self.assertIn('ava_live_tap_events_total{kind="tapped"}', values)
        self.assertEqual(values['ava_live_metrics_render_errors_total'], 1)

    def test_a_bad_family_is_dropped_and_counted_not_the_endpoint(self):
        clock = ManualClock()
        service = make_service(clock)
        service._tick()
        service.counts['disconnects'] = float('nan')  # a corrupted counter
        service.pipeline_stats.publish({'counts': {}})  # a malformed snapshot
        health = HealthServer(service, port=0)
        self.addCleanup(health.stop)
        values = samples(health.metrics())
        self.assertNotIn('ava_live_ari_disconnects_total', values)
        self.assertEqual(values['ava_live_pipeline_snapshot_valid'], 0)
        self.assertEqual(values['ava_live_metrics_render_errors_total'], 2)
        self.assertIn('ava_live_calls_active', values)  # other families still there

    def test_pump_families_show_a_store_outage(self):
        clock = ManualClock()
        service = make_service(clock)
        hub = service.hub

        class DownStore:
            def record(self, envelope):
                raise StoreError('database down', retryable=True)
        subscription = hub.subscribe(buffer=64)
        store_pump = StorePump(subscription, DownStore())
        thread = PumpThread('store', store_pump.pump, pending=lambda: store_pump.backlog + subscription.buffered())
        pumps = [{'thread': thread, 'resource': None,
                  'report': lambda: {'counts': dict(store_pump.counts), 'backlog': store_pump.backlog,
                                     'consecutive_failures': store_pump.consecutive_failures}}]
        for i in range(3):
            hub.publish('call.started', f'c{i}', {})
        store_pump.pump()
        store_pump.pump()
        health = HealthServer(service, pumps=pumps, port=0)
        self.addCleanup(health.stop)
        values = samples(health.metrics())
        self.assertEqual(values['ava_live_store_backlog'], 3)
        self.assertEqual(values['ava_live_store_consecutive_failures'], 2)
        self.assertEqual(values['ava_live_store_events_total{outcome="stored"}'], 0)
        self.assertEqual((values['ava_live_pump_alive{pump="store"}'], values['ava_live_pump_stuck{pump="store"}']),
                         (0, 0))
        self.assertNotIn('ava_live_store_superseded_total', values)  # no such count reported
        # Claude-authored, 19 Nov (5.QA): the PostgreSQL store's supersede count is exported.
        pumps[0]['report'] = lambda: {'counts': dict(store_pump.counts), 'backlog': store_pump.backlog,
                                      'consecutive_failures': store_pump.consecutive_failures, 'superseded': 2}
        self.assertEqual(samples(health.metrics())['ava_live_store_superseded_total'], 2)


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.clock = ManualClock()
        self.service = make_service(self.clock)
        self.health = HealthServer(self.service, host='127.0.0.1', port=0)
        self.health.start()
        self.addCleanup(self.health.stop)
        host, port = self.health.address
        self.base = f'http://{host}:{port}'

    def get(self, path, method='GET'):
        try:
            with request.urlopen(request.Request(self.base + path, method=method), timeout=5) as response:
                return response.status, response.headers, response.read().decode()
        except urlerror.HTTPError as failure:
            return failure.code, failure.headers, failure.read().decode()

    def test_routes(self):
        status, _, body = self.get('/healthz')
        self.assertEqual((status, json.loads(body)), (200, {'status': 'alive'}))
        status, _, body = self.get('/readyz')
        self.assertEqual((status, json.loads(body)['reason']), (503, 'ari_disconnected'))
        self.service.ready.set()
        self.service._tick()
        status, _, body = self.get('/readyz?verbose=1')
        self.assertEqual((status, json.loads(body)['ready']), (200, True))
        status, headers, body = self.get('/metrics')
        self.assertEqual(status, 200)
        self.assertTrue(headers['Content-Type'].startswith('text/plain; version=0.0.4'))
        self.assertEqual(samples(body)['ava_live_ready'], 1)
        self.assertEqual(self.get('/v1/calls')[0], 404)
        for method in ('POST', 'DELETE', 'HEAD', 'OPTIONS', 'PUT'):  # review L4: never 501
            status, headers, _ = self.get('/metrics', method=method)
            self.assertEqual((status, headers['Allow']), (405, 'GET'), method)
        self.assertGreaterEqual(self.health.counts['requests'], 5)
        self.assertGreaterEqual(samples(self.get('/metrics')[2])['ava_live_health_requests_total'], 5)

    def test_idle_clients_are_dropped_and_connections_are_capped(self):
        # Review M3: a client that sends nothing cannot hold a thread for ever, and extra
        # connections beyond the cap are closed at once.
        import socket
        import time
        self.health.stop()
        health = HealthServer(self.service, port=0, max_connections=2, client_timeout_s=0.5)
        health.start()
        self.addCleanup(health.stop)
        host, port = health.address
        idle = [socket.create_connection((host, port), timeout=5) for _ in range(2)]
        for sock in idle:
            self.addCleanup(sock.close)
        time.sleep(0.2)  # both slots taken by silent clients
        extra = socket.create_connection((host, port), timeout=5)
        self.addCleanup(extra.close)
        self.assertEqual(extra.recv(1), b'')  # closed without a response
        started = time.monotonic()
        self.assertEqual(idle[0].recv(1024), b'')  # dropped after the client timeout
        self.assertLess(time.monotonic() - started, 4.0)
        time.sleep(0.3)
        with request.urlopen(f'http://{host}:{port}/healthz', timeout=5) as response:  # slots free again
            self.assertEqual(response.status, 200)
        self.assertGreaterEqual(health.counts['refused_connections'], 1)

    def test_concurrent_scrapes_while_the_loop_turns(self):
        self.service.ready.set()
        stop, errors = threading.Event(), []

        def loop():
            while not stop.is_set():
                self.service._tick()
                self.clock.t += 0.01

        def scrape():
            try:
                for _ in range(20):
                    status, _, body = self.get('/metrics')
                    assert status == 200
                    samples(body)
            except Exception as failure:  # collected for the main thread
                errors.append(repr(failure))
        turning = threading.Thread(target=loop)
        turning.start()
        scrapers = [threading.Thread(target=scrape) for _ in range(4)]
        for thread in scrapers:
            thread.start()
        for thread in scrapers:
            thread.join(30)
        stop.set()
        turning.join(5)
        self.assertFalse(any(thread.is_alive() for thread in scrapers + [turning]))  # review: none hung
        self.assertEqual(errors, [])

    def test_stop_frees_the_port(self):
        host, port = self.health.address
        self.assertEqual(self.get('/healthz')[0], 200)  # leaves a closed connection behind (TIME_WAIT)
        self.health.stop()
        again = HealthServer(self.service, host=host, port=port)  # the same port binds again
        again.stop()
        with self.assertRaises(urlerror.URLError):
            request.urlopen(self.base + '/healthz', timeout=2)


class CommandLineTests(unittest.TestCase):
    def test_health_port_is_bound_at_start_up_reported_and_freed(self):
        import socket

        from services.live.__main__ import EXIT_OK, EXIT_REFUSED
        from tests.unit.test_live_cli import BASE, Harness, run
        with socket.socket() as probe:  # a free port to ask for
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        harness = Harness()
        seen = []
        harness.on_run = lambda stop: (seen.append(_status(port, '/healthz')), stop.set())
        code, lines, _ = run(BASE + ['--health-port', str(port)], harness)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(lines[0]['health'], {'host': '127.0.0.1', 'port': port})
        self.assertEqual(seen, [200])  # served while running
        with self.assertRaises(urlerror.URLError):  # closed at stop
            request.urlopen(f'http://127.0.0.1:{port}/healthz', timeout=2)
        with socket.socket() as busy:  # a port in use refuses the start
            busy.bind(('127.0.0.1', 0))
            busy.listen()
            taken = busy.getsockname()[1]
            harness = Harness()
            code, lines, _ = run(BASE + ['--health-port', str(taken)], harness)
        self.assertEqual((code, lines[0]['event']), (EXIT_REFUSED, 'refused'))
        self.assertIn('health endpoint', lines[0]['reason'])
        for bad in ('0', '65536', 'x'):
            with self.subTest(bad), self.assertRaises(SystemExit):
                import contextlib
                import io
                with contextlib.redirect_stderr(io.StringIO()):
                    run(BASE + ['--health-port', bad], Harness())


def _status(port, path):
    with request.urlopen(f'http://127.0.0.1:{port}{path}', timeout=5) as response:
        return response.status


if __name__ == '__main__':
    unittest.main()
