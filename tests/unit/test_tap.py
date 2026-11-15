"""Claude-authored tests (15 November 2026) for services/tap (backlog 6.2; PRD FR-T2,
FR-T3, FR-T4, FR-T5, FR-I1). Authored by Claude (Claude Code), not by GPT-6 Astra.

Pure tests: RTP parsing, a real UDP RtpSession on 127.0.0.1, the ARI client's URL,
ID and error rules (a local stub HTTP server), and TapApp dispatch with a fake ARI
client. The Asterisk end-to-end cases are in tests/lab/test_ari_tap.py (Docker, opt-in).
"""
import http.server
import json
import socket
import struct
import threading
import time
import unittest

from data.prepare import codecs as g711
from services.tap.ari import AriClient, AriError, _id
from services.tap.rtp_receiver import RtpError, RtpSession, parse_rtp
from services.tap.tap_app import TAP_TAG, TapApp


def rtp(seq, payload=b'\xff' * 160, pt=0, ssrc=7, ts=None, csrc=0, extension=None, padding=0):
    first = 0x80 | csrc | (0x10 if extension is not None else 0) | (0x20 if padding else 0)
    packet = struct.pack('!BBHII', first, pt, seq & 0xFFFF, (ts if ts is not None else seq * 160) & 0xFFFFFFFF, ssrc)
    packet += b'\x00\x00\x00\x01' * csrc
    if extension is not None:
        packet += struct.pack('!HH', 0xBEDE, len(extension) // 4) + extension
    packet += payload
    if padding:
        packet += bytes(padding - 1) + bytes([padding])
    return packet


class ParseRtpTests(unittest.TestCase):
    def test_fields_csrc_extension_and_padding(self):
        packet = parse_rtp(rtp(5, b'abc', pt=8, csrc=2, extension=b'\x01\x02\x03\x04', padding=3))
        self.assertEqual((packet['payload_type'], packet['sequence'], packet['ssrc'], packet['payload']),
                         (8, 5, 7, b'abc'))
        marked = bytearray(rtp(1))
        marked[1] |= 0x80
        self.assertTrue(parse_rtp(bytes(marked))['marker'])

    def test_malformed_refused(self):
        cases = {'short': b'\x80' * 11, 'version 1': b'\x40' + rtp(1)[1:], 'too long': rtp(1, b'x' * 1500),
                 'csrc beyond packet': bytes([0x8F]) + rtp(1, b'')[1:],
                 'truncated extension': bytes([0x90]) + rtp(1, b'')[1:],
                 'zero padding count': bytes([0xA0]) + rtp(1, b'ab\x00')[1:],
                 'padding beyond packet': bytes([0xA0]) + rtp(1, b'\xff')[1:], 'not bytes': 'rtp'}
        for label, value in cases.items():
            with self.assertRaises(RtpError, msg=label):
                parse_rtp(value)


class RtpSessionTests(unittest.TestCase):
    def setUp(self):
        self.now = [100.0]
        self.session = RtpSession(payload_type=0, host='127.0.0.1', clock=lambda: self.now[0], max_seconds=1)
        self.addCleanup(self.session.close)
        self.sender = self.socket()

    def socket(self):
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sender.bind(('127.0.0.1', 0))
        self.addCleanup(sender.close)
        return sender

    def send(self, *packets, sender=None):
        for packet in packets:
            (sender or self.sender).sendto(packet, ('127.0.0.1', self.session.port))

    def poll_all(self, session, expected):
        deadline, got = time.monotonic() + 5, 0
        while got < expected and time.monotonic() < deadline:
            got += session.poll(0.2)
        return got

    def test_decodes_in_order_and_counts_gaps_late_and_strangers(self):
        tone = bytes(range(0, 160)) * 2
        payloads = [tone[i:i + 160] for i in (0, 160)]
        self.send(rtp(10, payloads[0]), rtp(12, payloads[1]), rtp(11), rtp(12), rtp(13, pt=8),
                  b'garbage', rtp(14, b''))
        self.send(rtp(13), sender=self.socket())  # same SSRC, another address: refused (review M2)
        self.assertEqual(self.poll_all(self.session, 2), 2)
        self.session.poll(0.3)  # the rest are refused
        self.assertEqual(self.session.take(), g711.decode(b''.join(payloads), 'ulaw'))
        self.assertEqual(self.session.take(), b'', 'audio is handed over once and not kept')
        self.assertEqual(self.session.stats, {'packets': 2, 'malformed': 2, 'other_payload': 1, 'other_source': 1,
                                              'gaps': 1, 'missing': 1, 'late_or_duplicate': 2, 'over_limit': 0,
                                              'ssrc_changes': 0})
        self.assertEqual((self.session.ssrc, self.session.source), (7, self.sender.getsockname()))

    def test_allowed_source_refuses_every_other_host(self):
        session = RtpSession(host='127.0.0.1', allowed_source='192.0.2.1')
        self.addCleanup(session.close)
        self.sender.sendto(rtp(1), ('127.0.0.1', session.port))
        time.sleep(0.2)
        session.poll(0.3)
        self.assertEqual((session.stats['packets'], session.stats['other_source'], session.ssrc), (0, 1, None))

    def test_sequence_wrap_silence_clock_and_pending_bound(self):
        self.assertEqual(self.session.seconds_since_packet(), 0.0)
        self.now[0] = 131.0
        self.assertEqual(self.session.seconds_since_packet(), 31.0)
        self.send(*[rtp(65530 + i) for i in range(60)])  # wraps; 60 x 20 ms > 1 s pending, nobody takes
        self.assertEqual(self.poll_all(self.session, 60), 60)
        self.assertEqual(self.session.stats['gaps'], 0)
        self.assertEqual(len(self.session.take()), 50 * 320)
        self.assertEqual(self.session.stats['over_limit'], 10)
        self.assertEqual(self.session.seconds_since_packet(), 0.0)

    def test_refusals_and_close(self):
        for bad in ({'payload_type': 3}, {'max_seconds': 0}, {'max_seconds': 1.5}):
            with self.assertRaises(ValueError):
                RtpSession(host='127.0.0.1', **bad)
        self.session.close()
        self.assertTrue(self.session.closed)


class StubAri(http.server.BaseHTTPRequestHandler):
    reply = (200, b'{}')

    def do_GET(self):
        status, body = self.server.reply
        self.send_response(status)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_POST = do_DELETE = do_GET

    def log_message(self, *args):
        pass


class AriClientTests(unittest.TestCase):
    def test_urls_ids_and_unreachable_server(self):
        client = AriClient('http://127.0.0.1:1', 'ava', 's3cret', 'ava-tap', timeout=1)
        self.assertEqual(client.events_url(), 'ws://127.0.0.1:1/ari/events?app=ava-tap&subscribeAll=true')
        for bad in ('', 'a/b', 'a?b', 'a b', '%2e', '.', '..', 'x' * 129, None):
            with self.assertRaises(ValueError):
                _id(bad)
        with self.assertRaises(AriError) as caught:
            client.snoop('1234.5')
        self.assertNotIn('s3cret', str(caught.exception))
        for bad in (('ftp://x', 'a'), ('http://x:1', 'a/b')):
            with self.assertRaises(ValueError):
                AriClient(bad[0], 'u', 'p', bad[1])

    def test_every_bad_reply_is_an_ari_error(self):
        # Review M1: non-JSON bodies and HTTP errors must not escape as other types.
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), StubAri)
        self.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        client = AriClient(f'http://127.0.0.1:{server.server_address[1]}', 'ava', 'pw', 'ava-tap', timeout=2)
        server.reply = (200, b'{"id": "x"}')
        self.assertEqual(client.create_bridge(), {'id': 'x'})
        server.reply = (204, b'')
        self.assertIsNone(client.hangup('x'))
        for reply in ((200, b'<html>not json'), (200, b'[' * 100000), (500, b'{}'), (404, b'')):
            server.reply = reply
            with self.assertRaises(AriError, msg=reply[0]):
                client.create_bridge()


class FakeAri:
    def __init__(self, fail=None):
        self.calls, self.fail, self.counter, self.created, self.bridges = [], fail or {}, 0, [], []
        self.channels, self.variables = [], {}

    def _do(self, name, *args):
        self.calls.append((name, *args))
        if name in self.fail:
            raise AriError(f'{name} failed', 500)
        self.counter += 1
        object_id = f'{name}-{self.counter}'
        if name in ('snoop', 'external_media', 'bridge'):
            self.created.append(object_id)
        return {'id': object_id}

    def released(self):
        return {c[1] for c in self.calls if c[0] in ('hangup', 'destroy_bridge')}

    def snoop(self, channel_id, spy='in', app_args=''):
        return self._do('snoop', channel_id, spy, app_args)

    def external_media(self, host_port, fmt='ulaw'):
        return self._do('external_media', host_port, fmt)

    def create_bridge(self, name=''):
        return self._do('bridge', name)

    def list_channels(self):
        return self.channels

    def get_variable(self, channel_id, name):
        self.calls.append(('get_variable', channel_id, name))
        return self.variables.get(channel_id)

    def list_bridges(self):
        self.calls.append(('list_bridges',))
        if 'list_bridges' in self.fail:
            raise AriError('list failed', 500)
        return self.bridges

    def add_to_bridge(self, bridge_id, channels):
        return self._do('add', bridge_id, tuple(channels))

    def hangup(self, channel_id):
        return self._do('hangup', channel_id)

    def destroy_bridge(self, bridge_id):
        return self._do('destroy_bridge', bridge_id)


class Sink:
    def __init__(self):
        self.events, self.receiver_stats = [], {}

    def started(self, call):
        self.events.append(('started', call.call_id, call.caller, call.dialled))

    def audio(self, call_id, pcm):
        self.events.append(('audio', call_id, len(pcm)))

    def ended(self, call_id, reason, receiver_stats):
        assert isinstance(receiver_stats, dict) and 'packets' in receiver_stats
        self.events.append(('ended', call_id, reason))
        self.receiver_stats[call_id] = receiver_stats

    def failed(self, call_id, error):
        self.events.append(('failed', call_id, error))


def answered(channel_id, number='1001', exten='700', name=None, state='Up', marked=True):
    """The dialplan's Set(AVA_TAP=caller) after Answer() (ChannelVarset), or for
    `marked=False` a plain state change of an unmarked channel."""
    channel = {'id': channel_id, 'name': name or f'PJSIP/anonymous-{channel_id}', 'state': state,
               'caller': {'number': number}, 'dialplan': {'context': 'from-sipp', 'exten': exten},
               'creationtime': '2026-11-15T12:00:00.000+0000'}
    if marked:
        return {'type': 'ChannelVarset', 'variable': 'AVA_TAP', 'value': 'caller', 'channel': channel}
    return {'type': 'ChannelStateChange', 'channel': channel}


class TapAppTests(unittest.TestCase):
    def make(self, fail=None, **options):
        self.ari, self.sink, self.now = FakeAri(fail), Sink(), [0.0]
        options.setdefault('clock', lambda: self.now[0])
        app = TapApp(self.ari, self.sink, receiver_host='host.docker.internal', bind_host='127.0.0.1', **options)
        self.addCleanup(app.close)
        return app

    def test_answered_call_is_tapped_without_touching_it_and_ends_on_hangup(self):
        app = self.make(tap_extensions={'700'})
        app.handle(answered('c1', state='Ring'))  # marked but not answered yet: nothing
        self.assertEqual(self.ari.calls, [])
        app.handle(answered('c1', number='ava-1'))
        # Bridge first, each channel into it at once (review M4: crash leftovers sweepable).
        self.assertEqual([c[0] for c in self.ari.calls], ['bridge', 'snoop', 'add', 'external_media', 'add'])
        call = app.calls['c1']
        self.assertEqual(self.ari.calls[1][1:], ('c1', 'in', TAP_TAG))
        self.assertEqual(self.ari.calls[3][1], f'host.docker.internal:{call.session.port}')
        self.assertEqual((self.ari.calls[2][1:], self.ari.calls[4][1:]),
                         ((call.bridge_id, (call.snoop_id,)), (call.bridge_id, (call.media_id,))))
        self.assertEqual(self.sink.events, [('started', 'c1', 'ava-1', '700')])
        app.handle(answered('c1', number='ava-1'))  # later events for the same call: no second tap
        for name, channel_id, marked in (('Snoop/c1-0001', call.snoop_id, True),
                                         ('UnicastRTP/127.0.0.1:5000-1', call.media_id, True),
                                         ('PJSIP/other', 'c9', True),  # another extension
                                         ('PJSIP/agent-0001', 'agent-leg', False),  # Dial()ed leg, unmarked
                                         ('Local/x@ctx-0001;1', 'local', True), ('SIP/old-0001', 'chan-sip', True)):
            app.handle(answered(channel_id, name=name, exten='600' if channel_id == 'c9' else '700', marked=marked))
        self.assertEqual(list(app.calls), ['c1'])  # only the marked inbound PJSIP caller leg
        session = call.session
        app.handle({'type': 'ChannelHangupRequest', 'soft': True, 'channel': {'id': 'c1'}})
        self.assertIn('c1', app.calls, 'a soft hangup request (transfer) does not end the tap')
        app.handle({'type': 'ChannelHangupRequest', 'channel': {'id': 'c1'}})
        self.assertEqual(self.sink.events[-1], ('ended', 'c1', 'bye'))
        self.assertEqual(self.ari.released(), {call.bridge_id, call.snoop_id, call.media_id})
        self.assertFalse(any(c[1] == 'c1' for c in self.ari.calls if c[0] == 'hangup'), 'the call is never touched')
        self.assertTrue(session.closed)
        app.handle({'type': 'ChannelDestroyed', 'channel': {'id': call.snoop_id}})  # after the end: ignored
        app.handle(answered('c1', number='ava-1'))  # a late Up event for an ended call: not tapped again
        self.assertEqual((app.stats['ended'], app.stats['tapped'], app.calls), (1, 1, {}))

    def test_tap_failure_is_contained_and_releases_everything(self):
        for step in ('snoop', 'external_media', 'bridge', 'add'):
            app = self.make(fail={step})
            app.handle(answered('c2'))
            self.assertEqual(app.calls, {}, step)
            self.assertEqual(self.sink.events, [('failed', 'c2', 'AriError')], step)
            self.assertEqual(set(self.ari.created), self.ari.released(), step)
            self.assertFalse(any(c[1] == 'c2' for c in self.ari.calls if c[0] == 'hangup'))
            self.assertEqual(app._ours, set(), step)
            app.handle(answered('c2'))  # not retried on every later event
            self.assertEqual(app.stats['tap_failures'], 1, step)

    def test_capacity(self):
        app = self.make(max_calls=1)
        app.handle(answered('a'))
        app.handle(answered('b'))
        self.assertEqual(list(app.calls), ['a'])
        self.assertEqual(self.sink.events[-1], ('failed', 'b', 'capacity'))
        self.assertFalse(any(len(c) > 1 and c[1] == 'b' for c in self.ari.calls))

    def test_audio_delivery_silence_timeout_media_loss_and_close(self):
        app = self.make(silence_timeout_s=30.0)
        for call_id in ('c4', 'c5', 'c6'):
            app.handle(answered(call_id))
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(sender.close)
        for seq in range(3):
            sender.sendto(rtp(seq), ('127.0.0.1', app.calls['c4'].session.port))
        deadline = time.monotonic() + 5
        while app.calls['c4'].session.stats['packets'] < 3 and time.monotonic() < deadline:
            app.poll(0.1)
        audio = [e for e in self.sink.events if e[0] == 'audio']
        self.assertEqual({e[1] for e in audio}, {'c4'})
        self.assertEqual(sum(e[2] for e in audio), 960)  # 3 packets x 160 samples x 2 bytes, each once
        app.poll(0.0)
        self.assertEqual(sum(e[2] for e in self.sink.events if e[0] == 'audio'), 960, 'no re-delivery')
        app.handle({'type': 'ChannelDestroyed', 'channel': {'id': app.calls['c6'].media_id}})
        self.assertIn(('ended', 'c6', 'monitor_lost'), self.sink.events)
        self.now[0] = 31.0  # c5 never received a packet; c4's last packet was at 0.0
        app.poll(0.0)
        self.assertIn(('ended', 'c5', 'timeout'), self.sink.events)
        self.assertIn(('ended', 'c4', 'timeout'), self.sink.events)
        self.assertEqual((self.sink.receiver_stats['c4']['packets'], self.sink.receiver_stats['c5']['packets']), (3, 0))
        app.handle(answered('c7'))
        app.close()
        self.assertEqual(self.sink.events[-1], ('ended', 'c7', 'monitor_lost'))
        self.assertEqual((app.calls, app.stats['sink_errors']), ({}, 0))

    def test_sweep_removes_only_unowned_tap_bridges_and_their_channels(self):
        app = self.make()
        app.handle(answered('c11'))
        owned = app.calls['c11'].bridge_id
        self.assertEqual(self.ari.calls[0], ('bridge', TAP_TAG))  # tap bridges carry the tag
        self.ari.bridges = [{'id': owned, 'name': TAP_TAG, 'channels': ['s1', 'm1']},
                            {'id': 'left', 'name': TAP_TAG, 'channels': ['s2', 'm2', 'call-7']},
                            {'id': 'other-app', 'name': 'conference', 'channels': ['call-9']}, 'junk']
        self.ari.channels = [{'id': 's2', 'name': 'Snoop/call-7-01'}, {'id': 'm2', 'name': 'UnicastRTP/x-01'},
                             {'id': 'call-7', 'name': 'PJSIP/anonymous-07'}]
        self.assertEqual(app.sweep(), 1)
        removed = [c for c in self.ari.calls if c[0] in ('hangup', 'destroy_bridge')]
        # Review M2: a call channel placed in a tagged bridge is never hung up.
        self.assertEqual(removed, [('hangup', 's2'), ('hangup', 'm2'), ('destroy_bridge', 'left')])
        self.assertIn('c11', app.calls)
        app = self.make(fail={'list_bridges'})
        self.assertEqual((app.sweep(), app.stats['cleanup_errors']), (0, 1))

    def test_resume_retaps_after_reconnect_and_finds_calls_answered_earlier(self):
        # Review M1: close() (connection lost) must not bar a call from being tapped again,
        # and a call answered while the tap was away is found by resume().
        app = self.make()
        app.handle(answered('c12'))
        app.close()
        self.assertEqual(self.sink.events[-1], ('ended', 'c12', 'monitor_lost'))
        up = answered('c12')['channel']
        self.ari.channels = [up, {**answered('c13')['channel']}, {**answered('c14')['channel']},
                             {**answered('c15', state='Ring')['channel']}, {'id': 's', 'name': 'Snoop/x', 'state': 'Up'}]
        self.ari.variables = {'c12': 'caller', 'c13': 'caller'}  # c14 unmarked (an agent leg)
        self.assertEqual(app.resume(), 2)
        self.assertEqual(sorted(app.calls), ['c12', 'c13'])
        app.handle({'type': 'ChannelDestroyed', 'channel': {'id': 'c13'}})
        self.ari.variables['c13'] = 'caller'
        self.assertEqual(app.resume(), 0, 'a call that really ended is not tapped again')

    def test_snoop_loss_socket_failure_and_ssrc_change(self):
        app = self.make()
        app.handle(answered('c16'))
        app.handle({'type': 'ChannelDestroyed', 'channel': {'id': app.calls['c16'].snoop_id}})
        self.assertEqual(self.sink.events[-1], ('ended', 'c16', 'bye'))
        app.handle(answered('c17'))
        app.calls['c17'].session._socket.close()  # the socket fails under the tap
        app.poll(0.0)
        self.assertEqual(self.sink.events[-1], ('ended', 'c17', 'monitor_lost'))
        session = RtpSession(host='127.0.0.1')
        self.addCleanup(session.close)
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(sender.close)
        for packet in (rtp(1, ssrc=7), rtp(2, ssrc=7), rtp(900, ssrc=8), rtp(901, ssrc=8)):
            sender.sendto(packet, ('127.0.0.1', session.port))
        deadline = time.monotonic() + 5
        while session.stats['packets'] < 4 and time.monotonic() < deadline:
            session.poll(0.1)
        self.assertEqual((session.stats['packets'], session.stats['ssrc_changes'], session.stats['gaps'],
                          session.ssrc), (4, 1, 0, 8))

    def test_packets_in_flight_at_hangup_are_delivered(self):
        # Review L4: the socket is read once more before the session closes.
        app = self.make()
        app.handle(answered('c10'))
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(sender.close)
        sender.sendto(rtp(1), ('127.0.0.1', app.calls['c10'].session.port))
        time.sleep(0.2)
        app.handle({'type': 'ChannelDestroyed', 'channel': {'id': 'c10'}})
        self.assertEqual([e for e in self.sink.events if e[0] == 'audio'], [('audio', 'c10', 320)])

    def test_malformed_events_and_sink_faults_never_raise(self):
        class Broken(Sink):
            def started(self, call):
                raise RuntimeError('sink down')
        app = self.make()
        app.sink = Broken()
        app.handle(answered('c8'))
        self.assertEqual(app.stats['sink_errors'], 1)
        self.assertIn('c8', app.calls)
        malformed = [{'type': 'ChannelStateChange'}, {'type': 'ChannelStateChange', 'channel': {'id': 5}},
                     {'channel': None}, {'type': 'ChannelDestroyed', 'channel': {'id': 'unknown'}},
                     {'type': 'ChannelVarset', 'variable': 'AVA_TAP', 'value': 'caller',
                      'channel': {'id': 'x', 'state': 'Up', 'name': 'PJSIP/x', 'caller': 'not a dict'}},
                     {'type': 'ChannelVarset', 'variable': 'AVA_TAP', 'value': 'caller',
                      'channel': {'id': 'y', 'state': 'Up', 'name': 'PJSIP/y', 'dialplan': ['list']}}]
        for event in malformed:
            app.handle(event)  # never raises

        class Connection:
            def __init__(self, items):
                self.items = list(items)

            def recv(self, timeout=None):
                item = self.items.pop(0)
                if isinstance(item, BaseException):
                    raise item
                return item
        connection = Connection(['not json', json.dumps([1]), TimeoutError(), json.dumps(
            {'type': 'ChannelHangupRequest', 'channel': {'id': 'c8'}})])
        for _ in range(4):
            app.run_once(connection)
        self.assertGreaterEqual(app.stats['bad_events'], 4)
        self.assertNotIn('c8', app.calls)


if __name__ == '__main__':
    unittest.main()
