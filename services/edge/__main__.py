"""Run the lab demo's TCP forwarder (services/edge/forward.py; backlog 11.2).

    python -m services.edge --listen-host 0.0.0.0 --listen-port 8080 \
        --target-host api --target-port 8000

The Compose `demo` profile runs this in its own container and publishes the listen port
on the host's loopback address only (deploy/compose/README.md, "Demo profile"). One JSON
`listening` record on stdout once the socket is bound, and one `stopped` record with
the counts after SIGTERM/SIGINT. Nothing is logged per connection: no addresses, no
bytes. A refused start (bad arguments, port in use) exits 2 with one JSON record on
stderr.
"""
import argparse
import asyncio
import json
import signal
import sys

from services.edge.forward import Forwarder

EXIT_OK, EXIT_REFUSED = 0, 2


def build_parser():
    parser = argparse.ArgumentParser(prog='python -m services.edge', description='AVA lab demo TCP forwarder')
    parser.add_argument('--listen-host', default='127.0.0.1')
    parser.add_argument('--listen-port', type=int, required=True)
    parser.add_argument('--target-host', required=True)
    parser.add_argument('--target-port', type=int, required=True)
    parser.add_argument('--max-connections', type=int, default=32, help='1-1024 (default 32)')
    parser.add_argument('--connect-timeout', type=float, default=5.0, help='seconds (default 5)')
    parser.add_argument('--idle-timeout', type=float, default=300.0, help='seconds (default 300)')
    return parser


def _say(stream, record):
    stream.write(json.dumps(record, sort_keys=True) + '\n')
    stream.flush()


async def serve(args, *, stdout, stop_event=None):
    forwarder = Forwarder(args.target_host, args.target_port, max_connections=args.max_connections,
                          connect_timeout=args.connect_timeout, idle_timeout=args.idle_timeout)
    port = await forwarder.start(args.listen_host, args.listen_port)
    stop_event = stop_event or asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in ('SIGTERM', 'SIGINT'):
        number = getattr(signal, name, None)
        if number is None:
            continue
        try:
            loop.add_signal_handler(number, stop_event.set)
        except (NotImplementedError, RuntimeError, ValueError):
            pass  # Windows event loops: Ctrl+C raises KeyboardInterrupt instead
    _say(stdout, {'event': 'listening', 'listen_host': args.listen_host, 'listen_port': port,
                  'target': f'{args.target_host}:{args.target_port}', 'max_connections': args.max_connections})
    try:
        await stop_event.wait()
    finally:
        await forwarder.stop()
        _say(stdout, {'event': 'stopped', **forwarder.stats.snapshot()})


def main(argv=None, *, stdout=None, stderr=None):
    stdout, stderr = stdout or sys.stdout, stderr or sys.stderr
    args = build_parser().parse_args(argv)
    if not 0 <= args.listen_port <= 65535:
        _say(stderr, {'event': 'refused', 'reason': 'listen port must be 0-65535'})
        return EXIT_REFUSED
    try:
        # Validate before binding, so a bad value is a refusal, not a traceback.
        Forwarder(args.target_host, args.target_port, max_connections=args.max_connections,
                  connect_timeout=args.connect_timeout, idle_timeout=args.idle_timeout)
    except ValueError as error:
        _say(stderr, {'event': 'refused', 'reason': str(error)})
        return EXIT_REFUSED
    try:
        asyncio.run(serve(args, stdout=stdout))
    except OSError as error:  # port in use, address not available
        _say(stderr, {'event': 'refused', 'reason': f'cannot listen: {type(error).__name__}'})
        return EXIT_REFUSED
    except KeyboardInterrupt:
        pass
    return EXIT_OK


if __name__ == '__main__':
    sys.exit(main())
