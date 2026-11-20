"""Claude-authored tests (20 November 2026) for the lab's authenticated softphone
endpoint (backlog 6.1 "a softphone endpoint"; PRD FR-T1 "two or more SIP softphone
endpoints").

Authored by Claude (Claude Code), not by GPT-6 Astra. The `softphone` endpoint exists
only when the container is started with AVA_SOFTPHONE_PASSWORD (a random one per test
class here, passed through the harness's environment). It must register with digest
authentication, call the lab extensions with credentials, be reachable on extension 702
at its registered address, and refuse a request that names it without credentials.
SIPp plays the softphone (as it plays every lab endpoint): a real softphone application
was not used, so this shows the PBX side of a softphone, not a given client's
behaviour. Runs only with AVA_TEST_DOCKER_LAB=1 and the locally built image; never in
CI. Synthetic signals only: lab plumbing evidence, not detector evidence.
"""
from pathlib import Path
import re
import secrets
import time
import unittest
import uuid

from lab.asterisk import rtp_pcap
from lab.asterisk.harness import LabContainer, LabError
from tests.lab import test_bridged_call_lab as bridged  # module, so its cases are not collected here
from tests.lab.test_asterisk_lab import IMAGE, LAB
from tests.lab.test_bridged_call_lab import BLOCK, first_invite, offered_audio_codecs
from tests.unit.test_lab_rtp_pcap import speechlike

PHONE_PORT = 5073  # the softphone registers from, and listens on, this port


class _LabCalls:
    """Helpers shared with the bridged-call tests (same container conventions)."""
    listening = bridged.BridgedCallLabTests.listening
    wait_until = bridged.BridgedCallLabTests.wait_until
    wait_for_file = bridged.BridgedCallLabTests.wait_for_file
    read_wav = bridged.BridgedCallLabTests.read_wav
    assert_exact = bridged.BridgedCallLabTests.assert_exact

    def asterisk(self, command):
        return self.lab.command(['exec', self.lab.name, 'asterisk', '-rx', command], check=False).stdout

    def sipp(self, args, *, credentials=False, password=None, timeout=120):
        """Run SIPp in the container. With `credentials`, SIPp authenticates as
        `softphone` with the password taken from an environment variable inside the
        container (review M3: never on the host's command line): the container's own
        AVA_SOFTPHONE_PASSWORD, or `password` passed through docker's environment."""
        if not credentials:
            return self.lab.command(['exec', self.lab.name, 'sipp', *args], check=False, timeout=timeout)
        script = 'exec sipp "$@" -au softphone -ap "$AVA_SOFTPHONE_PASSWORD"'
        if password is None:
            return self.lab.command(['exec', self.lab.name, 'sh', '-c', script, 'sipp', *args], check=False,
                                    timeout=timeout)
        return self.lab.command(['exec', '-e', 'AVA_SOFTPHONE_PASSWORD', self.lab.name, 'sh', '-c', script, 'sipp',
                                 *args], check=False, timeout=timeout, env={'AVA_SOFTPHONE_PASSWORD': password})

    def caller(self, extension, token, duration_ms, *, scenario='uac_inject.xml', credentials=False, log=None):
        """A SIPp caller playing /lab/io/inject.pcap (mu-law). With `credentials`, the
        authenticated scenario as user `softphone`."""
        args = ['127.0.0.1:5060', '-sf', f'/lab/sipp/{scenario}', '-s', extension,
                '-key', 'caller', token, '-key', 'pt', '0', '-key', 'codec', 'PCMU', '-m', '1',
                '-d', str(duration_ms), '-i', '127.0.0.1', '-mi', '127.0.0.1', '-min_rtp_port', '16000',
                '-max_rtp_port', '16100', '-timeout', '60s', '-timeout_error', '-nostdin']
        if log is not None:
            args += ['-trace_msg', '-message_file', log]
        return self.sipp(args, credentials=credentials)

    def messages(self, log):
        return Path(self.lab.io, Path(log).name).read_text(encoding='latin-1')

    def contacts(self):
        """Registered softphone contacts, from `pjsip show contacts`."""
        return [line.split()[1] for line in self.asterisk('pjsip show contacts').splitlines()
                if line.strip().startswith('Contact:') and line.split()[1].startswith('softphone/')]


@LAB
class SoftphoneLabTests(_LabCalls, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password = secrets.token_hex(12)  # 24 letters/digits, never written to Git or logs
        try:
            cls.lab = LabContainer(IMAGE, network='none', env={'AVA_SOFTPHONE_PASSWORD': cls.password})
        except LabError as error:
            raise unittest.SkipTest(str(error))
        cls.addClassCleanup(cls.lab.stop)
        cls.lab.start()

    def setUp(self):
        # Each case starts with no registration, whatever an earlier case left.
        self.assertFalse(self.listening(PHONE_PORT), f'something is already bound to {PHONE_PORT}')
        self.register(expires=0)
        self.assertEqual(self.contacts(), [])

    def register(self, *, expires, password=None):
        return self.sipp(['127.0.0.1:5060', '-sf', '/lab/sipp/uac_register.xml', '-p', str(PHONE_PORT),
                          '-i', '127.0.0.1', '-key', 'expires', str(expires), '-m', '1', '-timeout', '20s',
                          '-timeout_error', '-nostdin'], credentials=True, password=password, timeout=60)

    def test_the_softphone_cannot_be_registered_under_another_from_user(self):
        # Review L5: the address-of-record `softphone` in To, another user in From and no
        # credentials. The request is not identified as `softphone`; whatever endpoint it
        # falls to, it must not create a contact.
        log = f'/lab/io/regas-{uuid.uuid4().hex[:8]}-messages.log'
        done = self.sipp(['127.0.0.1:5060', '-sf', '/lab/sipp/uac_register_as.xml', '-p', str(PHONE_PORT),
                          '-i', '127.0.0.1', '-key', 'from_user', 'intruder', '-m', '1', '-timeout', '20s',
                          '-timeout_error', '-nostdin', '-trace_msg', '-message_file', log], timeout=60)
        self.assertNotEqual(done.returncode, 0, 'the registration must be refused')
        messages = self.messages(log)
        self.assertRegex(messages, r'SIP/2\.0 [4-6]\d\d ', 'the PBX sent a final refusal')
        self.assertNotIn('SIP/2.0 200', messages)
        self.assertEqual(self.contacts(), [])

    def test_registration_needs_the_password(self):
        wrong = self.register(expires=120, password=secrets.token_hex(12))
        self.assertNotEqual(wrong.returncode, 0, 'a wrong password must not register')
        self.assertEqual(self.contacts(), [])
        done = self.register(expires=120)
        self.assertEqual(done.returncode, 0, f'register: {done.stdout[-1500:]} {done.stderr[-1500:]}')
        self.assertEqual(len(self.contacts()), 1, self.contacts())
        self.assertIn(f'@127.0.0.1:{PHONE_PORT}', self.contacts()[0])
        self.assertEqual(self.register(expires=0).returncode, 0)
        self.assertEqual(self.contacts(), [], 'Expires: 0 removes the registration')

    def test_a_call_naming_the_softphone_without_credentials_is_challenged(self):
        # Without credentials the request is not handed to `anonymous`: PJSIP identifies
        # the endpoint by the From user and challenges it; it is never answered.
        # Review L7: the probe expects exactly the challenge and acknowledges it, so it
        # exits 0 only if the PBX challenged (an answer would be an unexpected message).
        log = f'/lab/io/unauth-{uuid.uuid4().hex[:8]}-messages.log'
        done = self.sipp(['127.0.0.1:5060', '-sf', '/lab/sipp/uac_challenge_probe.xml', '-s', '600', '-m', '1',
                          '-i', '127.0.0.1', '-mi', '127.0.0.1', '-min_rtp_port', '16000', '-max_rtp_port', '16100',
                          '-timeout', '20s', '-timeout_error', '-nostdin', '-trace_msg', '-message_file', log],
                         timeout=60)
        self.assertEqual(done.returncode, 0, f'probe: {done.stdout[-1500:]} {done.stderr[-1500:]}')
        messages = self.messages(log)
        self.assertIn('SIP/2.0 401', messages, 'the PBX challenged the request')
        self.assertNotIn('SIP/2.0 200', messages, 'the PBX never answered it')
        branches = [re.search(rf'^{method} sip:.*?^Via: [^\r\n]*;branch=([^;\s]+)', messages,
                              re.MULTILINE | re.DOTALL).group(1) for method in ('INVITE', 'ACK')]
        self.assertEqual(branches[0], branches[1], 'the ACK closes the challenged transaction')

    def test_an_authenticated_softphone_call_carries_its_audio(self):
        payloads = rtp_pcap.rtp_payloads(speechlike(4.0, 31), 'ulaw')
        rtp_pcap.write_pcap(payloads, 0, Path(self.lab.io, 'inject.pcap'))
        log = f'/lab/io/auth-{uuid.uuid4().hex[:8]}-messages.log'
        done = self.caller('600', 'unused', len(payloads) * 20 + 1500, scenario='uac_auth_inject.xml',
                           credentials=True, log=log)
        self.assertEqual(done.returncode, 0, f'softphone caller: {done.stdout[-1500:]} {done.stderr[-1500:]}')
        # Review L6: the challenge was acknowledged on the first INVITE's transaction (same
        # Via branch), so the PBX sent its 401 once and did not retransmit it.
        messages = self.messages(log)
        branches = {method: re.search(rf'^{method} sip:.*?^Via: [^\r\n]*;branch=([^;\s]+)', messages,
                                      re.MULTILINE | re.DOTALL).group(1) for method in ('INVITE', 'ACK')}
        self.assertEqual(branches['ACK'], branches['INVITE'], 'the 401 ACK belongs to the first INVITE')
        self.assertEqual(messages.count('SIP/2.0 401'), 1, 'one challenge, not retransmitted')
        # The dialplan names the recording after the caller number, here the endpoint's user.
        recording = self.read_wav(Path(self.lab.io, 'rec-softphone.wav'))
        # Review M1: the credential umask is not inherited by Asterisk, so recordings stay
        # readable to the test user on a Linux host (Docker Desktop hides modes).
        mode = self.lab.command(['exec', self.lab.name, 'stat', '-c', '%a', '/lab/io/rec-softphone.wav']).stdout
        self.assertTrue(int(mode.strip(), 8) & 0o004, f'recording mode {mode.strip()} is not world-readable')
        self.assert_exact('softphone to PBX', rtp_pcap.reference_decode(payloads, 'ulaw'), recording, share=0.9)

    def test_a_registered_softphone_is_reached_on_extension_702(self):
        done = self.register(expires=120)
        self.assertEqual(done.returncode, 0, f'register: {done.stdout[-1500:]} {done.stderr[-1500:]}')
        log = f'/lab/io/phone-{uuid.uuid4().hex[:8]}-messages.log'
        # The softphone now listens where it registered (as a real client would).
        self.addCleanup(self.lab.command, ['exec', self.lab.name, 'pkill', '-f', f'uas_agent.xml -p {PHONE_PORT}'],
                        check=False)
        started = self.lab.command(['exec', '-d', self.lab.name, 'sipp', '-sf', '/lab/sipp/uas_agent.xml', '-p',
                                    str(PHONE_PORT), '-i', '127.0.0.1', '-mi', '127.0.0.1', '-min_rtp_port', '17200',
                                    '-max_rtp_port', '17300', '-rtp_echo', '-key', 'pt', '0', '-key', 'codec', 'PCMU',
                                    '-m', '1', '-timeout', '60s', '-nostdin', '-trace_msg', '-message_file', log],
                                   check=False)
        self.assertEqual(started.returncode, 0, started.stderr)
        self.wait_until(lambda: self.listening(PHONE_PORT), 10, 'the softphone did not bind its port')
        token = f'sp{uuid.uuid4().hex[:8]}'
        payloads = rtp_pcap.rtp_payloads(speechlike(4.0, 32), 'ulaw')
        rtp_pcap.write_pcap(payloads, 0, Path(self.lab.io, 'inject.pcap'))
        done = self.caller('702', token, len(payloads) * 20 + 1500)
        self.assertEqual(done.returncode, 0, f'caller: {done.stdout[-1500:]} {done.stderr[-1500:]}')
        self.wait_until(lambda: not self.listening(PHONE_PORT), 15, 'the softphone did not exit after the BYE')
        with open(Path(self.lab.io, Path(log).name), encoding='latin-1', newline='') as handle:
            _, offer = first_invite(handle.read())
        audio, _ = offered_audio_codecs(offer)
        self.assertEqual({name for _, name in audio}, {'PCMU', 'PCMA', 'GSM'}, 'the endpoint allows ulaw, alaw, gsm')
        base = f'bridge-{token}'
        self.assertEqual(Path(self.lab.io, f'{base}-format.txt').read_text().strip(), '(ulaw)')
        self.assertFalse(Path(self.lab.io, f'{base}-dialstatus.txt').exists(), 'Dial ended with the call')
        to_phone = self.read_wav(Path(self.lab.io, f'{base}-to-agent.wav'))
        self.assert_exact('PBX to softphone', rtp_pcap.reference_decode(payloads, 'ulaw'), to_phone, share=0.9)
        self.assert_exact('echo from softphone', to_phone, self.read_wav(Path(self.lab.io, f'{base}-from-agent.wav')))

    def test_extension_702_fails_at_once_when_the_softphone_is_not_registered(self):
        assert_unavailable(self)


def assert_unavailable(case):
    """Extension 702 with no softphone contact: Dial reports CHANUNAVAIL within seconds
    (no 30 s ring-out); the caller was answered first (the tap marks answered calls),
    so its SIPp fails on the PBX's BYE."""
    token = f'sp{uuid.uuid4().hex[:8]}'
    rtp_pcap.write_pcap([b'\xff' * BLOCK] * 50, 0, Path(case.lab.io, 'inject.pcap'))
    started = time.monotonic()
    done = case.caller('702', token, 20000)
    elapsed = time.monotonic() - started
    case.assertNotEqual(done.returncode, 0, 'the PBX hung up before the caller did')
    status = Path(case.lab.io, f'bridge-{token}-dialstatus.txt')
    case.wait_until(status.exists, 10, 'Dial did not report an outcome')
    case.assertEqual(status.read_text().strip(), 'CHANUNAVAIL')
    case.assertLess(elapsed, 15, 'no ring-out to a missing contact')
    case.assertFalse(Path(case.lab.io, f'bridge-{token}-format.txt').exists(), 'nothing answered on the phone side')


@LAB
class NoSoftphoneLabTests(_LabCalls, unittest.TestCase):
    """Without AVA_SOFTPHONE_PASSWORD the endpoint does not exist."""

    @classmethod
    def setUpClass(cls):
        try:
            cls.lab = LabContainer(IMAGE, network='none')
        except LabError as error:
            raise unittest.SkipTest(str(error))
        cls.addClassCleanup(cls.lab.stop)
        cls.lab.start()

    def test_no_softphone_endpoint_without_a_password(self):
        endpoints = self.asterisk('pjsip show endpoints')
        self.assertIn('anonymous', endpoints, 'the listing worked')
        self.assertNotIn('softphone', endpoints)
        listing = self.lab.command(['exec', self.lab.name, 'wc', '-c', '/etc/asterisk/pjsip_softphone.conf']).stdout
        self.assertEqual(listing.split()[0], '0', 'the included file is empty')

    def test_extension_702_fails_at_once_without_a_softphone(self):
        assert_unavailable(self)


@LAB
class SoftphonePasswordRuleLabTests(unittest.TestCase):
    """The entrypoint refuses a weak or unsafe softphone password and Asterisk never starts."""

    def start_with(self, password):
        try:
            lab = LabContainer(IMAGE, network='none', env={'AVA_SOFTPHONE_PASSWORD': password})
        except LabError as error:
            self.skipTest(str(error))
        self.addCleanup(lab.stop)
        with self.assertRaises(LabError) as raised:
            lab.start(boot_timeout=30)
        self.assertIn('exited during start-up', str(raised.exception))

    def test_short_password_is_refused(self):
        self.start_with('abc123')

    def test_password_with_other_characters_is_refused(self):
        self.start_with('abcdefgh12345678;x=1')


if __name__ == '__main__':
    unittest.main()
