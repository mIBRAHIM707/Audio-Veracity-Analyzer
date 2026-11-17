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

Storage and fan-out (16 Nov, both optional; PRD FR-I11, FR-A3, FR-A4; architecture
§2.4): `--database-url-env NAME` names an environment variable holding a PostgreSQL
connection string (never on the command line, never printed). Start-up connects once
(5 s connect timeout, 30 s lock timeout) and applies the pending migrations
(`services/store/migrate.py`); an unreachable database, a refused login, a held
migration lock or a migration history mismatch refuses the start (exit 2), because
events written into a missing schema would be refused for good. Whether an
unreachable database should instead start the service degraded, and whether the
service should apply migrations at all (DDL rights), are open decisions (16 Nov
report). While running, a `StorePump` moves every event to PostgreSQL on its own
thread (`services/live/background.py`; TCP keepalives, 10 s statement and lock
timeouts); during an outage events wait, first in the pump backlog and then in the hub
buffer, whose oldest are dropped past 4096. `--redis-url-env NAME` likewise names a
Redis URL: a `RedisForwarder` publishes every event on `ava:events:v1` on its own
thread (1 s socket timeouts, no client retries); Redis down at start-up is reported
(`redis_reachable: false`), not refused (pub/sub is at-most-once). On stop both pumps
drain in parallel for at most `--drain-timeout` seconds (above 0, at most 300); their counts and
whether anything was left go into the `stopped` record.

Health (16 Nov; PRD FR-A8, FR-O6): `--health-port N` serves `/healthz`, `/readyz` and
`/metrics` (`services/live/health.py`) on `--health-host` (default 127.0.0.1; no
authentication). The port is bound during start-up, so a port in use refuses the start.
`/readyz` is 200 only while the ARI stream is connected, the scoring loop is turning and
the detector is not failing batch after batch; the start record adds `health`.

Alert snippets (17 Nov; PRD FR-O5): off by default, so no raw audio is stored.
`--retention on --retention-dir DIR --retention-key-file FILE [--retention-ttl-days 7]`
keeps a 30 s ring of each admitted call's tapped audio and writes one AES-256-GCM
snippet per `alert.raised` (`services/live/retention.py`; writing and TTL cleanup run
on their own `retention` pump thread). The key file holds 32 raw bytes, outside the
repository (provisioning and rotation are the operator's). `--retention off` with
`--retention-dir` purges that folder's snippets (FR-O5 "switching off purges"). A bad
key, a folder inside the repository or a missing cryptography package refuses the
start; the start record says `retention: off` or its mode and TTL, never a path or key.
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
import time

EXIT_OK, EXIT_FAILED, EXIT_REFUSED, EXIT_INTERRUPTED = 0, 1, 2, 130
JOIN_GRACE_S = 1.0
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


def _drain_seconds(text):
    value = _positive_float(text)
    if value > 300:
        raise argparse.ArgumentTypeError('must be at most 300 seconds')
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
    parser.add_argument('--database-url-env', type=_env_name,
                        help='environment variable with a PostgreSQL connection string (enables event storage)')
    parser.add_argument('--redis-url-env', type=_env_name,
                        help='environment variable with a Redis URL (enables event publishing)')
    parser.add_argument('--health-port', type=_bounded_int(1, 65535),
                        help='serve /healthz, /readyz and /metrics on this TCP port (FR-A8); off if absent')
    parser.add_argument('--health-host', type=_bind_host, default='127.0.0.1',
                        help='local IPv4 address for the health endpoint (default 127.0.0.1; no authentication)')
    parser.add_argument('--drain-timeout', type=_drain_seconds, default=5.0,
                        help='seconds the storage/publish pumps may drain on stop (above 0, at most 300; default 5)')
    parser.add_argument('--retention', choices=('off', 'on'), default='off',
                        help='alert audio snippets (FR-O5); default off: no raw audio is stored')
    parser.add_argument('--retention-dir', help='snippet folder outside the repository; with --retention off, '
                                                'existing snippets there are purged')
    parser.add_argument('--retention-key-file', help='raw 32-byte AES-256-GCM key file outside the repository '
                                                     '(needed with --retention on)')
    parser.add_argument('--retention-ttl-days', type=_ttl_days, default=7.0,
                        help='days a snippet is kept (above 0, at most 365; default 7)')
    return parser


def _ttl_days(text):
    value = _positive_float(text)
    if value > 365:
        raise argparse.ArgumentTypeError('must be at most 365 days')
    return value


def _check_bind(host):
    """One UDP bind on an ephemeral port: an address this machine does not have fails
    now, not on the first call."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind((host, 0))


CONNECT_TIMEOUT_S = 5
# A database that stops answering after connecting must not hang the store thread for
# the OS TCP timeout (review M3): keepalives (tcp_user_timeout only where the OS has it)
# plus per-session statement/lock timeouts, set after connecting so a connection
# string's own `options` (e.g. search_path) is kept.
KEEPALIVES = {'keepalives': 1, 'keepalives_idle': 5, 'keepalives_interval': 2, 'keepalives_count': 3,
              'tcp_user_timeout': 10_000}
STORE_TIMEOUTS_MS = {'statement_timeout': 10_000, 'lock_timeout': 10_000}
MIGRATION_LOCK_TIMEOUT_MS = 30_000
REDIS_TIMEOUT_S = 1.0


def _session(connection, timeouts):
    for name, value in timeouts.items():
        connection.execute(f'SET {name} = {int(value)}')  # names from the constants above only
    connection.commit()  # back to idle: the store refuses connections inside a transaction
    return connection


def _open_store(dsn):
    """Connect once, apply pending migrations, return (PostgresEventStore, applied
    versions). Raises ImportError, MigrationError or the driver's errors (a held
    migration lock past 30 s raises the driver's LockNotAvailable)."""
    import psycopg
    from services.store.events_pg import PostgresEventStore
    from services.store.migrate import apply_migrations

    from psycopg.conninfo import conninfo_to_dict
    # Defaults only where the connection string sets nothing (an operator can tune them).
    given = conninfo_to_dict(dsn)
    defaults = {key: value for key, value in {'connect_timeout': CONNECT_TIMEOUT_S, **KEEPALIVES}.items()
                if key not in given}

    def raw():
        return psycopg.connect(dsn, **defaults)

    def connect():
        connection = raw()
        try:
            return _session(connection, STORE_TIMEOUTS_MS)
        except BaseException:
            connection.close()
            raise
    with raw() as connection:
        _session(connection, {'lock_timeout': MIGRATION_LOCK_TIMEOUT_MS})
        applied = apply_migrations(connection)
    return PostgresEventStore(connect), applied


def _open_redis(url):
    """Return (client, reachable). Raises ImportError, or ValueError for a bad URL.
    No client-side retries (redis-py retries by default): one attempt per publish
    batch stays within the 1 s socket timeouts."""
    import redis
    from redis.backoff import NoBackoff
    from redis.retry import Retry
    client = redis.Redis.from_url(url, socket_timeout=REDIS_TIMEOUT_S, socket_connect_timeout=REDIS_TIMEOUT_S,
                                  retry=Retry(NoBackoff(), 0))
    try:
        client.ping()
        return client, True
    except Exception:  # down or refusing: the forwarder counts failures while running
        return client, False


def _defaults():
    from services.inference.silero_vad import SileroVad
    from services.live.service import LiveService, load_detector
    from services.tap.ari import AriClient
    return {'load_detector': load_detector, 'vad': SileroVad, 'ari': AriClient, 'service': LiveService,
            'bind_check': _check_bind, 'store': _open_store, 'redis': _open_redis}


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
    service, pumps, health = prepared
    stop = stop_event or threading.Event()
    restore = _install_signals(stop.set)
    code, error = EXIT_OK, None
    try:
        if health is not None:
            health.start()
        for pump in pumps:
            pump['thread'].start()
        service.run(stop)
    except KeyboardInterrupt:  # a second Ctrl+C while stopping
        code = EXIT_INTERRUPTED
    except Exception as failure:  # run() contains its own faults; this is a last guard
        code, error = EXIT_FAILED, type(failure).__name__
    finally:
        restore()
    code = _stop_pumps(pumps, args.drain_timeout, code)
    if health is not None:
        _close_quietly(health)  # last, so the pumps' drain stays observable
    record = {'event': 'stopped', 'forced': code == EXIT_INTERRUPTED, 'service': dict(service.counts),
              'tap': dict(service.tap.stats), 'sink': dict(service.sink.counts),
              'pipeline': dict(service.pipeline.counts)}
    if error:
        record['error'] = error
    for pump in pumps:
        record[pump['thread'].name] = {**pump['thread'].snapshot(), **pump['report']()}
    _say(stderr, record)
    return code


def _stop_pumps(pumps, drain_s, code):
    """After `run`: its final monitor_lost ends are in the hub. Every pump is asked to
    drain first (in parallel, one deadline), then joined; a forced stop drains nothing.
    A Ctrl+C here forces the rest (review M4). A stuck pump keeps its client open."""
    drain = 0.0 if code == EXIT_INTERRUPTED else drain_s
    try:
        for pump in pumps:
            pump['thread'].request_stop(drain)
        deadline = time.monotonic() + drain + JOIN_GRACE_S
        for pump in pumps:
            # Short slices: a long Thread.join cannot be interrupted by Ctrl+C on Windows.
            while not pump['thread'].join(min(0.1, deadline - time.monotonic())) \
                    and time.monotonic() < deadline:
                pass
            if not pump['thread'].alive:
                _close_quietly(pump['resource'])
    except KeyboardInterrupt:
        for pump in pumps:
            pump['thread'].request_stop(0)
        for pump in pumps:  # one short slice each, so the record reflects the forced stop
            if pump['thread'].join(0.1):
                _close_quietly(pump['resource'])
        return EXIT_INTERRUPTED
    return code


def _start(args, environ, factories, stderr):
    """Every start-up check; returns (service, pumps) or an exit code. Whatever was
    opened (store, Redis client) is closed on a refusal and on any unexpected exit,
    including Ctrl+C (review L5)."""
    opened = []
    try:
        return _prepare(args, environ, factories, stderr, opened)
    except BaseException:
        for resource in opened:
            _close_quietly(resource)
        raise


def _prepare(args, environ, factories, stderr, opened):
    def refuse(message):
        for resource in opened:
            _close_quietly(resource)
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
    store = applied = redis_client = None
    redis_reachable = None
    if args.database_url_env:
        dsn = environ.get(args.database_url_env)
        if not dsn:
            return refuse(f'database connection string not set: put it in the {args.database_url_env} '
                          'environment variable')
        try:
            store, applied = made['store'](dsn)
        except ImportError as error:
            return refuse(f'missing dependency: {error.name or error} (requirements-store.txt)')
        except Exception as error:
            from services.store.migrate import MigrationError
            if isinstance(error, MigrationError):
                return refuse(f'database migrations: {error}')
            # Driver messages can echo connection details: the type name only.
            return refuse(f'database unreachable or refused the login: {type(error).__name__}')
        opened.append(store)
    if args.redis_url_env:
        url = environ.get(args.redis_url_env)
        if not url:
            return refuse(f'Redis URL not set: put it in the {args.redis_url_env} environment variable')
        try:
            redis_client, redis_reachable = made['redis'](url)
        except ImportError as error:
            return refuse(f'missing dependency: {error.name or error} (requirements-redis.txt)')
        except ValueError:  # the URL itself is never printed
            return refuse('Redis URL is not valid')
        except Exception as error:  # any other client error: the type name only
            return refuse(f'Redis client: {type(error).__name__}')
        opened.append(redis_client)
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
    retention = _retention(args)
    if isinstance(retention, str):
        return refuse(retention)
    vad_model = args.vad_model
    extra = {}
    if retention is not None:
        snippet_store, policy = retention
        from services.live.retention import SnippetRecorder
        extra['recorder_factory'] = lambda hub, live: SnippetRecorder(snippet_store, policy, hub, live)
    try:
        service = made['service'](ari, detector, config, lambda: made['vad'](vad_model),
                                  receiver_host=args.receiver_host, bind_host=args.bind_host,
                                  max_calls=args.max_calls, capacity_windows_per_s=args.capacity_windows_per_s,
                                  tap_extensions=set(args.tap_extensions) if args.tap_extensions else None, **extra)
    except (ValueError, TypeError) as error:
        return refuse(f'service: {error}')
    # Subscribed before `run`, so no event is published before the pumps listen.
    from services.events.fanout import MAX_BUFFER
    from services.live.background import PumpThread
    pumps = []
    if store is not None:
        from services.store.pump import StorePump
        subscription = service.hub.subscribe(buffer=MAX_BUFFER)
        store_pump = StorePump(subscription, store)
        # Events not yet stored: the pump backlog plus what waits in the hub buffer.
        pumps.append({'thread': PumpThread('store', store_pump.pump,
                                           pending=lambda: store_pump.backlog + subscription.buffered()),
                      'resource': store,
                      'report': lambda: {'counts': dict(store_pump.counts), 'backlog': store_pump.backlog,
                                         'consecutive_failures': store_pump.consecutive_failures}})
    if redis_client is not None:
        from services.events.redis_bridge import RedisForwarder
        channel_feed = service.hub.subscribe(buffer=MAX_BUFFER)
        forwarder = RedisForwarder(channel_feed, redis_client)
        pumps.append({'thread': PumpThread('redis', forwarder.pump, pending=channel_feed.buffered),
                      'resource': redis_client, 'report': lambda: {'counts': dict(forwarder.stats)}})
    recorder = getattr(service, 'recorder', None)
    if retention is not None and recorder is not None:
        # Seals/writes snippets and runs the TTL/purge cleanup off the scoring thread.
        pumps.append({'thread': PumpThread('retention', recorder.write_pending, pending=recorder.pending),
                      'resource': None, 'report': recorder.report})
    health = None
    if args.health_port is not None:
        try:
            health = made.get('health', _health_server)(service, pumps, args.health_host, args.health_port)
        except OSError as error:  # port in use or address not local
            return refuse(f'health endpoint {args.health_host}:{args.health_port}: '
                          f'{type(error).__name__}: {error.strerror or error}')
        opened.append(health)
    record = {'event': 'starting', 'model_version': card['version'], 'onnx_sha256': card['onnx']['sha256'],
              'config_fingerprint': fingerprint(config), 'per_window': args.per_window, 'threads': args.threads,
              'max_calls': args.max_calls, 'ari_url': args.ari_url.rstrip('/'), 'ari_app': args.ari_app,
              'ari_reachable': reachable, 'tap_extensions': sorted(args.tap_extensions or [])}
    if store is not None:
        record['database_migrations_applied'] = applied
    if redis_client is not None:
        record['redis_reachable'] = redis_reachable
    if health is not None:
        host, port = health.address
        record['health'] = {'host': host, 'port': port}
    record['retention'] = ('off' if retention is None else
                           {'mode': 'on' if retention[1].enabled else 'purge', 'ttl_days': args.retention_ttl_days})
    _say(stderr, record)
    return service, pumps, health


def _retention(args):
    """(SnippetStore, RetentionPolicy), None when retention is off and no folder is
    given (nothing stored, nothing to purge), or a refusal message. Paths and key bytes
    are never printed."""
    if args.retention == 'off' and args.retention_key_file:
        return '--retention-key-file is only used with --retention on'
    if args.retention == 'on' and not (args.retention_dir and args.retention_key_file):
        return '--retention on needs --retention-dir and --retention-key-file'
    if not args.retention_dir:
        return None
    try:
        from services.retention.snippets import AesGcmSealer, RetentionPolicy, SnippetStore
        policy = RetentionPolicy(args.retention == 'on', ttl_s=args.retention_ttl_days * 86400.0)
        if args.retention == 'off':  # FR-O5: switching off purges existing snippets
            return SnippetStore.for_cleanup(args.retention_dir, clock=time.time), policy
        sealer = AesGcmSealer.from_key_file(args.retention_key_file)
        return SnippetStore(args.retention_dir, sealer, clock=time.time), policy
    except RuntimeError as error:  # the cryptography package is missing
        return f'retention: {error}'
    except (OSError, ValueError) as error:  # messages name the rule, never the key or path
        return f'retention: {type(error).__name__}: {error if isinstance(error, ValueError) else error.strerror}'


def _health_server(service, pumps, host, port):
    from services.live.health import HealthServer
    return HealthServer(service, pumps=pumps, host=host, port=port)


def _close_quietly(resource):
    try:
        resource.close()
    except Exception:
        pass


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
