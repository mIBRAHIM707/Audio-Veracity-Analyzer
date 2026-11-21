"""Claude-authored tests (20 November 2026) for the detected call codec (PRD FR-U5 "call
detail view with codec detected"; backlog 7.3).

Authored by Claude (Claude Code), not by GPT-6 Astra. The codec travels:
TapApp reads `CHANNEL(audionativeformat)` over ARI once the tap is set up
(`services/tap/tap_app.py`) -> `TapScoringSink.started` -> `LiveCalls.start` ->
`call.started` data `codec` -> `PostgresEventStore` `start_data` -> history reader
`codec` (`services/store/history.py`) -> dashboard call detail (`dashboard/src/history.ts`,
Playwright HB08). These tests cover the parsing rule, the tap's read and its failure
paths, the real sink/LiveCalls/hub publication, the reader's mapping of stored values,
the live `/metrics` counter, and that the three copies of the name rule agree. The
PostgreSQL round trip is in `test_store_history.py` (`test_detected_codec_...`) and the
real Asterisk read in `tests/lab/test_ari_tap.py`.
"""
from pathlib import Path
import re
import unittest

from services.store import history as history_module
from services.tap import tap_app
from services.tap.scoring_sink import TapScoringSink
from services.tap.tap_app import NATIVE_FORMAT_VARIABLE, TapApp, native_codec
from tests.unit.test_speech_feed import make, scores
from tests.unit.test_tap import FakeAri, Sink, answered
from tests.unit.test_tap_scoring_sink import Clock

ROOT = Path(__file__).resolve().parents[2]


class NativeCodecTests(unittest.TestCase):
    def test_asterisk_values_become_format_names(self):
        for value, expected in (('(alaw)', 'alaw'), ('(ulaw)', 'ulaw'), ('(gsm)', 'gsm'), ('(g722)', 'g722'),
                                ('(slin16)', 'slin16'), ('(ULAW)', 'ulaw'), (' (gsm) ', 'gsm'), ('alaw', 'alaw'),
                                ('(ulaw|alaw)', 'ulaw|alaw'), ('(a|b|c|d)', 'a|b|c|d'),
                                ('(' + 'x' * 24 + ')', 'x' * 24)):
            self.assertEqual(native_codec(value), expected, value)

    def test_anything_else_is_not_detected(self):
        for value in (None, '', '()', '( )', '(al aw)', '(alaw;x)', '(alaw', 'alaw)x', '(a|b|c|d|e)', '(|alaw)',
                      '(alaw|)', '(5)', '(9alaw)', '(' + 'x' * 25 + ')', '(alaw)' * 30, '(ālaw)', '(<b>)',
                      '(alaw\n)x', 5, b'(alaw)', ['alaw'], {'value': 'alaw'}):
            self.assertIsNone(native_codec(value), repr(value))


class TapReadTests(unittest.TestCase):
    def make(self, fail=None, formats=None, ari=None):
        self.ari, self.sink = ari or FakeAri(fail), Sink()
        if formats is not None:
            self.ari.formats = formats
        app = TapApp(self.ari, self.sink, receiver_host='127.0.0.1', bind_host='127.0.0.1', jitter_depth_s=None)
        self.addCleanup(app.close)
        return app

    def test_the_codec_is_read_once_after_the_tap_is_set_up(self):
        app = self.make(formats={'c1': '(alaw)'})
        app.handle(answered('c1'))
        reads = [c for c in self.ari.calls if c[0] == 'get_variable']
        self.assertEqual(reads, [('get_variable', 'c1', NATIVE_FORMAT_VARIABLE)])
        self.assertEqual(self.ari.calls[-1], reads[0], 'read after the bridge and both channels exist')
        call = app.calls['c1']
        self.assertEqual((call.call_codec, call.codec), ('alaw', 'ulaw'), "the tap's own format is unchanged")
        self.assertEqual(app.stats['codec_unknown'], 0)
        app.handle(answered('c1'))  # later events: no second read
        self.assertEqual(len([c for c in self.ari.calls if c[0] == 'get_variable']), 1)
        # Review L1: the advisory read is bounded below the client's default 5 s.
        self.assertEqual(self.ari.codec_timeouts, [tap_app.CODEC_READ_TIMEOUT_S])
        self.assertLessEqual(tap_app.CODEC_READ_TIMEOUT_S, 1.0)

    def test_the_client_passes_the_read_timeout_to_its_request(self):
        from unittest import mock
        from services.tap.ari import AriClient
        client = AriClient('http://127.0.0.1:1', 'ava', 'pw', 'ava-tap')
        with mock.patch.object(client, '_request', return_value={'value': '(gsm)'}) as request:
            self.assertEqual(client.get_variable('c1', NATIVE_FORMAT_VARIABLE, timeout=1.0), '(gsm)')
            client.get_variable('c1', 'AVA_TAP')
        self.assertEqual([c.kwargs['timeout'] for c in request.call_args_list], [1.0, None])

    def test_a_failed_or_unusable_read_never_fails_the_tap(self):
        cases = (({'get_variable'}, {}, 'AriError from ARI'),
                 (None, {}, 'variable not set (None)'),
                 (None, {'c1': '(<script>)'}, 'unexpected characters'),
                 (None, {'c1': ''}, 'empty'))
        for fail, formats, label in cases:
            with self.subTest(label):
                app = self.make(fail=fail, formats=formats)
                app.handle(answered('c1'))
                self.assertIn('c1', app.calls, label)
                self.assertIsNone(app.calls['c1'].call_codec)
                self.assertEqual(self.sink.events, [('started', 'c1', '1001', '700')])
                self.assertEqual((app.stats['codec_unknown'], app.stats['tapped'], app.stats['tap_failures']),
                                 (1, 1, 0))

    def test_client_errors_of_other_kinds_are_contained(self):
        for error in (OSError('reset'), ValueError('bad id'), TypeError('x'), AttributeError('no such call'),
                      RuntimeError('unexpected'), KeyError('value')):
            with self.subTest(type(error).__name__):
                ari = FakeAri()

                def broken(channel_id, name, timeout=None, error=error):
                    if name == NATIVE_FORMAT_VARIABLE:
                        raise error
                    return None
                ari.get_variable = broken
                app = self.make(ari=ari)
                app.handle(answered('c1'))
                self.assertIsNone(app.calls['c1'].call_codec)
                self.assertEqual((app.stats['codec_unknown'], app.stats['tapped']), (1, 1))
                self.assertEqual(app._ours, {app.calls['c1'].snoop_id, app.calls['c1'].media_id},
                                 "the tap's channels stay tracked (review L2)")

    def test_resume_reads_the_codec_of_an_already_answered_call(self):
        app = self.make(formats={'c7': '(gsm)'})
        channel = answered('c7')['channel']
        self.ari.channels, self.ari.variables = [channel], {'c7': 'caller'}
        self.assertEqual(app.resume(), 1)
        self.assertEqual(app.calls['c7'].call_codec, 'gsm', 'the AVA_TAP mark is not mistaken for the codec')


class PublishedCodecTests(unittest.TestCase):
    """The real sink, LiveCalls and hub: the codec reaches `call.started`."""

    def started_data(self, **tapped):
        feed, pipeline, live, sub, _ = make()
        sink = TapScoringSink(live, feed, pipeline, Clock())
        sink.started(type('Call', (), {'call_id': 'p1', 'caller': 'ava-1', 'dialled': '700', 'codec': 'ulaw',
                                       **tapped})())
        events = scores(sub)
        self.assertEqual(events[0].type, 'call.started')
        return events[0].data

    def test_detected_codec_is_published(self):
        data = self.started_data(call_codec='alaw')
        self.assertEqual((data['codec'], data['tap_format']), ('alaw', 'ulaw'))

    def test_not_detected_is_published_as_null(self):
        self.assertIsNone(self.started_data(call_codec=None)['codec'])
        self.assertIsNone(self.started_data()['codec'], 'a call object without the field (older taps)')


class StoredCodecTests(unittest.TestCase):
    """`_session` maps the stored `start_data->>'codec'` text (any JSON could be stored)."""

    def row(self, codec):
        return (1, 'c1', None, False, 'monitoring', None, 'ok', False, 2.0, None, None, None, None, False, None, None,
                codec)

    def test_only_format_names_are_served(self):
        for stored, served in (('alaw', 'alaw'), ('ulaw|alaw', 'ulaw|alaw'), (None, None), ('', None),
                               ('5', None), ('<img src=x>', None), ('a' * 25, None),
                               ('ALAW', None), ('alaw ', None), ('+923001234567', None)):
            self.assertEqual(history_module._session(self.row(stored))['codec'], served, stored)

    def test_the_row_width_matches_the_selected_columns(self):
        self.assertEqual(len(history_module._SESSION_NAMES.split(',')) + 1, history_module.SESSION_WIDTH)
        self.assertEqual(len(self.row(None)), history_module.SESSION_WIDTH)
        self.assertNotIn(',', history_module._CODEC_COLUMN, 'one column, so the c. prefixing stays correct')


class RuleParityTests(unittest.TestCase):
    def test_tap_reader_and_dashboard_use_the_same_name_rule(self):
        self.assertEqual(tap_app.CODEC_PATTERN.pattern, history_module._CODEC.pattern)
        source = (ROOT / 'dashboard' / 'src' / 'history.ts').read_text(encoding='utf-8')
        match = re.search(r'const CODEC = /\^(.*)\$/;', source)
        self.assertIsNotNone(match, 'dashboard CODEC rule not found')
        self.assertEqual(match.group(1), tap_app.CODEC_PATTERN.pattern)


class MetricsTests(unittest.TestCase):
    def test_codec_unknown_is_exported_as_a_tap_counter(self):
        from services.live import health
        self.assertIn('codec_unknown', health.TAP_COUNTERS)
        app = TapApp(FakeAri(), Sink(), receiver_host='127.0.0.1', bind_host='127.0.0.1')
        self.addCleanup(app.close)
        self.assertTrue(set(health.TAP_COUNTERS) <= set(app.stats), 'every exported kind exists in TapApp.stats')


if __name__ == '__main__':
    unittest.main()
