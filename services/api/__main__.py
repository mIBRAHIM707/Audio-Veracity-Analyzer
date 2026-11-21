"""Command-line entry point of the analysis API (backlog 5.2; PRD FR-A1/A3/A4/A8;
architecture §2.5 "WebSocket /v1/events fans out Redis pub/sub events").

    python -m services.api --model-root D:/AVA-Data/models/antideepfake --port 8000 \
        [--dashboard dashboard/dist] [--redis-url-env AVA_REDIS_URL]

Serves `services/api/app.py` with uvicorn on `--host` (default 127.0.0.1). The API has
no authentication, so any other address is refused unless `--allow-non-loopback` is
given. The detector is the pinned AntiDeepfake baseline (`requirements-model.txt`),
verified and loaded on first use (`/readyz` runs the warm-up); start-up checks that torch
is installed and that the model directory holds `model.safetensors`. The listening
socket is bound before anything is announced, so a port in use refuses the start;
`--port 0` takes any free port, reported in the `starting` record.

`--redis-url-env NAME` names an environment variable holding the Redis URL the live
service publishes on (`python -m services.live --redis-url-env`); the URL is never on
the command line and never printed. The API then serves `/v1/calls` and
`WS /v1/events` from an in-process hub fed by a `RedisRelay` thread
(`services/events/relay_loop.py`): Redis down at start-up is reported
(`redis_reachable: false`) and retried with backoff, not refused (pub/sub is
at-most-once; the loss reaches WebSocket clients as `stream.gap`). Without it the calls
and events routes answer 503 / close 1011 (no live source).

`--auth on` (FR-U7, 19 Nov) needs `--database-url-env`: accounts, sessions and the
audit log are in that database (migration 0003, applied by `python -m services.auth
migrate` or the live process; users are created with `python -m services.auth
add-user`). Every /v1 route then needs a login, so a non-loopback `--host` is allowed
without `--allow-non-loopback`. The session cookie is Secure unless `--cookie-insecure`
(plain HTTP on a lab address; browsers accept Secure cookies from http://localhost).

Records (JSON lines on stderr): `starting` (with the port and pid) before serving;
`stopped` (with the stop signal and the relay counters) after a graceful shutdown on
SIGINT, SIGTERM or (Windows) SIGBREAK, exit 0; `refused`, exit 2; `interrupted` for
Ctrl+C during start-up, exit 130; `failed`, exit 1, when the server ended without
starting (for example the app's start-up failed). Open connections and running analyses get 10 s at
stop; a second Ctrl+C forces uvicorn's exit.
"""
import argparse
import importlib.util
import ipaddress
import json
import os
import re
import signal
import socket
import sys
import threading
from pathlib import Path

EXIT_OK, EXIT_FAILED, EXIT_REFUSED, EXIT_INTERRUPTED = 0, 1, 2, 130
GRACEFUL_SHUTDOWN_S = 10  # open connections and running analyses get this long at stop
STOP_SIGNALS = ('SIGINT', 'SIGTERM', 'SIGBREAK')


class ServeFailed(RuntimeError):
    """The server ended without having started (no stop signal)."""


def _port(text):
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f'not an integer: {text!r}') from None
    if not 0 <= value <= 65535:
        raise argparse.ArgumentTypeError('must be from 0 (any free port) to 65535')
    return value


def _threads(text):
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f'not an integer: {text!r}') from None
    if not 1 <= value <= 64:
        raise argparse.ArgumentTypeError('must be from 1 to 64')
    return value


def _host(text):
    try:
        return str(ipaddress.IPv4Address(text))
    except ValueError:
        raise argparse.ArgumentTypeError('must be an IPv4 address (127.0.0.1 by default)') from None


def _env_name(text):
    if not re.match(r'^[A-Za-z_][A-Za-z0-9_]{0,63}$', text):
        raise argparse.ArgumentTypeError('must be an environment variable name')
    return text


def _ttl_days(text):
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f'not a number: {text!r}') from None
    if not 0 < value <= 365:  # also refuses NaN; infinity is above 365
        raise argparse.ArgumentTypeError('must be more than 0 and at most 365 days')
    return value


def build_parser():
    parser = argparse.ArgumentParser(prog='python -m services.api', description='AVA local analysis API')
    parser.add_argument('--model-root', required=True,
                        help='pinned AntiDeepfake checkpoint directory (needs requirements-model.txt)')
    parser.add_argument('--threads', type=_threads, default=4, help='torch CPU threads for the detector')
    parser.add_argument('--host', type=_host, default='127.0.0.1', help='IPv4 address to listen on')
    parser.add_argument('--port', type=_port, required=True)
    parser.add_argument('--allow-non-loopback', action='store_true',
                        help='allow a non-loopback --host (the API has no authentication)')
    parser.add_argument('--dashboard', help='built dashboard directory (dashboard/dist) served at /')
    parser.add_argument('--redis-url-env', type=_env_name,
                        help='environment variable with the Redis URL the live service publishes on')
    parser.add_argument('--event-log', choices=('stderr', 'off'), default='stderr',
                        help='structured JSON operations log (services/api/oplog.py) on stderr (default) or off')
    parser.add_argument('--database-url-env', type=_env_name,
                        help='environment variable with the PostgreSQL URL the live service stores calls in; '
                             'serves the read-only call history routes (needs requirements-store.txt)')
    parser.add_argument('--auth', choices=('off', 'on'), default='off',
                        help='FR-U7 local accounts in the --database-url-env database (needs requirements-auth.txt '
                             'and migration 0003: python -m services.auth migrate); every /v1 route then needs a login')
    parser.add_argument('--cookie-insecure', action='store_true',
                        help='session cookie without the Secure flag, for plain HTTP on a non-loopback lab address')
    parser.add_argument('--retention-dir',
                        help='FR-U5 playback: the live service\'s snippet folder (read only; needs --auth on, '
                             '--retention-key-file and requirements-retention.txt)')
    parser.add_argument('--retention-key-file', help='the live service\'s raw 32-byte AES-256-GCM key file')
    parser.add_argument('--retention-ttl-days', type=_ttl_days,
                        help='snippets older than this are not played (use the live service\'s value; default 7; '
                             'only with --retention-dir)')
    return parser


def _open_service(args):
    if importlib.util.find_spec('torch') is None:  # the baseline loads lazily: check now
        raise ImportError('torch is not installed', name='torch')
    from services.inference.analysis import AnalysisService
    from services.inference.baseline import baseline_factory, model_id
    return AnalysisService(baseline_factory(args.model_root, threads=args.threads), model_id=model_id())


def _open_redis(url):
    """Return (client, reachable). Raises ImportError, or ValueError for a bad URL."""
    from services.events.relay_loop import open_relay_client
    client = open_relay_client(url)
    try:
        client.ping()
        reachable = True
    except Exception:  # unreachable now; the relay thread keeps retrying
        reachable = False
    return client, reachable


DB_CONNECT_TIMEOUT_S = 5
# As the live process (services/live/__main__.py): a database that stops answering must
# not hold a request thread for the OS TCP timeout.
DB_KEEPALIVES = {'keepalives': 1, 'keepalives_idle': 5, 'keepalives_interval': 2, 'keepalives_count': 3,
                 'tcp_user_timeout': 10_000}
DB_TIMEOUTS_MS = {'statement_timeout': 10_000, 'lock_timeout': 10_000}


def _connect_factory(dsn):
    """A psycopg connection factory with the timeouts above. Raises ImportError, or
    ValueError for a URL that does not parse."""
    import psycopg
    from psycopg.conninfo import conninfo_to_dict
    try:
        given = conninfo_to_dict(dsn)
    except psycopg.ProgrammingError as error:
        raise ValueError('database URL is not valid') from error
    defaults = {key: value for key, value in {'connect_timeout': DB_CONNECT_TIMEOUT_S, **DB_KEEPALIVES}.items()
                if key not in given}

    def connect():
        connection = psycopg.connect(dsn, **defaults)
        try:
            for name, value in DB_TIMEOUTS_MS.items():
                connection.execute(f'SET {name} = {int(value)}')  # names from the constant above only
            connection.commit()  # idle: the reader/store opens its own transaction
            return connection
        except BaseException:
            connection.close()
            raise
    return connect


def _open_history(dsn):
    """Return (PostgresCallHistory, reachable). Raises ImportError, or ValueError for a
    URL that does not parse. One probe read at start-up; a database that is down or not
    migrated is reported, not refused (the history routes answer 503 until it is)."""
    from services.store.history import HistoryError, PostgresCallHistory
    history = PostgresCallHistory(_connect_factory(dsn))
    try:
        history.recent(limit=1)
        return history, True
    except HistoryError as error:
        return history, error.reason


def _open_auth(dsn):
    """Return (Authenticator, reachable) for FR-U7 accounts on their own read-write
    connection. Raises ImportError (argon2-cffi or psycopg missing) or ValueError. One
    probe at start-up; a database that is down or not migrated (0003) is reported, not
    refused: login and every /v1 route answer 503 until it is (never open)."""
    from services.auth.passwords import Argon2Passwords
    from services.auth.service import Authenticator
    from services.auth.store import AuthStoreError, PostgresAuthStore
    store = PostgresAuthStore(_connect_factory(dsn))
    authenticator = Authenticator(store, Argon2Passwords())
    authenticator.close = store.close  # held and closed like the other resources
    # FR-A7 alert feedback shares the account store's connection: each label is written
    # in one transaction with its audit row (needs migration 0004; 503 until then).
    from services.store.feedback import AlertFeedbackStore
    authenticator.feedback = AlertFeedbackStore(store)
    try:
        store.resolve('A' * 43)  # a well-formed token that matches nothing: one real query
        return authenticator, True
    except AuthStoreError as error:
        return authenticator, error.reason


def _open_snippets(directory, key_file, ttl_days):
    """FR-U5 `SnippetReader` over the live service's folder and key. Raises RuntimeError
    (cryptography missing), ValueError or OSError; callers print neither path nor key."""
    import time
    from services.retention.playback import SnippetReader
    from services.retention.snippets import AesGcmSealer
    return SnippetReader(directory, AesGcmSealer.from_key_file(key_file), ttl_s=ttl_days * 86400.0,
                         clock=time.time)


def _bind(host, port):
    """The listening socket, bound before anything is announced (a busy port refuses)."""
    return socket.create_server((host, port))


def _serve(app, sock):
    """Run uvicorn on `sock` until a stop signal; returns the signal's name or None.
    uvicorn shuts down gracefully on SIGINT/SIGTERM (and SIGBREAK on Windows), restores
    the previous handlers and then raises the signal again; the flag-only handlers
    installed here are those previous handlers, so the process is not killed before the
    `stopped` record (a second Ctrl+C still forces uvicorn's exit)."""
    import uvicorn
    received = []
    names = [name for name in STOP_SIGNALS if hasattr(signal, name)]
    previous = {}
    if threading.current_thread() is threading.main_thread():
        for name in names:
            previous[name] = signal.signal(getattr(signal, name), lambda number, frame: received.append(number))
    try:
        config = uvicorn.Config(app, log_level='warning', timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_S)
        server = uvicorn.Server(config)
        if not received:  # a stop that arrived while the handlers went in: do not serve
            server.run(sockets=[sock])
    finally:
        for name, handler in previous.items():
            signal.signal(getattr(signal, name), handler)
    if not received and not server.started:  # e.g. the app's start-up (lifespan) failed
        raise ServeFailed('the server did not start')
    return signal.Signals(received[0]).name if received else None


def default_parts():
    return {'service': _open_service, 'redis': _open_redis, 'history': _open_history, 'auth': _open_auth,
            'snippets': _open_snippets, 'bind': _bind, 'serve': _serve}


def _say(stream, record):
    stream.write(json.dumps(record, sort_keys=True) + '\n')
    stream.flush()


def main(argv=None, *, environ=None, stderr=None, made=None):
    environ = os.environ if environ is None else environ
    stderr = sys.stderr if stderr is None else stderr
    made = {**default_parts(), **(made or {})}
    args = build_parser().parse_args(argv)
    held = {'sock': None, 'redis': None, 'history': None, 'auth': None}
    try:
        return _run(args, environ, stderr, made, held)
    except KeyboardInterrupt:  # Ctrl+C before serving (e.g. during the Redis ping)
        _say(stderr, {'event': 'interrupted'})
        return EXIT_INTERRUPTED
    finally:
        _stop_log(held)  # e.g. Ctrl+C or an unexpected error while serving
        for resource in held.values():
            if resource is not None:
                try:
                    resource.close()
                except Exception:  # closing a broken client may fail too; nothing to keep
                    pass


def _run(args, environ, stderr, made, held):
    def refuse(reason):
        _say(stderr, {'event': 'refused', 'reason': reason})
        return EXIT_REFUSED

    if args.auth == 'on' and not args.database_url_env:
        return refuse('--auth on needs --database-url-env: accounts live in PostgreSQL')
    if args.cookie_insecure and args.auth != 'on':
        return refuse('--cookie-insecure only applies with --auth on')
    if (args.retention_dir is None) != (args.retention_key_file is None):
        return refuse('snippet playback needs both --retention-dir and --retention-key-file')
    if args.retention_dir is not None and args.auth != 'on':
        return refuse('snippet playback needs --auth on: every playback is audited with its listener')
    if args.retention_dir is None and args.retention_ttl_days is not None:
        return refuse('--retention-ttl-days only applies with --retention-dir')
    ttl_days = 7.0 if args.retention_ttl_days is None else args.retention_ttl_days
    if not ipaddress.IPv4Address(args.host).is_loopback and not args.allow_non_loopback and args.auth != 'on':
        return refuse('the API has no authentication: listen on a loopback address, pass --auth on, '
                      'or pass --allow-non-loopback')
    if not (Path(args.model_root) / 'model.safetensors').is_file():
        return refuse('model root must be a directory holding model.safetensors')
    if args.dashboard is not None and not (Path(args.dashboard) / 'index.html').is_file():
        return refuse('dashboard must be a built dashboard directory containing index.html')
    try:
        held['sock'] = made['bind'](args.host, args.port)
    except OSError as error:
        return refuse(f'cannot listen on {args.host}:{args.port}: {error.strerror or type(error).__name__}')
    port = held['sock'].getsockname()[1]
    redis_client = redis_reachable = None
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
        held['redis'] = redis_client
    history = history_reachable = None
    if args.database_url_env:
        dsn = environ.get(args.database_url_env)
        if not dsn:
            return refuse(f'database URL not set: put it in the {args.database_url_env} environment variable')
        try:
            history, history_reachable = made['history'](dsn)
        except ImportError as error:
            return refuse(f'missing dependency: {error.name or error} (requirements-store.txt)')
        except ValueError:  # the URL itself is never printed
            return refuse('database URL is not valid')
        except Exception as error:  # any other driver error: the type name only
            return refuse(f'database client: {type(error).__name__}')
        held['history'] = history
    auth = auth_reachable = None
    if args.auth == 'on':
        try:
            auth, auth_reachable = made['auth'](dsn)
        except ImportError as error:
            return refuse(f'missing dependency: {error.name or error} (requirements-auth.txt)')
        except ValueError:
            return refuse('database URL is not valid')
        except Exception as error:
            return refuse(f'account store: {type(error).__name__}')
        held['auth'] = auth
    snippets = None
    if args.retention_dir is not None:
        try:
            snippets = made['snippets'](args.retention_dir, args.retention_key_file, ttl_days)
        except RuntimeError:  # the cryptography package is missing
            return refuse('snippet playback: missing dependency cryptography (requirements-retention.txt)')
        except ValueError as error:  # the messages name the rule, never the path or key
            return refuse(f'snippet playback: {error}')
        except OSError as error:
            return refuse(f'snippet playback: {type(error).__name__}: {error.strerror}')
    try:
        service = made['service'](args)
    except ImportError as error:
        return refuse(f'missing dependency: {error.name or error} (requirements-model.txt)')
    from services.api.app import create_app
    hub = relay = None
    if redis_client is not None:
        from services.events.fanout import EventHub
        from services.events.redis_bridge import RedisRelay
        hub = EventHub()
        relay = RedisRelay(lambda: redis_client, hub)
    from services.api.oplog import ApiLog
    oplog = ApiLog(stderr) if args.event_log == 'stderr' else None
    try:
        app = create_app(service, hub=hub, relay=relay, dashboard_dir=args.dashboard, oplog=oplog,
                         history=history, auth=auth, cookie_secure=not args.cookie_insecure,
                         feedback=getattr(auth, 'feedback', None), snippets=snippets)
    except ValueError as error:
        return refuse(str(error))
    record = {'event': 'starting', 'host': args.host, 'port': port, 'pid': os.getpid(),
              'model_id': service.model_id, 'threads': args.threads, 'dashboard': args.dashboard is not None,
              'allow_non_loopback': args.allow_non_loopback, 'live_events': relay is not None,
              'event_log': args.event_log}
    if relay is not None:
        record['redis_reachable'] = redis_reachable
    record['call_history'] = history is not None
    if history is not None:  # True, or why the probe read failed: unavailable, schema or error
        record['history_reachable'] = history_reachable
    record['auth'] = auth is not None
    if auth is not None:  # True, or why the probe failed: unavailable, schema or error
        record['auth_reachable'] = auth_reachable
        record['cookie_secure'] = not args.cookie_insecure
    if snippets is not None:  # never the folder or key path
        # The live service creates the folder at its first snippet, so a missing one is
        # reported, not refused; a mistyped one shows here (review L3).
        record['snippet_playback'] = {'ttl_days': ttl_days,
                                      'folder_exists': Path(args.retention_dir).is_dir()}
    _say(stderr, record)
    if oplog is not None:  # after the starting record, so that record stays the first line
        oplog.start()
        held['oplog'] = oplog  # stopped (pending lines written) on every exit path
    try:
        stop_signal = made['serve'](app, held['sock'])
    except ServeFailed as error:
        _stop_log(held)
        _say(stderr, {'event': 'failed', 'reason': str(error)})
        return EXIT_FAILED
    # Pending lines are written before the stopped record, which stays last unless stderr
    # blocked the writer past its timeout (then `stopped_in_time` is false; review L1).
    in_time = _stop_log(held)
    stopped = {'event': 'stopped', 'signal': stop_signal}
    if relay is not None:
        stopped['relay'] = dict(relay.stats)
    if oplog is not None:
        stopped['event_log'] = {**oplog.report(), 'stopped_in_time': in_time}
    _say(stderr, stopped)
    return EXIT_OK


def _stop_log(held):
    oplog = held.pop('oplog', None)
    return None if oplog is None else oplog.stop()


if __name__ == '__main__':
    sys.exit(main())
