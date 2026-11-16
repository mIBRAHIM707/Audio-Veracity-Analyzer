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
    return {'service': _open_service, 'redis': _open_redis, 'bind': _bind, 'serve': _serve}


def _say(stream, record):
    stream.write(json.dumps(record, sort_keys=True) + '\n')
    stream.flush()


def main(argv=None, *, environ=None, stderr=None, made=None):
    environ = os.environ if environ is None else environ
    stderr = sys.stderr if stderr is None else stderr
    made = {**default_parts(), **(made or {})}
    args = build_parser().parse_args(argv)
    held = {'sock': None, 'redis': None}
    try:
        return _run(args, environ, stderr, made, held)
    except KeyboardInterrupt:  # Ctrl+C before serving (e.g. during the Redis ping)
        _say(stderr, {'event': 'interrupted'})
        return EXIT_INTERRUPTED
    finally:
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

    if not ipaddress.IPv4Address(args.host).is_loopback and not args.allow_non_loopback:
        return refuse('the API has no authentication: listen on a loopback address or pass --allow-non-loopback')
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
    try:
        app = create_app(service, hub=hub, relay=relay, dashboard_dir=args.dashboard)
    except ValueError as error:
        return refuse(str(error))
    record = {'event': 'starting', 'host': args.host, 'port': port, 'pid': os.getpid(),
              'model_id': service.model_id, 'threads': args.threads, 'dashboard': args.dashboard is not None,
              'allow_non_loopback': args.allow_non_loopback, 'live_events': relay is not None}
    if relay is not None:
        record['redis_reachable'] = redis_reachable
    _say(stderr, record)
    try:
        stop_signal = made['serve'](app, held['sock'])
    except ServeFailed as error:
        _say(stderr, {'event': 'failed', 'reason': str(error)})
        return EXIT_FAILED
    stopped = {'event': 'stopped', 'signal': stop_signal}
    if relay is not None:
        stopped['relay'] = dict(relay.stats)
    _say(stderr, stopped)
    return EXIT_OK


if __name__ == '__main__':
    sys.exit(main())
