"""Lab demo helper (backlog 11.2; PRD roadmap 5.9 "demo script and rehearsal: live tapped
call, batch upload, timeline, egress proof"; BO-4, BO-8). See docs/operations/demo.md.

    python -m lab.demo status --env-file D:/AVA-Data/deploy/demo.env
    python -m lab.demo call   --env-file D:/AVA-Data/deploy/demo.env --wav a.wav [--wav b.wav] [--codec alaw]
    python -m lab.demo egress --env-file D:/AVA-Data/deploy/demo.env

Works on a running Compose stack started with `--profile lab --profile demo`
(deploy/compose/README.md). It never starts or stops the stack and never reads the env
file's contents: it passes the path to `docker compose`, which does.

- `status`: each service's state and health, and the dashboard's `/readyz` through the
  loopback demo port.
- `call`: joins the WAV files, converts them to G.711 RTP (lab/asterisk/rtp_pcap.py),
  copies the capture into the lab PBX and has SIPp call the tapped extension with it.
  The lab dialplan's Record() hangs up at 60 s, so more than 55 s of audio is refused.
  One call at a time: the SIPp scenario plays the fixed file /lab/io/inject.pcap.
- `egress`: from inside `live` and `api`, the container must have no default route (read
  from /proc/net/route and /proc/net/ipv6_route) AND a TCP connection to 1.1.1.1:443 must fail. Review M2: a failed
  connection alone is not proof (an offline laptop or an upstream firewall also fails
  it), so a container with a default route is "not proven" even when the connection
  fails. Exit 1 unless both services are proven isolated.

One JSON record per step on stdout. Exit 0 on success, 1 on a failed step, 2 on a
refusal (bad arguments, missing file, audio too long, docker not found).
"""
import argparse
import json
import secrets
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / 'deploy' / 'compose' / 'compose.yaml'
EXIT_OK, EXIT_FAILED, EXIT_REFUSED = 0, 1, 2
MAX_CALL_MS = 55_000  # the lab dialplan's Record() ends the call at 60 s
CODECS = ('ulaw', 'alaw')  # G.711 needs no external encoder; GSM needs the pinned ffmpeg
EGRESS_SERVICES = ('live', 'api')
# Services `status` requires running (db-setup is a one-shot job and may have exited).
REQUIRED_SERVICES = ('postgres', 'redis', 'asterisk', 'live', 'api', 'demo-proxy')
MONITORING_SERVICES = ('prometheus', 'grafana')
# Run inside a container (python -c): one JSON line with whether it has a default route
# (Linux /proc/net/route, destination 00000000) and whether 1.1.1.1:443 connected.
EGRESS_PROBE = (
    'import json, socket\n'
    'r = {}\n'
    'try:\n'
    '    with open("/proc/net/route") as f:\n'
    '        rows = [line.split() for line in f.readlines()[1:]]\n'
    '    r["default_route"] = any(len(x) > 1 and x[1] == "00000000" for x in rows)\n'
    'except OSError:\n'
    '    r["default_route"] = None\n'
    # Re-review R2: IPv6 too (destination all zeros, prefix length 00); no file = no IPv6.
    # The kernel keeps a ::/0 REJECT route on lo (flags & 0x0200, RTF_REJECT) when there
    # is no real one; that is "unreachable", not a route out (seen on 22 Nov).
    'try:\n'
    '    with open("/proc/net/ipv6_route") as f:\n'
    '        v6 = any(x[0] == "0" * 32 and x[1] == "00" and not int(x[8], 16) & 0x0200\n'
    '                 for x in (l.split() for l in f) if len(x) > 9)\n'
    '    r["default_route_v6"] = v6\n'
    '    if v6 and r["default_route"] is False:\n'
    '        r["default_route"] = True\n'
    'except FileNotFoundError:\n'
    '    r["default_route_v6"] = False\n'
    'except OSError:\n'
    '    r["default_route_v6"] = None\n'
    '    r["default_route"] = None\n'
    'try:\n'
    '    socket.create_connection(("1.1.1.1", 443), timeout=3).close()\n'
    '    r["connected"] = True\n'
    'except OSError as e:\n'
    '    r.update(connected=False, error=type(e).__name__, errno=e.errno)\n'
    'print(json.dumps(r))\n')


def egress_verdict(stdout, returncode):
    """(isolated, result text) from the probe's output."""
    try:
        probe = json.loads(stdout.strip().splitlines()[-1]) if returncode == 0 else None
    except (ValueError, IndexError):
        probe = None
    if not isinstance(probe, dict) or 'connected' not in probe:
        return False, 'probe did not run'
    if probe['connected']:
        return False, 'REACHED 1.1.1.1:443'
    if probe.get('default_route') is False:
        return True, f'no default route; connection failed ({probe.get("error")})'
    return False, (f'not proven: the connection failed ({probe.get("error")}) but the container '
                   f'{"has a default route" if probe.get("default_route") else "route table is unreadable"}; '
                   'an offline host or an upstream firewall would also fail it')


class Refused(Exception):
    pass


def compose_base(docker, env_file, project, *, monitoring=False, compose_file=COMPOSE_FILE):
    args = [docker, 'compose', '--env-file', str(env_file), '-f', str(compose_file), '-p', project,
            '--profile', 'lab', '--profile', 'demo']
    return args + (['--profile', 'monitoring'] if monitoring else [])


def sipp_command(base, *, caller, payload_type, codec_name, call_ms, extension):
    """The SIPp call inside the lab PBX container (lab/asterisk/sipp/uac_inject.xml), as the
    lab tests place it: one call, the capture played, hang-up 1.5 s after it ends."""
    return base + ['exec', '-T', 'asterisk', 'sipp', '127.0.0.1:5060', '-sf', '/lab/sipp/uac_inject.xml',
                   '-s', str(extension), '-key', 'caller', caller, '-key', 'pt', str(payload_type),
                   '-key', 'codec', codec_name, '-m', '1', '-d', str(call_ms + 1500), '-i', '127.0.0.1',
                   '-mi', '127.0.0.1', '-min_rtp_port', '16000', '-max_rtp_port', '16100', '-timeout', '90s',
                   '-timeout_error', '-nostdin']


def write_capture(wavs, codec, out_path):
    """WAV files -> one G.711 RTP capture. Returns (packets, payload type, codec name)."""
    from lab.asterisk import rtp_pcap
    if codec not in CODECS:
        raise Refused(f'codec must be one of {", ".join(CODECS)}')
    for wav in wavs:
        if not Path(wav).is_file():
            raise Refused(f'not a file: {Path(wav).name}')
    parts = []
    for index, wav in enumerate(wavs):
        try:
            parts.append(rtp_pcap.wav_to_pcm8k(wav))
        except (ValueError, EOFError, wave.Error) as error:  # review nit: truncated files too
            reason = str(error).replace(str(wav), '').lstrip(': ') or type(error).__name__
            raise Refused(f'unusable WAV (file {index + 1}): {reason}') from None
    pcm = b''.join(parts)
    call_ms = len(pcm) // 2 * 1000 // rtp_pcap.RATE
    if call_ms > MAX_CALL_MS:
        raise Refused(f'{call_ms / 1000:.1f} s of audio; at most {MAX_CALL_MS // 1000} s fit in one lab call')
    payloads = rtp_pcap.rtp_payloads(pcm, codec)
    payload_type, name = rtp_pcap.PAYLOAD_TYPES[codec]
    rtp_pcap.write_pcap(payloads, payload_type, out_path)
    return len(payloads), payload_type, name


def build_parser():
    parser = argparse.ArgumentParser(prog='python -m lab.demo', description='AVA lab demo helper')
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--env-file', required=True, help='the stack env file (outside the repository)')
    common.add_argument('-p', '--project', default='ava', help='Compose project name (default ava)')
    common.add_argument('--monitoring', action='store_true', help='the stack also runs --profile monitoring')
    commands = parser.add_subparsers(dest='command', required=True)
    status = commands.add_parser('status', parents=[common], help='service health and the dashboard /readyz')
    status.add_argument('--demo-port', type=int, default=8080, help='AVA_DEMO_PORT (default 8080)')
    call = commands.add_parser('call', parents=[common], help='place one tapped lab call with WAV audio')
    call.add_argument('--wav', action='append', required=True, help='a PCM16 WAV file; repeat to join several')
    call.add_argument('--codec', choices=CODECS, default='ulaw')
    call.add_argument('--extension', default='700', help='the tapped extension (default 700)')
    commands.add_parser('egress', parents=[common], help='prove live and api cannot reach the internet')
    return parser


def _ps_states(stdout):
    text = stdout.strip()
    try:  # one object per line (Compose v2.21+), or one array (older versions)
        rows = json.loads(text) if text.startswith('[') else [json.loads(line) for line in text.splitlines() if line]
    except ValueError:
        return None
    return {row.get('Service'): {'state': row.get('State'), 'health': row.get('Health') or None} for row in rows}


def main(argv=None, *, run=None, stdout=None, which=shutil.which, urlopen=urllib.request.urlopen):
    stdout = stdout or sys.stdout
    args = build_parser().parse_args(argv)
    run = run or (lambda cmd, **kw: subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8',
                                                   errors='replace', **kw))

    def say(record):
        stdout.write(json.dumps(record, sort_keys=True) + '\n')
        stdout.flush()

    def refuse(reason):
        say({'event': 'refused', 'command': args.command, 'reason': reason})
        return EXIT_REFUSED

    def fail(step, detail):
        say({'event': 'failed', 'command': args.command, 'step': step, 'detail': detail[-1500:]})
        return EXIT_FAILED

    env_file = Path(args.env_file)
    if not env_file.is_file():
        return refuse('env file not found')
    docker = which('docker')
    if not docker:
        return refuse('docker not found on PATH')
    base = compose_base(docker, env_file.resolve(), args.project, monitoring=args.monitoring)

    if args.command == 'status':
        if not 1 <= args.demo_port <= 65535:
            return refuse('demo port must be 1-65535')
        done = run(base + ['ps', '--all', '--format', 'json'], timeout=60)  # review L1: stopped ones too
        if done.returncode != 0:
            return fail('ps', done.stderr or done.stdout)
        states = _ps_states(done.stdout)
        if states is None:
            return fail('ps', 'unreadable docker compose ps output')
        say({'event': 'services', 'services': states})
        url = f'http://127.0.0.1:{args.demo_port}/readyz'
        try:
            with urlopen(url, timeout=10) as response:
                body = json.loads(response.read())
                status = response.status
        except (urllib.error.URLError, OSError, ValueError) as error:
            say({'event': 'dashboard', 'url': url, 'ready': False, 'error': type(error).__name__})
            return EXIT_FAILED
        say({'event': 'dashboard', 'url': f'http://127.0.0.1:{args.demo_port}/#live', 'ready': status == 200,
             'model_id': body.get('model_id')})
        required = REQUIRED_SERVICES + (MONITORING_SERVICES if args.monitoring else ())
        missing = [name for name in required if name not in states]
        unhealthy = [name for name, s in states.items() if name != 'db-setup'
                     and not (s['health'] in (None, 'healthy') and s['state'] == 'running')]
        if missing or unhealthy:
            say({'event': 'not_ready', 'missing': missing, 'unhealthy': sorted(unhealthy)})
        return EXIT_OK if status == 200 and not missing and not unhealthy else EXIT_FAILED

    if args.command == 'call':
        if not str(args.extension).isdigit() or len(str(args.extension)) > 8:
            return refuse('extension must be digits')
        with tempfile.TemporaryDirectory(prefix='ava-demo-') as folder:
            capture = Path(folder) / 'inject.pcap'
            try:
                packets, payload_type, codec_name = write_capture(args.wav, args.codec, capture)
            except Refused as error:
                return refuse(str(error))
            call_ms = packets * 20
            done = run(base + ['cp', str(capture), 'asterisk:/lab/io/inject.pcap'], timeout=120)
            if done.returncode != 0:
                return fail('copy capture', done.stderr or done.stdout)
        caller = f'demo{secrets.token_hex(3)}'
        say({'event': 'calling', 'extension': str(args.extension), 'codec': codec_name, 'seconds': call_ms / 1000,
             'caller': caller})
        done = run(sipp_command(base, caller=caller, payload_type=payload_type, codec_name=codec_name,
                                call_ms=call_ms, extension=args.extension), timeout=call_ms / 1000 + 120)
        if done.returncode != 0:
            return fail('sipp call', done.stdout + done.stderr)
        say({'event': 'call_completed', 'caller': caller, 'seconds': call_ms / 1000})
        return EXIT_OK

    isolated = {}
    for service in EGRESS_SERVICES:
        done = run(base + ['exec', '-T', service, 'python', '-c', EGRESS_PROBE], timeout=60)
        isolated[service], result = egress_verdict(done.stdout, done.returncode)
        say({'event': 'egress', 'service': service, 'target': '1.1.1.1:443', 'result': result[:300],
             'blocked': isolated[service], 'exit_code': done.returncode})
    return EXIT_OK if all(isolated.values()) else EXIT_FAILED


if __name__ == '__main__':
    sys.exit(main())
