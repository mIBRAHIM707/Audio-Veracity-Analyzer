"""Browser-test server: the real analysis API serving the built dashboard (backlog 2.3).

Claude-authored test support (14 November 2026). The API, decoding, conditioning,
windowing, aggregation and job path are the production code; only the detector is a
deterministic fake, so Playwright can drive known verdicts without model weights.
Infrastructure evidence only: it says nothing about detection accuracy.

Fake detector: logits [fake, real] = [40 * (zero-crossing rate - 0.1), 0], so a 300 Hz
tone scores genuine and a 3000 Hz tone scores synthetic after 16 kHz conditioning. A
window whose zero-crossing rate is between 0.11 and 0.14 (a 1000 Hz tone) raises, to
exercise the detector-failure path.

Run from the repository root (Playwright's spec starts and stops it):
uv run --no-project --python 3.11 --with-requirements requirements-api.txt \
    python -m tests.browser.serve_dashboard_api --port 8765 --dashboard dashboard/dist

Live board (backlog 7.1, Claude-authored 16 November 2026): `--control-port N` gives the
app a real `EventHub` (so `/v1/calls` and `WS /v1/events` work through the production
registry and WebSocket code) and starts a second, loopback-only test control server on
port N: `POST /publish` with `{"type", "call_id", "data", "ts"?}` publishes one FR-A4
event through the hub's own validation (400 with the error when it is refused) and
`POST /loss` with `{"missing", "interruptions"}` reports relay loss (`stream.gap`). The
control server is test support only; nothing in `services/` exposes it.
"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

import numpy as np
import uvicorn

from services.api.app import create_app
from services.events.contract import EventContractError
from services.events.fanout import EventHub
from services.inference.analysis import AnalysisService

MAX_CONTROL_BODY = 64 * 1024


def zero_crossing_rate(samples):
    signs = np.signbit(samples)
    return float(np.count_nonzero(signs[1:] != signs[:-1])) / max(len(samples) - 1, 1)


def fake_detector(samples):
    rate = zero_crossing_rate(np.asarray(samples, dtype=np.float32))
    if 0.11 <= rate <= 0.14:
        raise RuntimeError('fake detector fault (1000 Hz fixture)')
    return [40.0 * (rate - 0.1), 0.0]


def control_server(hub, port):
    """Loopback test control server publishing into `hub` (see the module docstring)."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):  # quiet
            pass

        def _reply(self, status, body):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):  # noqa: N802 (http.server naming)
            length = int(self.headers.get('Content-Length') or 0)
            if not 0 < length <= MAX_CONTROL_BODY:
                return self._reply(400, {'error': 'body required, at most 64 KiB'})
            try:
                body = json.loads(self.rfile.read(length))
                if self.path == '/publish':
                    envelope = hub.publish(body['type'], body.get('call_id'), body.get('data', {}), ts=body.get('ts'))
                    return self._reply(200, {'seq': envelope.seq, 'ts': envelope.ts})
                if self.path == '/loss':
                    hub.report_upstream_loss(missing=body.get('missing', 0), interruptions=body.get('interruptions', 0))
                    return self._reply(200, {})
                return self._reply(404, {'error': 'unknown control path'})
            except (EventContractError, ValueError, KeyError, TypeError) as error:
                return self._reply(400, {'error': f'{type(error).__name__}: {error}'})

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    threading.Thread(target=server.serve_forever, name='live-control', daemon=True).start()
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--dashboard', required=True)
    parser.add_argument('--model-root', help='real pinned AntiDeepfake checkpoint instead of the fake '
                        '(needs requirements-model.txt; backlog 2.QA)')
    parser.add_argument('--threads', type=int, default=4)
    parser.add_argument('--control-port', type=int,
                        help='serve live calls from an EventHub fed by a loopback test control server on this port')
    args = parser.parse_args()
    if args.model_root:
        from services.inference.baseline import baseline_factory, model_id
        service = AnalysisService(baseline_factory(args.model_root, threads=args.threads), model_id=model_id())
    else:
        service = AnalysisService(lambda: fake_detector, model_id='browser-fixture-fake-detector')
    hub = None
    if args.control_port:
        hub = EventHub()
        control_server(hub, args.control_port)
    app = create_app(service, hub=hub, dashboard_dir=args.dashboard)
    uvicorn.run(app, host='127.0.0.1', port=args.port, log_level='warning')


if __name__ == '__main__':
    main()
