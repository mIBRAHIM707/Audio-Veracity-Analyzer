"""Claude-authored tests (17 November 2026) for bridged calls between two SIP endpoints
with the codec forced by the PBX per endpoint (backlog 6.1; PRD FR-T1: "Asterisk PBX,
two or more SIP softphone endpoints, and a call generator ... with codec forcing for
G.711 and GSM"). Both endpoints are SIPp processes, not softphones.

Authored by Claude (Claude Code), not by GPT-6 Astra. A SIPp caller (endpoint
`anonymous`, mu-law offer) calls extension 810 or 811; the PBX dials a second SIPp
endpoint (`agent_alaw` / `agent_gsm`, one allowed codec each) and bridges the two, so
Asterisk transcodes between them. The agent answers with the forced codec and echoes
its RTP back. Evidence per call: the SDP offer in the INVITE the agent received, the
agent leg's native format, the audio the PBX sent to the agent and what came back
(MixMonitor on the agent leg), and what the PBX sent back towards the caller
(MixMonitor on the caller leg). Runs only with AVA_TEST_DOCKER_LAB=1 and the locally
built image; never in CI. Synthetic signals only: lab plumbing evidence, not detector
evidence.
"""
from pathlib import Path
import re
import time
import unittest
import uuid
import wave

import numpy as np

from data.prepare import codecs as g711
from lab.asterisk import rtp_pcap
from lab.asterisk.harness import LabContainer, LabError
from tests.lab.test_asterisk_lab import IMAGE, LAB, overlap, samples
from tests.unit.test_lab_rtp_pcap import speechlike

AGENTS = {'810': ('alaw', 5071), '811': ('gsm', 5072)}
BLOCK = 160  # samples per 20 ms packet at 8 kHz


def mismatch(a, b):
    """Diagnostics for two aligned int16 arrays: (exact fraction, max abs difference,
    first and last mismatching indices, n, differing 20 ms blocks)."""
    diff = np.abs(a.astype(np.int64) - b.astype(np.int64))
    bad = np.flatnonzero(diff)
    blocks = sorted({int(i) // BLOCK for i in bad})
    return (round(float(np.mean(diff == 0)), 5), int(diff.max(initial=0)),
            (int(bad[0]), int(bad[-1])) if len(bad) else None, len(a), f'{len(blocks)} 20 ms blocks', blocks[:40])


def first_invite(messages):
    """The first INVITE in a SIPp -trace_msg log, cut at its own Content-Length (review
    L2: never read SDP from a later message such as the agent's own 200 OK)."""
    start = messages.index('INVITE sip:')
    text = messages[start:]
    for separator in ('\r\n\r\n', '\n\n'):
        end = text.find(separator)
        if end != -1:
            break
    else:
        raise AssertionError('INVITE without a header/body separator')
    length = re.search(r'^Content-Length:\s*(\d+)', text[:end], re.MULTILINE | re.IGNORECASE)
    if length is None:
        raise AssertionError('INVITE without Content-Length')
    body_start = end + len(separator)
    return text[:end], text[body_start:body_start + int(length.group(1))]


def offered_audio_codecs(body):
    """Audio codecs (payload type, rtpmap name) of an SDP offer. Every payload type on
    the m=audio line must have an rtpmap; RFC 4733 telephone-event (DTMF) is returned
    separately, as it is not an audio codec."""
    match = re.search(r'^m=audio \d+ RTP/AVP ([0-9 ]+?)\s*$', body, re.MULTILINE)
    if match is None:
        raise AssertionError(f'no m=audio line in the offer: {body[:800]}')
    names = {int(pt): name for pt, name in re.findall(r'^a=rtpmap:(\d+) ([^/\s]+)/', body, re.MULTILINE)}
    offered = [int(pt) for pt in match.group(1).split()]
    if set(offered) - set(names):
        raise AssertionError(f'payload types without rtpmap: {offered} {names}')
    return ([(pt, names[pt]) for pt in offered if names[pt] != 'telephone-event'],
            [pt for pt in offered if names[pt] == 'telephone-event'])


@LAB
class BridgedCallLabTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.lab = LabContainer(IMAGE, network='none')
        except LabError as error:
            raise unittest.SkipTest(str(error))
        cls.addClassCleanup(cls.lab.stop)
        cls.lab.start()

    # --- helpers --------------------------------------------------------------------------
    def listening(self, port):
        """True if a UDP socket in the container is bound to `port` (from /proc/net/udp)."""
        table = self.lab.command(['exec', self.lab.name, 'cat', '/proc/net/udp']).stdout
        return any(line.split()[1].endswith(f':{port:04X}') for line in table.splitlines()[1:] if line.split())

    def wait_until(self, condition, timeout, message):
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() > deadline:
                self.fail(message)
            time.sleep(0.2)

    def start_agent(self, port, payload_type, name, log):
        # Review M4: no agent may be left from an earlier case, and this one is killed
        # at cleanup whatever happens.
        self.assertFalse(self.listening(port), f'an agent is already listening on {port}')
        self.addCleanup(self.lab.command, ['exec', self.lab.name, 'pkill', '-f', f'uas_agent.xml -p {port}'],
                        check=False)
        started = self.lab.command(['exec', '-d', self.lab.name, 'sipp', '-sf', '/lab/sipp/uas_agent.xml', '-p',
                                    str(port), '-i', '127.0.0.1', '-mi', '127.0.0.1', '-min_rtp_port', '17200',
                                    '-max_rtp_port', '17300', '-rtp_echo', '-key', 'pt', str(payload_type),
                                    '-key', 'codec', name, '-m', '1', '-timeout', '60s', '-nostdin', '-trace_msg',
                                    '-message_file', log], check=False)
        self.assertEqual(started.returncode, 0, started.stderr)
        self.wait_until(lambda: self.listening(port), 10, f'the agent did not bind {port}')  # review L1

    def call(self, extension, token, duration_ms):
        return self.lab.command(['exec', self.lab.name, 'sipp', '127.0.0.1:5060', '-sf', '/lab/sipp/uac_inject.xml',
                                 '-s', extension, '-key', 'caller', token, '-key', 'pt', '0', '-key', 'codec', 'PCMU',
                                 '-m', '1', '-d', str(duration_ms), '-i', '127.0.0.1', '-mi', '127.0.0.1',
                                 '-min_rtp_port', '16000', '-max_rtp_port', '16100', '-timeout', '60s',
                                 '-timeout_error', '-nostdin'], check=False, timeout=120)

    def wait_for_file(self, path, timeout=30):
        """A recording is complete once it exists and its size stops changing."""
        deadline, size = time.monotonic() + timeout, -1
        while time.monotonic() < deadline:
            if path.exists() and path.stat().st_size == size and size > 44:
                return
            size = path.stat().st_size if path.exists() else -1
            time.sleep(1.0)
        self.fail(f'{path.name} not written')

    def read_wav(self, path):
        self.wait_for_file(path)
        with wave.open(str(path), 'rb') as handle:
            self.assertEqual((handle.getnchannels(), handle.getsampwidth(), handle.getframerate()), (1, 2, 8000))
            return handle.readframes(handle.getnframes())

    def bridged_call(self, extension, seconds, seed):
        codec, port = AGENTS[extension]
        token = f'br{uuid.uuid4().hex[:8]}'
        log = f'/lab/io/agent-{token}-messages.log'
        self.start_agent(port, *rtp_pcap.PAYLOAD_TYPES[codec], log)
        payloads = rtp_pcap.rtp_payloads(speechlike(seconds, seed), 'ulaw')
        rtp_pcap.write_pcap(payloads, 0, Path(self.lab.io, 'inject.pcap'))
        done = self.call(extension, token, len(payloads) * 20 + 1500)
        self.assertEqual(done.returncode, 0, f'caller sipp: {done.stdout[-1500:]} {done.stderr[-1500:]}')
        # The agent left its port (it exits after the PBX's BYE, or on its own timeout;
        # the caller's success and the recordings show the call itself completed).
        self.wait_until(lambda: not self.listening(port), 15, 'the agent did not exit')
        base = f'bridge-{token}'
        format_file = Path(self.lab.io, f'{base}-format.txt')
        if not format_file.exists():
            logs = self.lab.command(['logs', '--tail', '60', self.lab.name], check=False)
            self.fail(f'no {format_file.name}; Asterisk said: {logs.stdout[-3000:]} {logs.stderr[-3000:]}')
        with open(Path(self.lab.io, Path(log).name), encoding='latin-1', newline='') as handle:
            _, offer = first_invite(handle.read())
        audio, events = offered_audio_codecs(offer)
        self.assertLessEqual(len(events), 1, 'at most one telephone-event (DTMF) payload')
        self.assertFalse(Path(self.lab.io, f'{base}-dialstatus.txt').exists(), 'Dial ended with the call')
        return {'sent': rtp_pcap.reference_decode(payloads, 'ulaw'), 'offer': audio,
                'format': format_file.read_text().strip(),  # CHANNEL(audionativeformat), e.g. "(alaw)"
                **{key: self.read_wav(Path(self.lab.io, f'{base}-{key.replace("_", "-")}.wav'))
                   for key in ('to_agent', 'from_agent', 'to_caller')}}

    def assert_exact(self, name, reference, recording, *, share=0.8, minimum=0.99):
        """Align `recording` on `reference`; the whole aligned region must be equal.
        Returns (reference offset, recording offset)."""
        correlation, ref_start, rec_start, n = overlap(reference, recording)
        a, b = samples(reference, ref_start, n), samples(recording, rec_start, n)
        self.assertGreater(correlation, minimum, (name, correlation, ref_start, rec_start, n, mismatch(a, b)))
        self.assertGreater(len(a), share * len(reference) // 2, f'most of the call is in the {name} recording')
        self.assertTrue(np.array_equal(a, b), (name, mismatch(a, b)))
        return ref_start, rec_start

    def assert_first_block_silent(self, name, recording):
        """Review L3/N2: the 20 ms that precede the aligned echo are silence."""
        first = np.frombuffer(recording, dtype='<i2')[:BLOCK]
        self.assertEqual(int(np.abs(first.astype(np.int64)).max()), 0, f'{name}: first 20 ms is not silence')

    # --- cases ----------------------------------------------------------------------------
    def test_alaw_agent_is_forced_and_transcoded_from_a_ulaw_caller(self):
        call = self.bridged_call('810', 4.0, 21)
        self.assertEqual(call['offer'], [(8, 'PCMA')], 'the PBX offered the agent A-law as its only audio codec')
        self.assertEqual(call['format'], '(alaw)', 'the agent leg negotiated A-law only')
        sent = call['sent']  # what the caller's mu-law packets carry
        # Asterisk 20.6 translates mu-law to A-law directly (codec_a_mu), so the agent
        # leg's recording is already A-law quantised: sample-identical to the project
        # encoder's A-law round trip of the mu-law decode, and not equal to the mu-law
        # decode itself. (Which format the recording shows depends on Asterisk's
        # translation path; a version change may need this updated.)
        alaw_trip = g711.decode(g711.encode(sent, 'alaw'), 'alaw')
        ref_start, rec_start = self.assert_exact('to agent', alaw_trip, call['to_agent'], share=0.9)
        n = min(len(sent) // 2 - ref_start, len(call['to_agent']) // 2 - rec_start)
        self.assertLess(mismatch(samples(sent, ref_start, n), samples(call['to_agent'], rec_start, n))[0], 0.5,
                        'the A-law leg is not the mu-law audio')
        # The echo (agent -> PBX) is exactly what the agent leg sent, one silent 20 ms
        # block later (review N2: nothing is skipped inside the aligned region).
        starts = self.assert_exact('echo from agent', call['to_agent'], call['from_agent'])
        self.assertEqual(starts, (0, BLOCK), 'one silent block, then every echoed frame')
        self.assert_first_block_silent('echo from agent', call['from_agent'])
        # Review M2: the PBX sends the echo on to the mu-law caller (A-law -> mu-law).
        echo_to_ulaw = g711.decode(g711.encode(call['from_agent'], 'ulaw'), 'ulaw')
        self.assert_exact('to caller', echo_to_ulaw, call['to_caller'])

    def test_gsm_agent_is_forced_and_transcoded_from_a_ulaw_caller(self):
        from data.prepare.ffmpeg_codecs import Ffmpeg
        from tests.unit.test_ffmpeg_codecs import real_ffmpeg_path
        path = real_ffmpeg_path()
        if path is None:
            self.skipTest('the GSM reference needs the pinned ffmpeg')
        call = self.bridged_call('811', 4.0, 22)
        self.assertEqual(call['offer'], [(3, 'GSM')], 'the PBX offered the agent GSM as its only audio codec')
        self.assertEqual(call['format'], '(gsm)', 'the agent leg negotiated GSM only')
        sent, to_agent, from_agent = call['sent'], call['to_agent'], call['from_agent']
        # No direct mu-law -> GSM translator: the agent leg sees the mu-law decode exactly
        # (signed linear) before the GSM encoder.
        self.assert_exact('to agent', sent, to_agent, share=0.9, minimum=0.999)
        # Review M3: the reference is a libgsm encode (pinned ffmpeg) of exactly what the
        # GSM encoder received, frame for frame from the start of the agent leg.
        ffmpeg = Ffmpeg(path)
        frames = to_agent[:len(to_agent) // (2 * BLOCK) * 2 * BLOCK]
        gsm_trip = rtp_pcap.reference_decode(rtp_pcap.rtp_payloads(frames, 'gsm', ffmpeg), 'gsm', ffmpeg)
        through_gsm, a_start, b_start, m = overlap(gsm_trip, from_agent)
        # Measured: the echo recording starts with one silent 20 ms block, then carries
        # every echoed frame from the encoder's first one (review N2: nothing is skipped
        # inside the aligned region).
        self.assertEqual((a_start, b_start), (0, BLOCK), 'one silent block, then the first frame the encoder got')
        self.assert_first_block_silent('echo from agent', from_agent)
        a, b = samples(gsm_trip, a_start, m), samples(from_agent, b_start, m)
        self.assertGreater(len(a), 0.8 * len(sent) // 2, 'most of the echo came back')
        self.assertTrue(np.array_equal(a, b), ('echo from agent', through_gsm, mismatch(a, b)))
        to_original, *_ = overlap(sent, from_agent)
        self.assertGreater(through_gsm, to_original + 0.2, 'GSM-coded, not the unencoded audio')
        # Review M2: the PBX sends the decoded echo on to the mu-law caller. There is no
        # direct GSM -> mu-law translator, so the caller leg records the GSM decode itself
        # (signed linear, before the mu-law encoder), unlike the A-law case above.
        self.assert_exact('to caller', from_agent, call['to_caller'])

    def test_unreachable_agent_fails_the_call_without_an_answer(self):
        # Review M1: no agent listens on 5071, so the PBX cannot reach the second
        # endpoint. Over UDP nothing reports the absence (measured: no fast failure), so
        # Dial rings it out for its 30 s timeout. The outcome must come from the agent leg
        # (DIALSTATUS NOANSWER), the caller must never be answered (SIPp expects a 200)
        # and the answer hook must never run.
        self.assertFalse(self.listening(5071), 'no agent may be listening for this case')
        token = f'br{uuid.uuid4().hex[:8]}'
        rtp_pcap.write_pcap([b'\xff' * BLOCK], 0, Path(self.lab.io, 'inject.pcap'))
        started = time.monotonic()
        done = self.call('810', token, 1000)
        elapsed = time.monotonic() - started
        self.assertNotEqual(done.returncode, 0, 'an unanswered bridged call must not succeed')
        status = Path(self.lab.io, f'bridge-{token}-dialstatus.txt')
        self.wait_until(status.exists, 10, 'Dial did not report an outcome')
        self.assertEqual(status.read_text().strip(), 'NOANSWER')
        self.assertGreater(elapsed, 25, 'the call failed for a reason other than the unanswered agent leg')
        self.assertLess(elapsed, 45, 'the caller learns of the failure soon after the 30 s dial timeout')
        self.assertFalse(Path(self.lab.io, f'bridge-{token}-format.txt').exists())
