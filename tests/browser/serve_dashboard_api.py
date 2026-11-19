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

History (backlog 7.3, Claude-authored 19 November 2026): `--history-dsn-env NAME` (needs
`--control-port` and requirements-store.txt) reads a disposable PostgreSQL DSN from
environment variable NAME, creates a fresh schema `ava_browser_<random>`, applies the
real migrations and serves `/v1/history/calls`, `/v1/calls/{id}/history` and
`/v1/alerts` through the production `PostgresCallHistory`. `POST /store` with
`{"type", "call_id", "data", "seq", "ts"}` writes one FR-A4 envelope through the
production `PostgresEventStore` (answer: its outcome), and `POST /drop-schema` drops the
schema (the spec calls it before stopping the server; a killed server leaves it).

Accounts (backlog 7.3 / PRD FR-U7, Claude-authored 19 November 2026): `--auth` (needs
`--history-dsn-env` and requirements-auth.txt) turns on the production accounts in the
same fresh schema, with two fictional users, `ana` (analyst) and `boss` (admin), both
with the password `AUTH_TEST_PASSWORD`. Hashing uses cheap Argon2 parameters here only
(t=1, 8 MiB, p=1) so the browser cases stay fast; the production parameters are tested
in tests/unit/test_local_auth.py. `POST /auth-disable` with `{"username", "disabled"}`
disables or enables a user through the production store (revoking its sessions),
`POST /age-sessions` with `{"seconds"}` moves every session's last use that far into
the past (to reach the idle limit without waiting), and `POST /audit` returns the
newest audit rows. With `--auth` the production `AlertFeedbackStore` serves FR-A7
alert feedback on the account store's connection (Claude-authored, 19 November 2026,
attempt 9), and `POST /feedback-rows` with `{"alert_id"}` returns that alert's stored
feedback rows with their window copies (test support: the API never returns the copy).

Snippets (backlog 7.3 / PRD FR-U5, Claude-authored 19 November 2026, attempt 10):
`--snippets` (needs `--auth` and requirements-retention.txt) makes a fresh folder under
the system temp directory and a random in-memory AES-256-GCM key, and serves
`GET /v1/alerts/{id}/snippet` through the production `SnippetReader` (TTL
`SNIPPET_TEST_TTL_S`). `POST /snippet-put` with `{"alert_id", "seconds", "hz",
"age_s"?}` writes a synthetic tone through the production `SnippetStore` as if created
`age_s` seconds ago (answer: whether it was stored); `POST /snippet-tamper` with
`{"alert_id"}` flips one byte of that snippet's ciphertext. The folder is deleted at
`/drop-schema` and at exit. No real audio is involved.
"""
import argparse
import atexit
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time

import numpy as np
import uvicorn

from services.api.app import create_app
from services.events.contract import EventContractError
from services.events.fanout import EventHub
from services.inference.analysis import AnalysisService

MAX_CONTROL_BODY = 64 * 1024
AUTH_TEST_PASSWORD = 'browser-test-password-2026'  # fictional, test only
AUTH_TEST_USERS = (('ana', 'analyst'), ('boss', 'admin'))
SNIPPET_TEST_TTL_S = 3600.0


class SnippetFixture:
    """A temp retention folder with the production AES-256-GCM store and reader."""

    def __init__(self):
        from services.retention.playback import SnippetReader
        from services.retention.snippets import AesGcmSealer
        self.root = Path(tempfile.mkdtemp(prefix='ava-browser-snippets-'))
        self._key = os.urandom(32)
        self.reader = SnippetReader(self.root, AesGcmSealer(self._key), ttl_s=SNIPPET_TEST_TTL_S, clock=time.time)
        atexit.register(self.drop)

    def put(self, body):
        from services.retention.snippets import AesGcmSealer, SnippetStore
        seconds, hz, age_s = float(body['seconds']), float(body['hz']), float(body.get('age_s', 0))
        if not 0 < seconds <= 30 or not 50 <= hz <= 3500 or not 0 <= age_s <= 30 * 86400:
            raise ValueError('seconds 0-30, hz 50-3500, age_s 0-30 days')
        t = np.arange(int(seconds * 8000)) / 8000
        pcm = (np.sin(2 * np.pi * hz * t) * 8000).astype(np.int16)
        store = SnippetStore(self.root, AesGcmSealer(self._key), clock=lambda: time.time() - age_s)
        return store.put(str(body['alert_id']), call_id='browser-test', direction='caller', pcm=pcm, sample_rate=8000)

    def tamper(self, alert_id):
        path = self.root / (hashlib.sha256(str(alert_id).encode('utf-8')).hexdigest() + '.snip')
        data = bytearray(path.read_bytes())
        data[-1] ^= 0x01
        path.write_bytes(bytes(data))

    def drop(self):
        shutil.rmtree(self.root, ignore_errors=True)


def auth_fixture(fixture):
    """The production Authenticator on the fixture's schema with the two test users."""
    from services.auth.passwords import Argon2Passwords
    from services.auth.service import Authenticator
    from services.auth.store import PostgresAuthStore
    passwords = Argon2Passwords(time_cost=1, memory_kib=8192, parallelism=1)
    store = PostgresAuthStore(fixture.connect)
    for username, role in AUTH_TEST_USERS:
        store.add_user(username, passwords.hash(AUTH_TEST_PASSWORD), role, actor='system:browser-test')
    return Authenticator(store, passwords)


def zero_crossing_rate(samples):
    signs = np.signbit(samples)
    return float(np.count_nonzero(signs[1:] != signs[:-1])) / max(len(samples) - 1, 1)


def fake_detector(samples):
    rate = zero_crossing_rate(np.asarray(samples, dtype=np.float32))
    if 0.11 <= rate <= 0.14:
        raise RuntimeError('fake detector fault (1000 Hz fixture)')
    return [40.0 * (rate - 0.1), 0.0]


class HistoryFixture:
    """A fresh schema in a disposable PostgreSQL database, written by the production event
    store and read by the production history reader (see the module docstring)."""

    def __init__(self, dsn):
        import uuid

        import psycopg

        from services.store import migrate
        from services.store.events_pg import PostgresEventStore
        from services.store.history import PostgresCallHistory

        self._psycopg, self._dsn = psycopg, dsn
        self.schema = f'ava_browser_{uuid.uuid4().hex[:12]}'
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(f'CREATE SCHEMA {self.schema}')
        self._connections = []
        self._lock = threading.Lock()
        self.history = None
        try:
            migrate.apply_migrations(self.connect())
        except BaseException:
            self.drop()  # never leave a half-made schema behind (review L7)
            raise
        self.store = PostgresEventStore(self.connect)
        self.history = PostgresCallHistory(self.connect)

    def connect(self):
        connection = self._psycopg.connect(self._dsn, options=f'-c search_path={self.schema}')
        self._connections.append(connection)
        return connection

    def record(self, body):
        from services.events.contract import make_envelope

        envelope = make_envelope(body['type'], body.get('call_id'), body.get('data', {}), seq=body['seq'], ts=body['ts'])
        with self._lock:  # one writer, as the live process
            return self.store.record(envelope)

    def drop(self):
        if self.history is not None:
            self.history.close()
        for connection in self._connections:
            try:
                connection.close()
            except Exception:
                pass
        with self._psycopg.connect(self._dsn, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA IF EXISTS {self.schema} CASCADE')


def control_server(hub, port, fixture=None, auth=None, snippets=None):
    """Loopback test control server publishing into `hub` and, with a history `fixture`,
    writing to its store (see the module docstring)."""

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
                if self.path == '/store' and fixture is not None:
                    return self._reply(200, {'outcome': fixture.record(body)})
                if self.path == '/auth-disable' and auth is not None:
                    revoked = auth.store.set_disabled(body['username'], bool(body['disabled']),
                                                      actor='system:browser-test')
                    return self._reply(200, {'sessions_revoked': revoked})
                if self.path == '/age-sessions' and auth is not None:
                    seconds = int(body['seconds'])
                    if not 0 < seconds <= 86400:
                        raise ValueError('seconds must be from 1 to 86400')
                    with fixture.connect() as connection:  # as if every session was last used `seconds` earlier
                        aged = connection.execute(
                            'UPDATE auth_sessions SET created_at = created_at - make_interval(secs => %s), '
                            'last_seen_at = last_seen_at - make_interval(secs => %s)', (seconds, seconds)).rowcount
                    return self._reply(200, {'aged': aged})
                if self.path == '/feedback-rows' and auth is not None:
                    with fixture.connect() as connection:
                        rows = connection.execute(
                            'SELECT id, label, username, alert_status, windows, windows_total FROM alert_feedback '
                            'WHERE alert_id = %s ORDER BY id', (str(body['alert_id']),)).fetchall()
                    return self._reply(200, {'rows': [
                        {'id': i, 'label': label, 'username': name, 'alert_status': status, 'windows': windows,
                         'windows_total': total} for i, label, name, status, windows, total in rows]})
                if self.path == '/audit' and auth is not None:
                    return self._reply(200, {'rows': auth.store.recent_audit(int(body.get('limit', 20)))})
                if self.path == '/snippet-put' and snippets is not None:
                    return self._reply(200, {'stored': snippets.put(body)})
                if self.path == '/snippet-tamper' and snippets is not None:
                    snippets.tamper(body['alert_id'])
                    return self._reply(200, {})
                if self.path == '/drop-schema' and fixture is not None:
                    if auth is not None:
                        auth.store.close()
                    if snippets is not None:
                        snippets.drop()
                    fixture.drop()
                    return self._reply(200, {'dropped': fixture.schema})
                return self._reply(404, {'error': 'unknown control path'})
            except (EventContractError, ValueError, KeyError, TypeError) as error:
                return self._reply(400, {'error': f'{type(error).__name__}: {error}'})
            except Exception as error:  # e.g. a database error: the spec sees its class (review L7)
                return self._reply(500, {'error': f'{type(error).__name__}: {error}'})

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
    parser.add_argument('--history-dsn-env', metavar='NAME',
                        help='serve stored history from a fresh schema in the PostgreSQL database named by this '
                             'environment variable (needs --control-port and requirements-store.txt)')
    parser.add_argument('--auth', action='store_true',
                        help='production accounts with two fictional test users (needs --history-dsn-env)')
    parser.add_argument('--ws-session-recheck-s', type=float,
                        help='test only: how often an open WebSocket re-checks its session (production: 60 s)')
    parser.add_argument('--snippets', action='store_true',
                        help='FR-U5 snippet playback from a temp folder with a random key (needs --auth and '
                             'requirements-retention.txt)')
    args = parser.parse_args()
    if args.snippets and not args.auth:
        parser.error('--snippets needs --auth')
    if args.history_dsn_env and not args.control_port:
        parser.error('--history-dsn-env needs --control-port')
    if args.auth and not args.history_dsn_env:
        parser.error('--auth needs --history-dsn-env')
    if args.model_root:
        from services.inference.baseline import baseline_factory, model_id
        service = AnalysisService(baseline_factory(args.model_root, threads=args.threads), model_id=model_id())
    else:
        service = AnalysisService(lambda: fake_detector, model_id='browser-fixture-fake-detector')
    hub = None
    fixture = None
    if args.history_dsn_env:
        dsn = os.environ.get(args.history_dsn_env)
        if not dsn:
            parser.error(f'environment variable {args.history_dsn_env} is not set')
        fixture = HistoryFixture(dsn)
    auth = auth_fixture(fixture) if args.auth else None
    if args.ws_session_recheck_s is not None:
        from services.api import app as app_module
        app_module.SESSION_RECHECK_S = args.ws_session_recheck_s  # read at each check; test support only
    snippets = SnippetFixture() if args.snippets else None
    if args.control_port:
        hub = EventHub()
        control_server(hub, args.control_port, fixture, auth, snippets)
    feedback = None
    if auth is not None:  # FR-A7 alert feedback, as services/api/__main__.py wires it
        from services.store.feedback import AlertFeedbackStore
        feedback = AlertFeedbackStore(auth.store)
    app = create_app(service, hub=hub, dashboard_dir=args.dashboard,
                     history=None if fixture is None else fixture.history, auth=auth, feedback=feedback,
                     snippets=None if snippets is None else snippets.reader)
    uvicorn.run(app, host='127.0.0.1', port=args.port, log_level='warning')


if __name__ == '__main__':
    main()
