"""Command-line entry point of the live service (backlog 8.1; PRD FR-O4, FR-T2, FR-A8;
architecture §2.1-§2.4).

    python -m services.live --card model_card.json --onnx ava.onnx --vad-model silero_vad.onnx \
        --ari-url http://127.0.0.1:8088 --ari-user ava --receiver-host 10.0.0.5 [--scoring-config ava.json]

The ARI password is read from an environment variable (`--ari-password-env`, default
`AVA_ARI_PASSWORD`), never from the command line or the URL, and is never printed.
Start-up checks, in order, before any call is tapped: the password; the receiver/bind
addresses and tap extensions; the scoring config; `load_detector` (card, hash, graph,
warm-up); one Silero VAD (pinned hash); and one authenticated ARI request. Wrong ARI
credentials (HTTP 401/403) refuse; an unreachable Asterisk does not (the loop keeps
reconnecting; `ari_reachable: false` in the start record). Every refusal prints one
JSON line and exits 2 (no traceback); Ctrl+C during start-up exits 130.

Running: the first SIGINT/SIGTERM (SIGBREAK on Windows) stops the loop (queued ends
complete, every other call ends `monitor_lost`, exit 0); the default handlers are then
back, so a second Ctrl+C forces the exit (130). `starting` (process up, model loaded,
not yet `ready`: readiness is the ARI stream, FR-A8) and `stopped` records with the
counts go to stderr as single JSON lines.

Not here yet: PostgreSQL/Redis hooks, `/readyz` and metrics for this process.
"""
import argparse
import ipaddress
import json
import os
import re
import signal
import socket
import sys
import threading

EXIT_OK, EXIT_REFUSED, EXIT_INTERRUPTED = 0, 2, 130
MAX_CALLS_LIMIT = 64
_HOSTNAME = re.compile(r'^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?'
                       r'(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$')
_EXTENSION = re.compile(r'^[0-9A-Za-z*#+_-]{1,32}$')


def _bounded_int(low, high):
    def parse(text):
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f'not an integer: {text!r}') from None
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(f'must be from {low} to {high}')
        return value
    return parse


def _positive_float(text):
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f'not a number: {text!r}') from None
    if not (value > 0 and value < float('inf')):
        raise argparse.ArgumentTypeError('must be a finite number above 0')
    return value


def _ipv4(text):
    try:
        return ipaddress.IPv4Address(text)
    except ValueError:
        return None


def _receiver_host(text):
    """Where Asterisk sends tapped RTP: an IPv4 address or a host name (IPv6 is not
    supported by the host:port the tap gives ExternalMedia)."""
    address = _ipv4(text)
    if address is not None:
        if address.is_unspecified or address.is_multicast or text == '255.255.255.255':
            raise argparse.ArgumentTypeError('must be an address Asterisk can send to (not 0.0.0.0/multicast)')
        return text
    if ':' in text or not _HOSTNAME.match(text) or text.replace('.', '').isdigit():
        raise argparse.ArgumentTypeError('must be an IPv4 address or a host name (IPv6 not supported)')
    return text


def _bind_host(text):
    if _ipv4(text) is None:
        raise argparse.ArgumentTypeError('must be a local IPv4 address (0.0.0.0 for all)')
    return text


def _extension(text):
    if not _EXTENSION.match(text):
        raise argparse.ArgumentTypeError('must be 1-32 of 0-9 A-Z a-z * # + _ -')
    return text


def _env_name(text):
    if not re.match(r'^[A-Za-z_][A-Za-z0-9_]{0,63}$', text):
        raise argparse.ArgumentTypeError('must be an environment variable name')
    return text


def build_parser():
    parser = argparse.ArgumentParser(prog='python -m services.live', description='AVA live tap scoring service')
    parser.add_argument('--card', required=True, help='model card JSON (models/export/model_card.py)')
    parser.add_argument('--onnx', required=True, help='exported detector named by the card')
    parser.add_argument('--scoring-config', help='scoring JSON document (ava.yaml structure); defaults if absent')
    parser.add_argument('--per-window', action='store_true', help='run the detector one window at a time')
    parser.add_argument('--threads', type=_bounded_int(1, 16), default=1, help='ONNX intra-op threads')
    parser.add_argument('--vad-model', required=True, help='pinned Silero VAD ONNX file')
    parser.add_argument('--ari-url', required=True, help='Asterisk ARI base URL, e.g. http://127.0.0.1:8088')
    parser.add_argument('--ari-user', required=True)
    parser.add_argument('--ari-password-env', type=_env_name, default='AVA_ARI_PASSWORD',
                        help='environment variable with the password')
    parser.add_argument('--ari-app', default='ava-tap')
    parser.add_argument('--receiver-host', type=_receiver_host, required=True,
                        help='IPv4 address or host name Asterisk sends tapped RTP to')
    parser.add_argument('--bind-host', type=_bind_host, default='0.0.0.0', help='local IPv4 address for RTP receivers')
    parser.add_argument('--max-calls', type=_bounded_int(1, MAX_CALLS_LIMIT), default=8)
    parser.add_argument('--capacity-windows-per-s', type=_positive_float, default=4.0)
    parser.add_argument('--tap-extension', type=_extension, action='append', dest='tap_extensions',
                        help='only tap calls to this dialled extension (repeatable); default: every marked call')
    return parser


def _check_bind(host):
    """One UDP bind on an ephemeral port: an address this machine does not have fails
    now, not on the first call."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind((host, 0))


def _defaults():
    from services.inference.silero_vad import SileroVad
    from services.live.service import LiveService, load_detector
    from services.tap.ari import AriClient
    return {'load_detector': load_detector, 'vad': SileroVad, 'ari': AriClient, 'service': LiveService,
            'bind_check': _check_bind}


def _say(stream, record):
    stream.write(json.dumps(record, sort_keys=True) + '\n')
    stream.flush()


def main(argv=None, *, environ=None, factories=None, stop_event=None, stderr=None):
    """Returns the exit code. `factories` replaces load_detector/vad/ari/service/
    bind_check in tests."""
    environ = os.environ if environ is None else environ
    stderr = sys.stderr if stderr is None else stderr
    args = build_parser().parse_args(argv)  # usage errors exit 2 through argparse
    try:
        prepared = _start(args, environ, factories, stderr)
    except KeyboardInterrupt:
        _say(stderr, {'event': 'interrupted', 'phase': 'start-up'})
        return EXIT_INTERRUPTED
    if isinstance(prepared, int):
        return prepared
    service = prepared
    stop = stop_event or threading.Event()
    restore = _install_signals(stop.set)
    code = EXIT_OK
    try:
        service.run(stop)
    except KeyboardInterrupt:  # a second Ctrl+C while stopping
        code = EXIT_INTERRUPTED
    finally:
        restore()
    _say(stderr, {'event': 'stopped', 'forced': code != EXIT_OK, 'service': dict(service.counts),
                  'tap': dict(service.tap.stats), 'sink': dict(service.sink.counts),
                  'pipeline': dict(service.pipeline.counts)})
    return code


def _start(args, environ, factories, stderr):
    """Every start-up check; returns the built service or an exit code."""
    def refuse(message):
        _say(stderr, {'event': 'refused', 'reason': message})
        return EXIT_REFUSED

    password = environ.get(args.ari_password_env)
    if not password:
        return refuse(f'ARI password not set: put it in the {args.ari_password_env} environment variable')
    try:
        made = factories or _defaults()
        from services.live.service import StartupError
        from services.scorer.config import ConfigError, fingerprint, read_json_document
        from services.tap.ari import AriError
    except ImportError as error:
        return refuse(f'missing dependency: {error.name or error}')
    try:
        made['bind_check'](args.bind_host)
    except OSError as error:
        return refuse(f'bind host {args.bind_host}: {type(error).__name__}: {error.strerror or error}')
    document = {}
    if args.scoring_config:
        try:
            document = read_json_document(args.scoring_config)
        except (OSError, ConfigError) as error:
            return refuse(f'scoring config: {type(error).__name__}: {error}')
        if not isinstance(document, dict):
            return refuse('scoring config: the document must be a JSON object')
    try:
        detector, card, config = made['load_detector'](args.card, args.onnx, document, threads=args.threads,
                                                       per_window=args.per_window)
    except StartupError as error:
        return refuse(str(error))
    try:
        made['vad'](args.vad_model)  # fail now on a missing, unpinned or unloadable model
    except Exception as error:  # onnxruntime errors are not OSError/ValueError
        return refuse(f'VAD: {type(error).__name__}: {error}')
    try:
        ari = made['ari'](args.ari_url, args.ari_user, password, args.ari_app)
    except ValueError as error:
        return refuse(f'ARI: {error}')
    try:
        ari.list_bridges()  # one authenticated request
        reachable = True
    except AriError as error:
        if error.status in (401, 403):
            return refuse(f'ARI refused the credentials (HTTP {error.status})')
        reachable = False  # Asterisk down or restarting: the loop keeps reconnecting
    vad_model = args.vad_model
    try:
        service = made['service'](ari, detector, config, lambda: made['vad'](vad_model),
                                  receiver_host=args.receiver_host, bind_host=args.bind_host,
                                  max_calls=args.max_calls, capacity_windows_per_s=args.capacity_windows_per_s,
                                  tap_extensions=set(args.tap_extensions) if args.tap_extensions else None)
    except (ValueError, TypeError) as error:
        return refuse(f'service: {error}')
    _say(stderr, {'event': 'starting', 'model_version': card['version'], 'onnx_sha256': card['onnx']['sha256'],
                  'config_fingerprint': fingerprint(config), 'per_window': args.per_window, 'threads': args.threads,
                  'max_calls': args.max_calls, 'ari_url': args.ari_url.rstrip('/'), 'ari_app': args.ari_app,
                  'ari_reachable': reachable, 'tap_extensions': sorted(args.tap_extensions or [])})
    return service


def _install_signals(on_signal):
    """Signal handlers only work in the main thread; elsewhere (tests) nothing changes.
    The first signal calls `on_signal` and restores the previous handlers, so a second
    Ctrl+C interrupts a stalled shutdown. On Windows, Ctrl+C / Ctrl+Break in the console
    are the dependable stop signals."""
    if threading.current_thread() is not threading.main_thread():
        return lambda: None
    names = ['SIGINT', 'SIGTERM'] + (['SIGBREAK'] if hasattr(signal, 'SIGBREAK') else [])
    previous = {}

    def restore():
        for number, handler in previous.items():
            signal.signal(number, handler)

    def handle(signum, frame):
        restore()
        on_signal()
    for name in names:
        number = getattr(signal, name)
        previous[number] = signal.signal(number, handle)
    return restore


if __name__ == '__main__':
    sys.exit(main())
