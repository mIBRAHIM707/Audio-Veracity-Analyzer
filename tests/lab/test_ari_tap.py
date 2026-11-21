"""Claude-authored tests (15 November 2026): the passive ARI tap against a real
Asterisk 20 in the lab container (backlog 6.2; PRD FR-T2, FR-T3, FR-T4, FR-T5, FR-I1;
architecture §2.1, the Phase 0 tap spike 0.9).

Authored by Claude (Claude Code), not by GPT-6 Astra. Opt-in (AVA_TEST_DOCKER_LAB=1,
needs the lab image and `requirements-tap.txt`); never in CI. The container runs on
Docker's default bridge with ARI published on 127.0.0.1 only and a random per-run ARI
password (passed through the environment, not the command line); ExternalMedia sends
RTP to this host (host.docker.internal). SIPp calls extension 700, which answers and
records what the caller sends without ever entering Stasis; the tap spies on it from
outside, so each case can compare the tapped audio with the call's own recording.
Synthetic signals only: tap plumbing evidence, not detector evidence.
19 Nov 2026 (Claude-authored, 6.QA): callers offering only A-law or only GSM are tapped
through Asterisk's transcoding to the tap's mu-law ExternalMedia.
"""
import os
from pathlib import Path
import secrets
import threading
import time
import unittest
import uuid
import wave

import numpy as np

from lab.asterisk import rtp_pcap
from lab.asterisk.harness import LabContainer, LabError
from services.tap.ari import AriClient
from services.tap.tap_app import TapApp
from tests.lab.test_asterisk_lab import IMAGE, overlap, samples
from tests.unit.test_lab_rtp_pcap import speechlike

LAB = unittest.skipIf(os.environ.get('AVA_TEST_DOCKER_LAB') != '1', 'set AVA_TEST_DOCKER_LAB=1 (needs Docker and the lab image)')


class Sink:
    def __init__(self):
        self.lock = threading.Lock()
        self.events, self.audio_by_call, self.receiver_stats, self.times = [], {}, {}, {}
        self.concealed, self.codecs = {}, {}

    def started(self, call):
        with self.lock:
            self.events.append(('started', call.call_id, call.caller, call.dialled))
            self.codecs[call.call_id] = call.call_codec  # FR-U5 detected codec (20 Nov)

    def audio(self, call_id, pcm, concealment):
        with self.lock:
            self.audio_by_call[call_id] = self.audio_by_call.get(call_id, b'') + pcm
            self.concealed[call_id] = self.concealed.get(call_id, 0) + sum(c for _, c in concealment)

    def quality(self, call_id, change):
        with self.lock:
            self.events.append(('quality', call_id, change))

    def ended(self, call_id, reason, receiver_stats):
        with self.lock:
            self.events.append(('ended', call_id, reason))
            self.receiver_stats[call_id] = receiver_stats
            self.times[call_id] = time.monotonic()

    def failed(self, call_id, error):
        with self.lock:
            self.events.append(('failed', call_id, error))


class TapRunner:
    """Runs a TapApp on its own thread (the single owner), as the service would.
    `stop()` shuts it down cleanly (taps released); `kill()` drops the WebSocket and
    abandons the app without any cleanup, like a crashed process."""

    def __init__(self, ari, sink, on_tick=None):
        self.ari, self.on_tick = ari, on_tick  # on_tick: run on this thread after each loop turn
        self.app = TapApp(ari, sink, receiver_host='host.docker.internal', bind_host='0.0.0.0',
                          tap_extensions={'700', '701'})
        self.connection = None
        self.freeze_s = 0.0  # set > 0 to stop reading events once for that long (a hung tap)
        self.ready, self.stop_event = threading.Event(), threading.Event()
        self.killed = False
        self.error = None
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        if not self.ready.wait(10) or self.connection is None:
            raise AssertionError(f'tap did not connect: {self.error!r}')

    def _loop(self):
        try:
            with self.ari.events() as connection:
                self.connection = connection
                self.app.resume()  # sweep what a crashed tap left; tap calls already answered
                self.ready.set()
                while not self.stop_event.is_set():
                    if self.freeze_s:
                        frozen, self.freeze_s = self.freeze_s, 0.0
                        time.sleep(frozen)  # connected but not reading: events and RTP pile up
                    self.app.run_once(connection, timeout=0.05)
                    if self.on_tick is not None:
                        self.on_tick()
        except Exception as error:  # connection closed by the test, or a real failure
            if not self.stop_event.is_set():
                self.error = error
        finally:
            if not self.killed:
                self.app.close()
            self.ready.set()

    def stop(self):
        self.stop_event.set()
        try:
            if self.connection is not None:
                self.connection.close()
        finally:
            self.thread.join(timeout=15)

    def kill(self):
        self.killed = True
        self.stop()
        for call in list(self.app.calls.values()):  # a dead process frees its sockets, nothing else
            call.session.close()


@LAB
class AriTapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        password = secrets.token_hex(16)
        try:
            cls.lab = LabContainer(IMAGE, network='bridge', publish=['127.0.0.1::8088'],
                                   env={'AVA_ARI_PASSWORD': password})
        except LabError as error:
            raise unittest.SkipTest(str(error))
        cls.addClassCleanup(cls.lab.stop)
        cls.lab.start()
        cls.ari = AriClient(f'http://{cls.lab.port("8088/tcp")}', 'ava', password, 'ava-tap')

    def call(self, pcm, extra_ms=1500, during=None, service='700', record=True, codec='ulaw', ffmpeg=None,
             scenario='/lab/sipp/uac_inject.xml'):
        """Place one call to `service` offering only `codec` (default mu-law; `ffmpeg`
        for GSM) and return (token, injected reference = the decode of what SIPp sent,
        recording or None, the time SIPp finished). `during(token)` runs on this thread
        while SIPp runs. `scenario` may name another SIPp scenario in the container."""
        payloads = rtp_pcap.rtp_payloads(pcm, codec, ffmpeg)
        payload_type, codec_name = rtp_pcap.PAYLOAD_TYPES[codec]
        rtp_pcap.write_pcap(payloads, payload_type, self.lab.path('inject.pcap'))
        token = f'tap{uuid.uuid4().hex[:8]}'
        result = {}

        def run():
            result['done'] = self.lab.command(
                ['exec', self.lab.name, 'sipp', '127.0.0.1:5060', '-sf', scenario, '-s', service,
                 '-key', 'caller', token, '-key', 'pt', str(payload_type), '-key', 'codec', codec_name, '-m', '1',
                 '-d', str(len(payloads) * 20 + extra_ms), '-i', '127.0.0.1', '-mi', '127.0.0.1',
                 '-min_rtp_port', '16000', '-max_rtp_port', '16100', '-timeout', '60s', '-timeout_error', '-nostdin'],
                check=False, timeout=120)
            result['finished'] = time.monotonic()
        caller = threading.Thread(target=run)
        caller.start()
        if during is not None:
            during(token)
        caller.join(timeout=150)
        done = result['done']
        self.assertEqual(done.returncode, 0, f'sipp: {done.stdout[-1200:]} {done.stderr[-1200:]}')
        reference = rtp_pcap.reference_decode(payloads, codec, ffmpeg)
        if not record:
            return token, reference, None, result['finished']
        recording = self.lab.path(f'rec-{token}.wav')
        deadline, size = time.monotonic() + 30, -1
        while time.monotonic() < deadline:
            if recording.exists() and recording.stat().st_size == size and size > 44:
                break
            size = recording.stat().st_size if recording.exists() else -1
            time.sleep(0.5)
        self.assertTrue(recording.exists(), 'the call itself must complete and record')
        with wave.open(str(recording), 'rb') as handle:
            recorded = handle.readframes(handle.getnframes())
        return token, reference, recorded, result['finished']

    def wait_for(self, sink, predicate, seconds=15):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            with sink.lock:
                found = [e for e in sink.events if predicate(e)]
            if found:
                return found
            time.sleep(0.05)
        return []

    def asterisk_tap_objects(self):
        """Bridges and tap channels (snoop, ExternalMedia) still alive in Asterisk."""
        channels = [c['name'] for c in self.ari.list_channels() if c['name'].startswith(('Snoop/', 'UnicastRTP/'))]
        return self.ari.list_bridges(), channels

    def assert_call_unharmed(self, reference, recorded, share=0.9, min_correlation=0.999):
        correlation, _, _, n = overlap(reference, recorded)
        self.assertGreater(correlation, min_correlation)
        self.assertGreater(n, share * len(reference) // 2, 'the whole call was recorded')

    def test_tap_receives_the_caller_audio_and_the_call_lifecycle(self):
        sink = Sink()
        runner = TapRunner(self.ari, sink)
        self.addCleanup(runner.stop)
        token, reference, recorded, finished = self.call(speechlike(4.0, 11))
        started = self.wait_for(sink, lambda e: e[0] == 'started' and e[2] == token)
        self.assertEqual(len(started), 1, sink.events)
        call_id = started[0][1]
        self.assertEqual(started[0][3], '700')  # FR-T3: from and to come from ARI fields
        self.assertEqual(sink.codecs[call_id], 'ulaw')  # FR-U5 detected codec (Claude-authored, 20 Nov)
        ended = self.wait_for(sink, lambda e: e[0] == 'ended' and e[1] == call_id)
        self.assertEqual(ended, [('ended', call_id, 'bye')])
        # FR-T5: torn down within 5 s of the call's end, in Asterisk as well as here.
        self.assertLess(sink.times[call_id] - finished, 5.0)
        self.assertEqual(runner.app.calls, {})
        self.assertEqual(self.asterisk_tap_objects(), ([], []))
        tapped = sink.audio_by_call.get(call_id, b'')
        self.assertGreater(len(tapped), 0, 'no tapped audio reached the receiver')
        correlation, ref_start, tap_start, n = overlap(reference, tapped)
        receiver = sink.receiver_stats[call_id]
        self.assertGreater(correlation, 0.95, (correlation, ref_start, tap_start, n, receiver))
        # The tap runs through the 60 ms jitter buffer (the caller's call holds 1.5 s of
        # silence after the audio ends before the BYE).
        self.assertGreater(n, 0.85 * len(reference) // 2, (ref_start, tap_start, n))
        self.assertEqual((receiver['jitter_concealed_frames'], receiver['jitter_late'], receiver['late_or_duplicate'],
                          receiver['malformed'], receiver['other_source']), (0, 0, 0, 0, 0), receiver)
        self.assertEqual(sink.concealed.get(call_id), 0, 'no concealment, even after the stream stopped')
        self.assertFalse([e for e in sink.events if e[0] == 'quality'], 'no false media-quality report')
        difference = samples(reference, ref_start, n).astype(int) != samples(tapped, tap_start, n).astype(int)
        frames = difference[:n - n % 160].reshape(-1, 160).any(axis=1)
        # Measured 15 Nov: the first few 20 ms frames after the answer (snoop/bridge
        # start-up) are missing or silent; allow up to 200 ms. After that the tap path
        # (snoop -> bridge -> ExternalMedia mu-law) is lossless for mu-law input.
        startup = int(np.argmin(frames)) if frames.any() else 0
        self.assertLessEqual(startup, 10, f'start-up loss {startup} frames')
        self.assertFalse(frames[startup:].any(), f'differing frames after start-up: {np.flatnonzero(frames)[:20]}')
        self.assert_call_unharmed(reference, recorded)
        self.assertIsNone(runner.error)

    # -- callers on other PBX codecs (Claude-authored, 19 Nov 2026; backlog 6.QA "codecs
    #    other than mu-law end to end"; PRD FR-T2 G.711 A-law/mu-law and GSM) ------------

    def check_transcoded_tap(self, codec, ffmpeg, seed, min_correlation):
        """A caller offering only `codec`: the call runs on it (the PBX records the
        decode of what was sent), and Asterisk transcodes it for the tap's mu-law
        ExternalMedia, so the tap hears the caller's decoded audio through one more
        mu-law step. Returns (correlation with the sent audio's decode, with the
        original signal)."""
        pcm = speechlike(4.0, seed)
        sink = Sink()
        runner = TapRunner(self.ari, sink)
        self.addCleanup(runner.stop)
        negotiated = {}

        def read_format(token):
            # Review M1: read the caller leg's negotiated native format while it runs.
            found = self.wait_for(sink, lambda e: e[0] == 'started' and e[2] == token)
            if found:
                negotiated['format'] = self.ari.get_variable(found[0][1], 'CHANNEL(audionativeformat)')
        token, reference, recorded, finished = self.call(pcm, codec=codec, ffmpeg=ffmpeg, during=read_format)
        # Asterisk prints the native format capabilities in parentheses, e.g. "(alaw)".
        self.assertEqual(negotiated.get('format'), f'({codec})', 'the call ran on the offered codec only')
        started = self.wait_for(sink, lambda e: e[0] == 'started' and e[2] == token)
        self.assertEqual(len(started), 1, sink.events)
        call_id = started[0][1]
        # Claude-authored, 20 Nov (FR-U5): the tap's own read gives the same codec.
        self.assertEqual(sink.codecs[call_id], codec, 'the tap detected the call codec')
        self.assertEqual(self.wait_for(sink, lambda e: e[0] == 'ended' and e[1] == call_id), [('ended', call_id, 'bye')])
        self.assertLess(sink.times[call_id] - finished, 5.0)
        self.assertEqual(runner.app.calls, {})
        self.assertEqual(self.asterisk_tap_objects(), ([], []))
        tapped = sink.audio_by_call.get(call_id, b'')
        self.assertGreater(len(tapped), 0, 'no tapped audio reached the receiver')
        correlation, ref_start, tap_start, n = overlap(reference, tapped)
        receiver = sink.receiver_stats[call_id]
        self.assertGreater(correlation, min_correlation, (correlation, ref_start, tap_start, n, receiver))
        self.assertGreater(n, 0.85 * len(reference) // 2, (ref_start, tap_start, n))
        self.assertEqual((receiver['jitter_concealed_frames'], receiver['jitter_late'], receiver['late_or_duplicate'],
                          receiver['malformed'], receiver['other_source']), (0, 0, 0, 0, 0), receiver)
        self.assertEqual(sink.concealed.get(call_id), 0)
        self.assertFalse([e for e in sink.events if e[0] == 'quality'], 'no false media-quality report')
        # The call itself ran on the offered codec and was not disturbed by the tap
        # (Asterisk's own GSM decoder vs ffmpeg's: > 0.99 as in test_asterisk_lab).
        self.assert_call_unharmed(reference, recorded, min_correlation=min(0.999, min_correlation))
        self.assertIsNone(runner.error)
        to_original, *_ = overlap(pcm, tapped)
        return correlation, to_original

    def test_an_alaw_caller_is_tapped_through_transcoding(self):
        correlation, _ = self.check_transcoded_tap('alaw', None, 12, 0.999)
        print(f'\nobserved: A-law caller, tap correlation with the A-law decode {correlation:.5f}')

    def test_a_gsm_caller_is_tapped_through_transcoding(self):
        from data.prepare.ffmpeg_codecs import Ffmpeg
        from tests.unit.test_ffmpeg_codecs import real_ffmpeg_path
        path = real_ffmpeg_path()
        if path is None:
            self.skipTest('GSM payloads need the pinned ffmpeg')
        correlation, to_original = self.check_transcoded_tap('gsm', Ffmpeg(path), 13, 0.99)
        # The tap hears the GSM-coded call, not the clean signal (lab review L5 rule).
        self.assertGreater(correlation, to_original)
        print(f'\nobserved: GSM caller, tap correlation with the GSM decode {correlation:.5f}, '
              f'with the original {to_original:.5f}')

    def test_a_caller_offering_two_codecs_is_reported_with_the_one_in_use(self):
        """Claude-authored, 20 Nov (FR-U5, review M1): an ordinary phone offers several
        codecs. The caller offers mu-law then A-law (static payload types 0 and 8) and
        sends mu-law; the detected codec must be the one the call runs on, not a list."""
        source = Path(__file__).resolve().parents[2] / 'lab' / 'asterisk' / 'sipp' / 'uac_inject.xml'
        text = source.read_text(encoding='iso-8859-1')
        single = 'm=audio [media_port] RTP/AVP [pt]\n      a=rtpmap:[pt] [codec]/8000'
        self.assertIn(single, text)
        both = 'm=audio [media_port] RTP/AVP 0 8\n      a=rtpmap:0 PCMU/8000\n      a=rtpmap:8 PCMA/8000'
        self.lab.path('uac_two_codecs.xml').write_text(text.replace(single, both), encoding='iso-8859-1')
        sink = Sink()
        runner = TapRunner(self.ari, sink)
        self.addCleanup(runner.stop)
        negotiated = {}

        def read_format(token):
            found = self.wait_for(sink, lambda e: e[0] == 'started' and e[2] == token)
            if found:
                negotiated['format'] = self.ari.get_variable(found[0][1], 'CHANNEL(audionativeformat)')
        token, reference, recorded, _ = self.call(speechlike(3.0, 14), during=read_format,
                                                  scenario='/lab/io/uac_two_codecs.xml')
        started = self.wait_for(sink, lambda e: e[0] == 'started' and e[2] == token)
        self.assertEqual(len(started), 1, sink.events)
        print(f"\nobserved: two-codec offer, CHANNEL(audionativeformat) = {negotiated.get('format')!r}, "
              f'detected {sink.codecs[started[0][1]]!r}')
        self.assertEqual(sink.codecs[started[0][1]], 'ulaw')
        self.assert_call_unharmed(reference, recorded)
        self.assertIsNone(runner.error)

    def test_call_goes_on_when_the_tap_is_not_running(self):
        # FR-T4: no tap connected; the call never depends on it.
        _, reference, recorded, _ = self.call(speechlike(3.0, 12))
        self.assert_call_unharmed(reference, recorded)

    def test_call_goes_on_when_the_tap_stops_mid_call(self):
        # FR-T4: the tap shuts down cleanly once it has started on the call.
        sink = Sink()
        runner = TapRunner(self.ari, sink)
        self.addCleanup(runner.stop)

        def stop_once_tapped(token):
            self.assertTrue(self.wait_for(sink, lambda e: e[0] == 'started' and e[2] == token))
            time.sleep(0.5)
            runner.stop()
        _, reference, recorded, _ = self.call(speechlike(5.0, 13), during=stop_once_tapped)
        self.assertTrue(self.wait_for(sink, lambda e: e[0] == 'ended' and e[2] == 'monitor_lost'), sink.events)
        self.assert_call_unharmed(reference, recorded)
        self.assertEqual(self.asterisk_tap_objects(), ([], []), 'a clean stop releases its objects')

    def test_only_the_caller_leg_is_tapped_when_an_agent_answers(self):
        # Review H-A1: extension 701 dials an agent (a SIPp UAS on 5070); the agent leg
        # must not be tapped as the caller.
        sink = Sink()
        runner = TapRunner(self.ari, sink)
        self.addCleanup(runner.stop)
        self.lab.command(['exec', '-d', self.lab.name, 'sipp', '-sn', 'uas', '-p', '5070', '-i', '127.0.0.1',
                          '-mi', '127.0.0.1', '-min_rtp_port', '17000', '-max_rtp_port', '17100', '-m', '1',
                          '-timeout', '60s', '-nostdin'])
        time.sleep(1.0)
        seen = []

        def watch_agent(token):
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline and not seen:
                seen.extend(c for c in self.ari.list_channels()
                            if c['name'].startswith('PJSIP/agent') and c['state'] == 'Up')
                time.sleep(0.2)
        token, reference, _, _ = self.call(speechlike(4.0, 15), service='701', record=False, during=watch_agent)
        self.assertTrue(seen, 'the agent leg must really be answered for this test to mean anything')
        time.sleep(1.0)
        with sink.lock:
            starts = [e for e in sink.events if e[0] == 'started']
        self.assertEqual(len(starts), 1, sink.events)
        self.assertEqual((starts[0][2], starts[0][3]), (token, '701'))
        self.assertTrue(self.wait_for(sink, lambda e: e[0] == 'ended' and e[1] == starts[0][1]))
        correlation, *_ = overlap(reference, sink.audio_by_call.get(starts[0][1], b'\x00\x00' * 8000))
        self.assertGreater(correlation, 0.95, 'the tapped audio is the caller, not the agent')
        self.assertEqual(self.asterisk_tap_objects(), ([], []))

    def test_call_goes_on_when_the_tap_hangs_mid_call(self):
        # Review M3: the tap stays connected but stops reading for 8 s mid-call.
        sink = Sink()
        runner = TapRunner(self.ari, sink)
        self.addCleanup(runner.stop)

        def freeze_once_tapped(token):
            self.assertTrue(self.wait_for(sink, lambda e: e[0] == 'started' and e[2] == token))
            runner.freeze_s = 8.0
        _, reference, recorded, _ = self.call(speechlike(5.0, 16), during=freeze_once_tapped)
        self.assert_call_unharmed(reference, recorded)
        # The tap catches up afterwards and ends the call it was tapping.
        self.assertTrue(self.wait_for(sink, lambda e: e[0] == 'ended', seconds=30), sink.events)

    def test_call_goes_on_when_the_tap_crashes_mid_call(self):
        # FR-T4 / PRD 4.7: the tap disappears without any cleanup (like a killed
        # process) after it has started on the call; the call completes unharmed.
        sink = Sink()
        runner = TapRunner(self.ari, sink)
        self.addCleanup(runner.stop)

        def kill_once_tapped(token):
            self.assertTrue(self.wait_for(sink, lambda e: e[0] == 'started' and e[2] == token))
            time.sleep(0.5)
            runner.kill()
        _, reference, recorded, _ = self.call(speechlike(5.0, 14), during=kill_once_tapped)
        self.assert_call_unharmed(reference, recorded)
        # Measured 15 Nov: a crash leaves the tap bridge and its ExternalMedia channel in
        # Asterisk (the snoop ends with the call). A restarted tap sweeps them.
        bridges, channels = self.asterisk_tap_objects()
        self.assertEqual(len(bridges), 1, (bridges, channels))
        restarted = TapRunner(self.ari, Sink())
        self.addCleanup(restarted.stop)
        self.assertEqual(restarted.app.stats['swept'], 1)
        self.assertEqual(self.asterisk_tap_objects(), ([], []))


if __name__ == '__main__':
    unittest.main()
