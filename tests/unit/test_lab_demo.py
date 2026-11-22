"""Claude-authored tests (22 November 2026; not GPT-6 Astra) for the lab demo helper,
lab/demo.py (backlog 11.2; PRD roadmap 5.9, BO-4/BO-8; docs/operations/demo.md).

A fake `docker compose` runner records commands; WAV files are synthetic tones written
in a temporary folder (no real audio). Cases:
LD01 `call` writes a G.711 capture of the joined WAVs, copies it into the PBX and runs
     the same SIPp call the lab tests place, with the codec's payload type;
LD02 more than 55 s of audio, a missing or non-PCM WAV, a bad extension, a missing env
     file or no docker are refusals (exit 2) and run nothing;
LD03 a failed copy or call is exit 1 naming the step;
LD04 `egress`: both blocked is exit 0; a connection or a probe that did not run is exit 1
     and is never reported as blocked;
LD05 `status`: healthy services plus a ready dashboard is exit 0; a missing forwarder,
     an unhealthy service or an unreachable dashboard is exit 1.
"""
import io
import json
import struct
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace

from lab import demo


def tone_wav(path, seconds, rate=16000):
    import math
    samples = [int(8000 * math.sin(2 * math.pi * 440 * i / rate)) for i in range(int(seconds * rate))]
    with wave.open(str(path), 'wb') as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(struct.pack(f'<{len(samples)}h', *samples))
    return path


class FakeRun:
    def __init__(self, results=None):
        self.calls, self.results = [], results or {}
        self.captures = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        if 'cp' in cmd:
            source = Path(cmd[cmd.index('cp') + 1])
            self.captures.append(source.read_bytes())  # the temp capture exists during the copy
        for key, result in self.results.items():
            if key in cmd:
                return result
        return SimpleNamespace(returncode=0, stdout='', stderr='')


def records(out):
    return [json.loads(line) for line in out.getvalue().splitlines()]


class DemoTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.env = self.folder / 'demo.env'
        self.env.write_text('AVA_ARI_PASSWORD=not-read\n', encoding='utf-8')

    def main(self, argv, run=None, **kwargs):
        out = io.StringIO()
        run = run or FakeRun()
        code = demo.main(argv, run=run, stdout=out, which=lambda name: '/usr/bin/docker', **kwargs)
        return code, records(out), run

    def test_ld01_call_writes_a_capture_and_places_the_lab_call(self):
        a, b = tone_wav(self.folder / 'a.wav', 2.0), tone_wav(self.folder / 'b.wav', 1.0, rate=8000)
        for codec, payload_type, name in (('ulaw', '0', 'PCMU'), ('alaw', '8', 'PCMA')):
            with self.subTest(codec=codec):
                code, out, run = self.main(['call', '--env-file', str(self.env), '--wav', str(a), '--wav', str(b),
                                            '--codec', codec])
                self.assertEqual(code, 0, out)
                copy, call = run.calls
                base = ['/usr/bin/docker', 'compose', '--env-file', str(self.env.resolve()), '-f',
                        str(demo.COMPOSE_FILE), '-p', 'ava', '--profile', 'lab', '--profile', 'demo']
                self.assertEqual(copy[:len(base)], base)
                self.assertEqual(copy[len(base):][0], 'cp')
                self.assertEqual(copy[-1], 'asterisk:/lab/io/inject.pcap')
                capture = run.captures[0]
                self.assertEqual(capture[:4], struct.pack('<I', 0xA1B2C3D4), 'a libpcap file')
                packets = 3000 // 20  # 3 s of audio, one packet per 20 ms
                header, record_head = 24, 16
                frame = (len(capture) - header) // packets - record_head
                self.assertEqual(frame, 14 + 20 + 8 + 12 + 160, 'Ethernet+IPv4+UDP+RTP+160 G.711 bytes')
                rtp = capture[header + record_head + 42:header + record_head + 54]
                self.assertEqual(rtp[1] & 0x7F, int(payload_type))
                self.assertEqual(call[:len(base)], base)
                tail = call[len(base):]
                self.assertEqual(tail[:4], ['exec', '-T', 'asterisk', 'sipp'])
                self.assertEqual(tail[tail.index('-s') + 1], '700')
                self.assertEqual(tail[tail.index('-d') + 1], str(packets * 20 + 1500))
                keys = {tail[i + 1]: tail[i + 2] for i, v in enumerate(tail) if v == '-key'}
                self.assertEqual((keys['pt'], keys['codec']), (payload_type, name))
                self.assertTrue(keys['caller'].startswith('demo'))
                self.assertEqual([r['event'] for r in out], ['calling', 'call_completed'])
                self.assertEqual(out[0]['seconds'], 3.0)
                self.assertNotIn(str(self.folder), json.dumps(out), 'no local path in the output')

    def test_ld02_refusals_run_nothing(self):
        long_wav = tone_wav(self.folder / 'long.wav', 56.0, rate=8000)
        not_pcm = self.folder / 'float.wav'
        not_pcm.write_bytes(b'RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x03\x00\x01\x00'
                            b'\x80\x3e\x00\x00\x00\xfa\x00\x00\x04\x00\x20\x00data\x00\x00\x00\x00')
        ok = tone_wav(self.folder / 'ok.wav', 1.0)
        truncated = self.folder / 'truncated.wav'
        truncated.write_bytes(tone_wav(self.folder / 'whole.wav', 1.0).read_bytes()[:30])
        cases = (
            (['call', '--env-file', str(self.env), '--wav', str(ok), '--wav', str(truncated)], 'unusable WAV (file 2)'),
            (['call', '--env-file', str(self.env), '--wav', str(long_wav)], 'at most 55 s'),
            (['call', '--env-file', str(self.env), '--wav', str(self.folder / 'missing.wav')], 'not a file'),
            (['call', '--env-file', str(self.env), '--wav', str(not_pcm)], 'unusable WAV'),
            (['call', '--env-file', str(self.env), '--wav', str(ok), '--extension', '7;00'], 'digits'),
            (['call', '--env-file', str(self.folder / 'none.env'), '--wav', str(ok)], 'env file not found'),
            (['status', '--env-file', str(self.env), '--demo-port', '0'], 'demo port'),
        )
        for argv, reason in cases:
            with self.subTest(reason=reason):
                code, out, run = self.main(argv)
                self.assertEqual(code, 2, out)
                self.assertEqual(run.calls, [])
                [record] = out
                self.assertEqual(record['event'], 'refused')
                self.assertIn(reason, record['reason'])
        out = io.StringIO()
        self.assertEqual(demo.main(['egress', '--env-file', str(self.env)], run=FakeRun(), stdout=out,
                                   which=lambda name: None), 2)
        self.assertIn('docker not found', records(out)[0]['reason'])

    def test_ld03_a_failed_step_is_exit_1_naming_it(self):
        ok = tone_wav(self.folder / 'ok.wav', 1.0)
        failed = SimpleNamespace(returncode=1, stdout='', stderr='no such service: asterisk')
        code, out, run = self.main(['call', '--env-file', str(self.env), '--wav', str(ok)], FakeRun({'cp': failed}))
        self.assertEqual((code, out[-1]['step']), (1, 'copy capture'))
        self.assertEqual(len(run.calls), 1, 'no call after a failed copy')
        code, out, _ = self.main(['call', '--env-file', str(self.env), '--wav', str(ok)],
                                 FakeRun({'sipp': SimpleNamespace(returncode=1, stdout='488 Not Acceptable', stderr='')}))
        self.assertEqual((code, out[-1]['step']), (1, 'sipp call'))
        self.assertIn('488', out[-1]['detail'])

    def test_ld04_egress_is_proven_only_with_no_default_route_and_a_failed_connection(self):
        def probe(**fields):
            return SimpleNamespace(returncode=0, stdout=json.dumps(fields) + '\n', stderr='')
        isolated = probe(default_route=False, connected=False, error='OSError', errno=101)
        code, out, run = self.main(['egress', '--env-file', str(self.env)], FakeRun({'python': isolated}))
        self.assertEqual(code, 0)
        self.assertEqual([(r['service'], r['blocked']) for r in out], [('live', True), ('api', True)])
        self.assertIn('no default route', out[0]['result'])
        self.assertEqual([c[c.index('exec') + 2] for c in run.calls], ['live', 'api'])
        # Review M2: an offline host (timeout) with a default route is NOT proof.
        for result, expected in (
                (probe(default_route=True, connected=False, error='TimeoutError', errno=None), 'not proven'),
                (probe(default_route=None, connected=False, error='OSError', errno=101), 'not proven'),
                (probe(default_route=False, connected=True), 'REACHED'),
                (SimpleNamespace(returncode=1, stdout='', stderr='service "api" is not running'), 'probe did not run'),
                (SimpleNamespace(returncode=0, stdout='blocked OSError\n', stderr=''), 'probe did not run'),
                (SimpleNamespace(returncode=0, stdout='', stderr=''), 'probe did not run')):
            with self.subTest(expected=expected, stdout=result.stdout):
                code, out, _ = self.main(['egress', '--env-file', str(self.env)], FakeRun({'python': result}))
                self.assertEqual(code, 1)
                self.assertTrue(all(r['result'].startswith(expected) for r in out), out)
                self.assertFalse(any(r['blocked'] for r in out))

    def test_ld04c_route_tables_a_real_default_route_counts_a_reject_route_does_not(self):
        """Re-review R2: the probe on synthetic /proc tables. The IPv6 ::/0 REJECT route on
        lo (flags 0x00200200), present in every container, is not a route out."""
        import subprocess
        import sys
        v4_none = 'Iface\tDestination\tGateway\tFlags\neth0\t0000FEA9\t00000000\t0001\n'
        v4_default = v4_none + 'eth0\t00000000\t0100FEA9\t0003\n'
        zero = '0' * 32
        reject = f'{zero} 00 {zero} 00 {zero} ffffffff 00000001 00000000 00200200       lo\n'
        real_v6 = f'{zero} 00 {zero} 00 fe800000000000000000000000000001 00000400 00000001 00000000 00000003     eth0\n'
        for v4, v6, expected in ((v4_none, reject, (False, False)), (v4_default, reject, (True, False)),
                                 (v4_none, reject + real_v6, (True, True)), (v4_none, None, (False, False))):
            with self.subTest(v4=v4 is v4_default, v6=v6):
                route4, route6 = self.folder / 'route', self.folder / 'ipv6_route'
                route4.write_text(v4, encoding='utf-8')
                if v6 is None:
                    route6.unlink(missing_ok=True)
                else:
                    route6.write_text(v6, encoding='utf-8')
                script = (demo.EGRESS_PROBE.replace('"/proc/net/route"', repr(str(route4)))
                          .replace('"/proc/net/ipv6_route"', repr(str(route6)))
                          .replace('"1.1.1.1", 443', '"127.0.0.1", 9'))
                done = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, timeout=30)
                result = json.loads(done.stdout)
                self.assertEqual((result['default_route'], result['default_route_v6']), expected, result)
                self.assertEqual(demo.egress_verdict(done.stdout, 0)[0], not expected[0])

    def test_ld04b_the_probe_itself_reports_route_and_connection(self):
        """The probe script runs here (no Docker): it prints one JSON line with both keys."""
        import subprocess
        import sys
        done = subprocess.run([sys.executable, '-c', demo.EGRESS_PROBE.replace('"1.1.1.1", 443', '"127.0.0.1", 9')],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        result = json.loads(done.stdout)
        self.assertIn('default_route', result)
        self.assertIn('default_route_v6', result)
        if result['default_route_v6']:
            self.assertTrue(result['default_route'], 'an IPv6 default route counts as a route')
        self.assertFalse(result['connected'])  # nothing listens on the discard port here
        self.assertIn('error', result)

    def test_ld05_status_needs_healthy_services_the_forwarder_and_a_ready_dashboard(self):
        rows = [{'Service': 'postgres', 'State': 'running', 'Health': 'healthy'},
                {'Service': 'redis', 'State': 'running', 'Health': 'healthy'},
                {'Service': 'api', 'State': 'running', 'Health': 'healthy'},
                {'Service': 'live', 'State': 'running', 'Health': 'healthy'},
                {'Service': 'db-setup', 'State': 'exited', 'Health': ''},
                {'Service': 'asterisk', 'State': 'running', 'Health': ''},
                {'Service': 'demo-proxy', 'State': 'running', 'Health': 'healthy'}]

        class Response(io.BytesIO):
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False
        ready = lambda url, timeout: Response(json.dumps({'model_id': 'baseline'}).encode())  # noqa: E731

        def ps(rows):
            return FakeRun({'ps': SimpleNamespace(returncode=0, stdout='\n'.join(json.dumps(r) for r in rows),
                                                  stderr='')})
        code, out, _ = self.main(['status', '--env-file', str(self.env)], ps(rows), urlopen=ready)
        self.assertEqual(code, 0, out)
        self.assertEqual(out[-1], {'event': 'dashboard', 'url': 'http://127.0.0.1:8080/#live', 'ready': True,
                                   'model_id': 'baseline'})
        unhealthy = [dict(r, Health='unhealthy') if r['Service'] == 'live' else r for r in rows]
        self.assertEqual(self.main(['status', '--env-file', str(self.env)], ps(unhealthy), urlopen=ready)[0], 1)
        for missing in ('demo-proxy', 'live', 'api', 'asterisk'):
            with self.subTest(missing=missing):  # review L1: a stopped or absent service fails
                code, out, run = self.main(['status', '--env-file', str(self.env)],
                                           ps([r for r in rows if r['Service'] != missing]), urlopen=ready)
                self.assertEqual(code, 1)
                self.assertIn({'event': 'not_ready', 'missing': [missing], 'unhealthy': []}, out)
                self.assertIn('--all', run.calls[0], 'stopped containers are listed too')
        stopped = [dict(r, State='exited', Health='') if r['Service'] == 'live' else r for r in rows]
        self.assertEqual(self.main(['status', '--env-file', str(self.env)], ps(stopped), urlopen=ready)[0], 1)
        code, out, _ = self.main(['status', '--env-file', str(self.env), '--monitoring'], ps(rows), urlopen=ready)
        self.assertEqual(code, 1, 'with --monitoring, prometheus and grafana are required')
        self.assertIn({'event': 'not_ready', 'missing': ['prometheus', 'grafana'], 'unhealthy': []}, out)

        def refused(url, timeout):
            raise ConnectionRefusedError()
        code, out, _ = self.main(['status', '--env-file', str(self.env), '--demo-port', '18080'], ps(rows),
                                 urlopen=refused)
        self.assertEqual(code, 1)
        self.assertEqual(out[-1], {'event': 'dashboard', 'url': 'http://127.0.0.1:18080/readyz', 'ready': False,
                                   'error': 'ConnectionRefusedError'})


if __name__ == '__main__':
    unittest.main()
