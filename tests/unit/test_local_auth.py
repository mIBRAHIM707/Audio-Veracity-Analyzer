"""Claude-authored tests (19 November 2026, attempt 7) for PRD FR-U7 local accounts and
the PRD §6.3 audit log: services/auth/ (passwords, store, service, command line),
migration 0003 and the API's login, session check, cross-origin refusal, WebSocket
check and audited CSV export (services/api/app.py, __main__.py).

Authored by Claude (Claude Code), not by GPT-6 Astra. Groups:
- pure tests (always run): audit/username/origin rules and the `Authenticator` with a
  fake store and fake hasher (equal work for unknown names, lockout, busy, failures);
- Argon2 tests (need requirements-auth.txt): parameters, rules, verify never raising;
- API tests with the fake store (need requirements-api.txt);
- PostgreSQL tests, run only when AVA_TEST_POSTGRES_DSN names a disposable database
  (AVA_REQUIRE_POSTGRES=1 makes the skip a failure): the real store, migration 0003
  constraints, the command line and the API end to end. Each test uses its own schema.
No audio and no detector are involved.
"""
import io
import json
import threading
import time
import unittest
import unittest.mock
import uuid

from services.auth import service as service_module
from services.auth.service import Authenticator
from services.auth.store import AuthStoreError, Principal, UserRecord, check_audit, valid_username
from tests.unit import test_store_history as base
from tests.unit.test_store_history import DSN, REAL

try:
    import argon2  # noqa: F401
    from services.auth.passwords import Argon2Passwords, PasswordRuleError, check_new_password
except ImportError:  # pragma: no cover - lightweight environment
    Argon2Passwords = None
ARGON2 = unittest.skipIf(Argon2Passwords is None, 'requirements-auth.txt not installed')

TestClient = base.TestClient
if TestClient is not None:
    from services.api import app as app_module
    from services.api.app import create_app, same_origin
    from services.inference import analysis
    from tests.unit import test_analysis_api as api_fixture
API = unittest.skipIf(TestClient is None, 'requirements-api.txt not installed')

PASSWORD = 'correct horse battery'


# -- fakes ----------------------------------------------------------------------------

class FakePasswords:
    """'h:<password>' is a current hash, 'old:<password>' an outdated one."""

    dummy_hash = 'h:\x00dummy'

    def __init__(self):
        self.verified, self.hashed = [], []

    def verify(self, stored, password):
        self.verified.append(stored)
        return stored in (f'h:{password}', f'old:{password}')

    def needs_rehash(self, stored):
        return stored.startswith('old:')

    def hash(self, password):
        self.hashed.append(password)
        return f'h:{password}'


class FakeStore:
    absolute_s = 3600

    def __init__(self, **users):
        self.users = {name: UserRecord(i + 1, name, value[0], value[1], value[2] if len(value) > 2 else False)
                      for i, (name, value) in enumerate(users.items())}
        self.sessions, self.audits, self.rehashed = {}, [], []
        self.fail = self.audit_fail = None
        self.refuse_open = False
        self.counter = 0

    def _check(self):
        if self.fail:
            raise AuthStoreError('down', reason=self.fail)

    def find_user(self, username):
        self._check()
        return self.users.get(username)

    def open_session(self, user, detail=None):
        self._check()
        current = self.users.get(user.username)
        # As the real store: refused if the user was disabled or its hash changed since it was read.
        if self.refuse_open or current is None or current.disabled or current.password_hash != user.password_hash:
            return None
        self.counter += 1
        token = f'{self.counter:043d}'
        principal = Principal(user.id, user.username, user.role, 1e9)
        self.sessions[token] = principal
        self.audits.append((user.username, 'auth.login', 'ok', dict(detail or {})))
        return token, principal

    def resolve(self, token):
        self._check()
        return self.sessions.get(token)

    def close_session(self, token, principal):
        self._check()
        self.audits.append((principal.username, 'auth.logout', 'ok', {}))
        return self.sessions.pop(token, None) is not None

    def audit(self, actor, action, outcome='ok', detail=None):
        if self.audit_fail:
            raise AuthStoreError('down', reason=self.audit_fail)
        check_audit(actor, action, outcome, detail or {})
        self.audits.append((actor, action, outcome, dict(detail or {})))

    def rehash(self, user_id, old_hash, new_hash):
        self._check()
        self.rehashed.append((user_id, old_hash, new_hash))
        user = next(u for u in self.users.values() if u.id == user_id)
        if user.password_hash != old_hash:
            return False
        self.users[user.username] = UserRecord(user.id, user.username, new_hash, user.role, user.disabled)
        return True


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def authenticator(store=None, **kwargs):
    store = store or FakeStore(ana=(f'h:{PASSWORD}', 'analyst'), boss=(f'h:{PASSWORD}', 'admin'),
                               gone=(f'h:{PASSWORD}', 'analyst', True), legacy=(f'old:{PASSWORD}', 'analyst'))
    clock = kwargs.pop('clock', Clock())
    passwords = FakePasswords()
    return Authenticator(store, passwords, clock=clock, **kwargs), store, passwords, clock


# -- pure -----------------------------------------------------------------------------

class RuleTests(unittest.TestCase):
    def test_usernames_are_lower_case_ascii_with_a_bounded_length(self):
        for name in ('a', 'ana', 'ana.khan', 'a_1-b', 'a' * 64):
            self.assertTrue(valid_username(name), name)
        for name in ('', 'Ana', '1ana', '.ana', 'a' * 65, 'ana ', 'آنا', 'ana\x00', None, 7, 'anonymous\n'):
            self.assertFalse(valid_username(name), repr(name))

    def test_audit_rows_are_checked_before_any_database_work(self):
        self.assertEqual(check_audit('ana', 'alerts.export', 'ok', {'rows': 3}), '{"rows":3}')
        self.assertEqual(check_audit('system:cli', 'user.add', 'ok', {}), '{}')
        self.assertEqual(check_audit('anonymous', 'auth.login', 'refused', {'u': 'ٹیسٹ'}), '{"u":"ٹیسٹ"}')
        bad = [('Ana', 'auth.login', 'ok', {}), ('system:', 'x', 'ok', {}), ('ana', 'Auth.login', 'ok', {}),
               ('ana', 'a.b.c.d', 'ok', {}), ('ana', 'auth.login', 'maybe', {}), ('ana', 'x', 'ok', []),
               ('ana', 'x', 'ok', {'v': float('nan')}), ('ana', 'x', 'ok', {'v': 'x' * 5000}),
               ('ana', 'x', 'ok', {'v': 'a\x00b'})]
        for row in bad:
            with self.assertRaises(ValueError, msg=repr(row)[:80]):
                check_audit(*row)


@API
class OriginTests(unittest.TestCase):
    def test_cross_site_browser_requests_are_refused(self):
        cases = [({}, True), ({'host': 'ava.lab:8000'}, True),
                 ({'host': 'ava.lab:8000', 'origin': 'https://ava.lab:8000'}, True),
                 ({'host': 'ava.lab:8000', 'origin': 'http://AVA.lab:8000'}, True),  # scheme ignored (TLS proxy)
                 ({'host': 'ava.lab:8000', 'origin': 'https://ava.lab'}, False),  # other port
                 ({'host': 'ava.lab:8000', 'origin': 'https://evil.example'}, False),
                 ({'host': 'ava.lab:8000', 'origin': 'null'}, False),
                 ({'host': 'ava.lab:8000', 'origin': 'garbage'}, False),
                 ({'origin': 'https://ava.lab:8000'}, False),  # no Host to compare with
                 ({'host': 'ava.lab:8000', 'sec-fetch-site': 'cross-site'}, False),
                 ({'host': 'ava.lab:8000', 'sec-fetch-site': 'same-origin'}, True)]
        for headers, expected in cases:
            self.assertIs(same_origin(headers), expected, headers)


class AuthenticatorTests(unittest.TestCase):
    def test_a_correct_password_opens_a_session_and_is_audited_with_it(self):
        auth, store, passwords, _ = authenticator()
        result = auth.login('ana', PASSWORD)
        self.assertEqual((result.outcome, result.principal.username, result.principal.role), ('ok', 'ana', 'analyst'))
        self.assertEqual(store.sessions[result.token], result.principal)
        self.assertEqual(store.audits, [('ana', 'auth.login', 'ok', {})])
        self.assertEqual(passwords.verified, [f'h:{PASSWORD}'])

    def test_unknown_disabled_and_wrong_answer_alike_and_cost_one_verify_each(self):
        auth, store, passwords, _ = authenticator()
        for username, password, reason in (('nobody', PASSWORD, 'unknown_user'), ('gone', PASSWORD, 'disabled'),
                                           ('ana', 'wrong password!', 'wrong_password')):
            passwords.verified.clear()
            result = auth.login(username, password)
            self.assertEqual((result.outcome, result.token, result.principal), ('invalid', None, None))
            self.assertEqual(len(passwords.verified), 1, f'{username}: one Argon2 verify, known or not')
            # The name is recorded for existing accounts only (review L1): an unknown
            # "name" may be a password typed into the wrong field.
            expected = {'reason': reason} if username == 'nobody' else {'reason': reason, 'username': username}
            self.assertEqual(store.audits[-1], ('anonymous', 'auth.login', 'refused', expected))
        self.assertEqual(passwords.verified, [f'h:{PASSWORD}'], 'the wrong password was checked against the real hash')
        self.assertEqual(store.sessions, {})

    def test_a_malformed_name_is_verified_against_the_dummy_and_not_echoed_to_the_audit(self):
        auth, store, passwords, _ = authenticator()
        for name in ('Robert\'); DROP TABLE users;--', 'x' * 500, ''):
            self.assertEqual(auth.login(name, PASSWORD).outcome, 'invalid')
            self.assertEqual(store.audits[-1], ('anonymous', 'auth.login', 'refused', {'reason': 'unknown_user'}))
        self.assertEqual(set(passwords.verified), {FakePasswords.dummy_hash})
        self.assertEqual(auth.login(None, PASSWORD).outcome, 'invalid')
        self.assertEqual(store.audits[-1][3], {'reason': 'malformed'})

    def test_five_failures_lock_known_and_unknown_names_alike(self):
        auth, store, passwords, clock = authenticator()
        for name in ('ana', 'nobody'):
            outcomes = []
            for _ in range(5):
                clock.now += 1
                outcomes.append(auth.login(name, 'wrong password!').outcome)
            self.assertEqual(outcomes, ['invalid'] * 4 + ['locked'], name)
            self.assertEqual(store.audits[-1][3]['reason'], 'locked_now')
            verified, audits = len(passwords.verified), len(store.audits)
            self.assertEqual(auth.login(name, PASSWORD).outcome, 'locked', 'even the right password')
            self.assertEqual((len(passwords.verified), len(store.audits)), (verified, audits),
                             'a locked name costs no verify and no audit write')
        clock.now += service_module.LOCKOUT_S + 1
        self.assertEqual(auth.login('ana', PASSWORD).outcome, 'ok', 'the lock ends')
        self.assertEqual(auth.counts['locked'], 4)

    def test_failures_outside_the_window_do_not_count_and_success_resets(self):
        auth, _, _, clock = authenticator()
        for _ in range(4):
            auth.login('ana', 'wrong password!')
        clock.now += service_module.WINDOW_S + 1
        for _ in range(4):
            self.assertEqual(auth.login('ana', 'wrong password!').outcome, 'invalid')
        self.assertEqual(auth.login('ana', PASSWORD).outcome, 'ok')
        for _ in range(4):
            self.assertEqual(auth.login('ana', 'wrong password!').outcome, 'invalid', 'reset by the success')

    def test_the_failure_table_is_bounded(self):
        auth, _, _, _ = authenticator(max_tracked=3)
        for i in range(10):
            auth.login(f'n{i}', 'wrong password!')
        self.assertEqual(list(auth._failures), ['n7', 'n8', 'n9'])

    def test_concurrent_hashing_is_bounded_and_the_extra_attempt_is_busy(self):
        gate, inside = threading.Event(), threading.Event()
        auth, store, passwords, _ = authenticator(max_hashing=1)
        verify = passwords.verify

        def slow(stored, password):
            inside.set()
            gate.wait(5)
            return verify(stored, password)
        passwords.verify = slow
        results = []
        worker = threading.Thread(target=lambda: results.append(auth.login('ana', PASSWORD)))
        worker.start()
        self.assertTrue(inside.wait(5))
        self.assertEqual(auth.login('boss', PASSWORD).outcome, 'busy')
        gate.set()
        worker.join(5)
        self.assertEqual(results[0].outcome, 'ok')
        self.assertEqual(auth.login('boss', PASSWORD).outcome, 'ok', 'the slot is released')
        self.assertNotIn('busy', [a[3].get('reason') for a in store.audits])

    def test_store_failures_never_grant_a_login(self):
        auth, store, _, _ = authenticator()
        store.fail = 'unavailable'
        self.assertEqual(auth.login('ana', PASSWORD).outcome, 'unavailable')
        store.fail = None
        original = store.open_session
        store.open_session = unittest.mock.Mock(side_effect=AuthStoreError('x', reason='timeout'))
        self.assertEqual(auth.login('ana', PASSWORD).outcome, 'unavailable', 'no session, no audit: no login')
        store.open_session = original
        store.refuse_open = True  # disabled or password changed between the read and now
        self.assertEqual(auth.login('ana', PASSWORD).outcome, 'invalid')
        self.assertEqual(store.audits[-1][3], {'reason': 'changed', 'username': 'ana'})
        store.audit_fail = 'unavailable'
        self.assertEqual(auth.login('ana', 'wrong password!').outcome, 'invalid', 'a refusal stands unrecorded')
        self.assertEqual(store.sessions, {})

    def test_an_outdated_hash_is_upgraded_on_a_correct_login(self):
        auth, store, passwords, _ = authenticator()
        self.assertEqual(auth.login('legacy', PASSWORD).outcome, 'ok')
        self.assertEqual(store.rehashed, [(4, f'old:{PASSWORD}', f'h:{PASSWORD}')])
        self.assertEqual(auth.login('legacy', 'wrong password!').outcome, 'invalid')
        self.assertEqual(len(store.rehashed), 1, 'never on a wrong password')

    def test_a_login_that_loses_the_upgrade_race_still_succeeds(self):
        """Review L2: two logins read the old hash; the other one upgrades it first."""
        auth, store, passwords, _ = authenticator()
        find = store.find_user

        def find_then_race(username):
            user = find(username)
            if username == 'legacy' and user.password_hash.startswith('old:'):
                store.users['legacy'] = UserRecord(user.id, 'legacy', f'h:{PASSWORD}', 'analyst', False)
            return user  # the record read before the other login's upgrade
        store.find_user = find_then_race
        result = auth.login('legacy', PASSWORD)
        self.assertEqual(result.outcome, 'ok')
        self.assertEqual(store.rehashed[0][1], f'old:{PASSWORD}')
        self.assertEqual(store.users['legacy'].password_hash, f'h:{PASSWORD}', 'the winner\'s hash stays')

    def test_reserved_audit_actors_are_not_usernames(self):
        for name in ('anonymous', 'system'):
            self.assertFalse(valid_username(name))
        self.assertTrue(valid_username('systems'))


@ARGON2
class Argon2Tests(unittest.TestCase):
    def test_parameters_are_pinned_and_written_in_the_hash(self):
        passwords = Argon2Passwords()
        stored = passwords.hash(PASSWORD)
        self.assertTrue(stored.startswith('$argon2id$v=19$m=65536,t=3,p=4$'), stored)
        self.assertTrue(passwords.verify(stored, PASSWORD))
        self.assertFalse(passwords.needs_rehash(stored))
        self.assertNotEqual(stored, passwords.hash(PASSWORD), 'salted')

    def test_verify_never_raises_and_old_parameters_need_a_rehash(self):
        cheap = Argon2Passwords(time_cost=1, memory_kib=8192, parallelism=1)
        stored = cheap.hash(PASSWORD)
        strong = Argon2Passwords()
        self.assertTrue(strong.verify(stored, PASSWORD))
        self.assertTrue(strong.needs_rehash(stored))
        for bad_hash, password in ((stored, 'wrong'), ('$argon2id$garbage', PASSWORD), ('', PASSWORD),
                                   (None, PASSWORD), (stored, None), (stored, 'x' * 5000)):
            self.assertFalse(strong.verify(bad_hash, password))
        self.assertFalse(strong.needs_rehash('not a hash'))

    def test_the_same_text_composed_differently_verifies(self):
        passwords = Argon2Passwords(time_cost=1, memory_kib=8192, parallelism=1)
        stored = passwords.hash('caf\u00e9 au lait 2026')
        self.assertTrue(passwords.verify(stored, 'cafe\u0301 au lait 2026'))

    def test_new_password_rules(self):
        self.assertEqual(check_new_password('ٹیسٹ پاس ورڈ ۲۰۲۶ ok'), 'ٹیسٹ پاس ورڈ ۲۰۲۶ ok')
        for password, rule in (('short', '12 to 256'), ('x' * 257, '12 to 256'), (' leading space1', 'white'),
                               ('tab\tinside here', 'control'), ('ana-is-my-name-1', 'username'), (None, 'text')):
            with self.assertRaisesRegex(PasswordRuleError, rule):
                check_new_password(password, 'ana')


# -- API with the fake store ----------------------------------------------------------

@API
class AuthApiTests(unittest.TestCase):
    def make(self, cookie_secure=True, history=None, hub=None):
        self.auth, self.store, self.passwords, self.clock = authenticator()
        service = analysis.AnalysisService(lambda: api_fixture.Detector(), model_id='fx-model')
        app = create_app(service, auth=self.auth, cookie_secure=cookie_secure, history=history, hub=hub)
        client = TestClient(app, base_url='https://testserver')
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def login(self, client, username='ana', password=PASSWORD, **kwargs):
        return client.post('/v1/auth/login', json={'username': username, 'password': password}, **kwargs)

    def test_every_v1_route_needs_a_session_and_the_open_routes_stay_open(self):
        client = self.make()
        for method, path in (('GET', '/v1/calls'), ('GET', '/v1/calls/c1'), ('GET', '/v1/calls/c1/history'),
                             ('GET', '/v1/history/calls'), ('GET', '/v1/alerts'), ('GET', '/v1/alerts?format=csv'),
                             ('GET', '/v1/jobs/x'), ('GET', '/v1/auth/me'), ('POST', '/v1/auth/logout'),
                             ('POST', '/v1/analyze'), ('GET', '/v1/not-a-route')):
            response = client.request(method, path)
            self.assertEqual((response.status_code, response.json()), (401, {'detail': 'Login required'}), path)
        for path in ('/healthz', '/metrics', '/openapi.json'):
            self.assertEqual(client.get(path).status_code, 200, path)
        self.assertEqual(client.get('/v1/calls', headers={'cookie': 'ava_session=' + 'A' * 43}).status_code, 401)
        metrics = client.get('/metrics').text
        self.assertIn('ava_auth_refused_requests_total{reason="no_session"} 12\n', metrics)
        self.assertIn('ava_api_requests_total{route="/v1/calls",status="4xx"} 2\n', metrics)

    def test_an_upload_without_a_session_is_refused_before_its_body_is_read(self):
        client = self.make()
        response = client.post('/v1/analyze', files={'file': ('x.wav', api_fixture.wav(1.0), 'audio/wav')})
        self.assertEqual(response.status_code, 401)
        # A body over the limit is still 401 (not 413): nothing is parsed for a stranger.
        big = client.post('/v1/analyze', content=b'x', headers={'content-length': str(10 ** 12),
                                                                 'content-type': 'multipart/form-data; boundary=x'})
        self.assertEqual(big.status_code, 401)

    def test_login_sets_a_strict_http_only_secure_cookie_and_opens_the_routes(self):
        client = self.make()
        response = self.login(client)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {'username': 'ana', 'role': 'analyst', 'expires_ts': 1e9})
        cookie = response.headers['set-cookie']
        token = next(iter(self.store.sessions))
        for part in (f'ava_session={token}', 'HttpOnly', 'Secure', 'SameSite=strict', 'Path=/', 'Max-Age=3600'):
            self.assertIn(part, cookie)
        self.assertNotIn(token, response.text, 'the token is only in the cookie')
        self.assertEqual(client.get('/v1/auth/me').json(), {'username': 'ana', 'role': 'analyst', 'expires_ts': 1e9})
        self.assertEqual(client.get('/v1/calls').status_code, 503, 'past the session check: no live source here')
        upload = client.post('/v1/analyze', files={'file': ('x.wav', api_fixture.wav(8.0), 'audio/wav')})
        self.assertEqual(upload.status_code, 200, 'an analyst may analyse files')

    def test_the_lab_can_drop_the_secure_flag(self):
        client = self.make(cookie_secure=False)
        self.assertNotIn('Secure', self.login(client).headers['set-cookie'])

    def test_login_refusals(self):
        client = self.make()
        wrong = self.login(client, password='wrong password!')
        unknown = self.login(client, username='nobody')
        self.assertEqual((wrong.status_code, wrong.json()), (401, {'detail': 'Invalid username or password'}))
        self.assertEqual((unknown.status_code, unknown.json()), (401, wrong.json()), 'same answer')
        self.assertNotIn('set-cookie', wrong.headers)
        for body in (b'not json', b'[]', json.dumps({'username': 'ana'}).encode(),
                     json.dumps({'username': 'ana', 'password': 1}).encode(),
                     json.dumps({'username': 'ana', 'password': PASSWORD, 'role': 'admin'}).encode(), b'\xff\xfe'):
            response = client.post('/v1/auth/login', content=body, headers={'content-type': 'application/json'})
            self.assertEqual(response.status_code, 422, body)
        huge = client.post('/v1/auth/login', content=b'{' + b' ' * 5000 + b'}')
        self.assertEqual(huge.status_code, 413)

        def chunks():
            for _ in range(10):
                yield b' ' * 1000
        self.assertEqual(client.post('/v1/auth/login', content=chunks()).status_code, 413, 'chunked body bounded')
        statuses = [self.login(client, password='wrong password!').status_code for _ in range(4)]
        self.assertEqual(statuses, [401, 401, 401, 429], 'the fifth failure for ana locks the name')
        locked = self.login(client)
        self.assertEqual((locked.status_code, locked.json()['detail']), (429, 'Too many failed attempts; try again later'))
        self.auth.store.fail = 'unavailable'
        down = self.login(client, username='boss')
        self.assertEqual((down.status_code, down.headers['retry-after']), (503, '1'))
        metrics = client.get('/metrics').text
        for outcome, count in (('ok', 0), ('invalid', 5), ('locked', 2), ('busy', 0), ('unavailable', 1)):
            self.assertIn(f'ava_auth_logins_total{{outcome="{outcome}"}} {count}\n', metrics)

    def test_a_store_failure_during_the_session_check_is_503_never_a_pass(self):
        client = self.make()
        self.login(client)
        self.store.fail = 'timeout'
        response = client.get('/v1/auth/me')
        self.assertEqual((response.status_code, response.json()), (503, {'detail': 'Account store unavailable (timeout)'}))
        self.assertIn('ava_auth_refused_requests_total{reason="store_unavailable"} 1\n', client.get('/metrics').text)

    def test_logout_ends_the_session_and_clears_the_cookie(self):
        client = self.make()
        self.login(client)
        token = client.cookies['ava_session']
        response = client.post('/v1/auth/logout')
        self.assertEqual(response.status_code, 204)
        self.assertIn('ava_session=""', response.headers['set-cookie'])
        self.assertNotIn(token, self.store.sessions)
        self.assertEqual(self.store.audits[-1], ('ana', 'auth.logout', 'ok', {}))
        self.assertNotIn('ava_session', client.cookies)
        self.assertEqual(client.get('/v1/auth/me', headers={'cookie': f'ava_session={token}'}).status_code, 401,
                         'the old token is dead')

    def test_cross_origin_writes_are_refused_even_with_a_session(self):
        client = self.make()
        evil = {'origin': 'https://evil.example'}
        self.assertEqual(self.login(client, headers=evil).status_code, 403, 'login CSRF')
        self.assertEqual(self.store.sessions, {})
        self.assertEqual(self.login(client, headers={'origin': 'https://testserver'}).status_code, 200)
        for method, path in (('POST', '/v1/auth/logout'), ('POST', '/v1/analyze'), ('DELETE', '/v1/calls')):
            response = client.request(method, path, headers={'sec-fetch-site': 'cross-site'})
            self.assertEqual((response.status_code, response.json()), (403, {'detail': 'Cross-origin request refused'}))
        self.assertEqual(len(self.store.sessions), 1, 'logout was refused')
        self.assertEqual(client.get('/v1/auth/me', headers=evil).status_code, 200, 'reads are not refused by origin')
        self.assertIn('ava_auth_refused_requests_total{reason="cross_origin"} 4\n', client.get('/metrics').text)

    def test_csv_exports_are_audited_and_not_sent_when_the_audit_fails(self):
        page = {'alerts': [{'alert_id': 'a1', 'direction': 'caller', 'state': 'open', 'raised_ts': 1.0,
                            'raised_window': 1, 'raised_ema': 0.8, 'peak_ema': 0.9, 'closed_ts': None,
                            'cleared_window': None, 'cleared_ema': None, 'call_id': 'c1', 'session': 1}],
                'next_cursor': None}

        class History:
            def alerts(self, *args):
                return page
        client = self.make(history=History())
        self.login(client)
        response = client.get('/v1/alerts?format=csv&state=open&min_confidence=0.5&since=10')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.store.audits[-1], ('ana', 'alerts.export', 'ok', {
            'state': 'open', 'since': 10.0, 'until': None, 'min_confidence': 0.5, 'limit': 1000, 'continued': False,
            'rows': 1}))
        audits = len(self.store.audits)
        self.assertEqual(client.get('/v1/alerts').status_code, 200)
        self.assertEqual(len(self.store.audits), audits, 'a JSON search is not an export')
        self.store.audit_fail = 'unavailable'
        refused = client.get('/v1/alerts?format=csv')
        self.assertEqual((refused.status_code, refused.json()['detail']),
                         (503, 'Export not recorded in the audit log (unavailable)'))
        self.assertNotIn('text/csv', refused.headers['content-type'])

    def test_the_websocket_needs_a_session_and_a_same_origin_handshake(self):
        from services.events.fanout import EventHub
        from starlette.websockets import WebSocketDisconnect
        # The test client connects as ws:// (not wss://), so a Secure cookie would not be sent.
        client = self.make(hub=EventHub(), cookie_secure=False)
        with self.assertRaises(WebSocketDisconnect) as refused:
            with client.websocket_connect('/v1/events'):
                pass
        self.assertEqual(refused.exception.code, 1008)
        self.login(client)
        with self.assertRaises(WebSocketDisconnect):
            with client.websocket_connect('/v1/events', headers={'origin': 'https://evil.example'}):
                pass
        with client.websocket_connect('/v1/events', headers={'origin': 'https://testserver'}) as socket:
            socket.send_text('hello')  # accepted: the socket is open
        metrics = client.get('/metrics').text
        self.assertIn('ava_auth_refused_requests_total{reason="no_session"} 1\n', metrics)
        self.assertIn('ava_auth_refused_requests_total{reason="cross_origin"} 1\n', metrics)

    def test_an_open_websocket_is_closed_once_its_session_ends(self):
        from services.events.fanout import EventHub
        from starlette.websockets import WebSocketDisconnect
        client = self.make(hub=EventHub(), cookie_secure=False)
        self.login(client)
        with unittest.mock.patch.object(app_module, 'SESSION_RECHECK_S', 0.05):
            with client.websocket_connect('/v1/events') as socket:
                self.store.sessions.clear()  # revoked elsewhere (another tab, an operator)
                with self.assertRaises(WebSocketDisconnect) as closed:
                    socket.receive_text()
        self.assertEqual((closed.exception.code, closed.exception.reason), (1008, 'Session ended; log in again'))

    def test_a_store_failure_during_the_websocket_recheck_closes_it(self):
        from services.events.fanout import EventHub
        from starlette.websockets import WebSocketDisconnect
        client = self.make(hub=EventHub(), cookie_secure=False)
        self.login(client)
        with unittest.mock.patch.object(app_module, 'SESSION_RECHECK_S', 0.05):
            with client.websocket_connect('/v1/events') as socket:
                self.store.fail = 'unavailable'  # fails closed: no session check, no events
                with self.assertRaises(WebSocketDisconnect) as closed:
                    socket.receive_text()
        self.assertEqual(closed.exception.code, 1008)

    def test_a_stalled_store_cannot_take_every_worker_thread(self):
        """Review M1: session checks are bounded; probes keep answering."""
        release, entered = threading.Event(), threading.Semaphore(0)
        with unittest.mock.patch.object(app_module, 'MAX_ACCOUNT_CALLS', 2):
            client = self.make()
        self.login(client)
        token = client.cookies['ava_session']
        resolve = self.store.resolve

        def stalled(value):
            entered.release()
            release.wait(10)
            return resolve(value)
        self.store.resolve = stalled
        results = []
        workers = [threading.Thread(target=lambda: results.append(
            client.get('/v1/auth/me', headers={'cookie': f'ava_session={token}'}).status_code)) for _ in range(2)]
        try:
            for worker in workers:
                worker.start()
            for _ in workers:
                self.assertTrue(entered.acquire(timeout=10), 'both slots taken')
            busy = client.get('/v1/auth/me')
            self.assertEqual((busy.status_code, busy.json()['detail']), (503, 'Account store unavailable (busy)'))
            login = self.login(client, username='boss')
            self.assertEqual((login.status_code, login.json()['detail']), (503, 'Login unavailable (busy); retry later'))
            self.assertEqual(client.get('/healthz').status_code, 200)
            self.assertEqual(client.get('/metrics').status_code, 200)
        finally:
            release.set()
            for worker in workers:
                worker.join(10)
        self.assertEqual(results, [200, 200])

    def test_mutating_requests_are_audited_after_they_are_answered(self):
        client = self.make()
        self.login(client)
        ok = client.post('/v1/analyze', files={'file': ('x.wav', api_fixture.wav(8.0), 'audio/wav')})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(self.store.audits[-1], ('ana', 'api.analyze', 'ok',
                                                 {'route': '/v1/analyze', 'method': 'POST', 'status': 200}))
        bad = client.post('/v1/analyze', files={'file': ('x.wav', b'not a wav', 'audio/wav')})
        self.assertEqual(bad.status_code, 422)
        self.assertEqual(self.store.audits[-1][1:], ('api.analyze', 'refused',
                                                     {'route': '/v1/analyze', 'method': 'POST', 'status': 422}))
        self.assertEqual(client.delete('/v1/calls').status_code, 405)
        self.assertEqual(self.store.audits[-1][1:3], ('api.request', 'refused'))
        reads = len(self.store.audits)
        client.get('/v1/auth/me')
        self.assertEqual(len(self.store.audits), reads, 'reads are not audited')
        self.store.audit_fail = 'unavailable'
        again = client.post('/v1/analyze', files={'file': ('x.wav', api_fixture.wav(8.0), 'audio/wav')})
        self.assertEqual(again.status_code, 200, 'the answer stands; the missing row is counted')
        self.assertIn('ava_audit_write_failures_total 1\n', client.get('/metrics').text)
        self.store.audit_fail = None
        client.post('/v1/auth/logout')
        self.assertEqual(self.store.audits[-1][1], 'auth.logout', 'logout writes its own row only')

    def test_without_auth_nothing_changes(self):
        service = analysis.AnalysisService(lambda: api_fixture.Detector(), model_id='fx-model')
        with TestClient(create_app(service)) as client:
            self.assertEqual(client.post('/v1/auth/login', json={}).status_code, 404)
            self.assertEqual(client.get('/v1/jobs/x').status_code, 404, 'no session needed')
            self.assertNotIn('ava_auth_', client.get('/metrics').text)


# -- PostgreSQL -----------------------------------------------------------------------

@REAL
@ARGON2
class PostgresAuthTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from services.auth.store import PostgresAuthStore
        from services.store import migrate
        self.psycopg, self.PostgresAuthStore = psycopg, PostgresAuthStore
        self.schema = f'ava_test_{uuid.uuid4().hex[:12]}'
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(f'CREATE SCHEMA {self.schema}')
        self.connections = []
        self.addCleanup(self.drop_schema)
        migrate.apply_migrations(self.connect())
        self.passwords = Argon2Passwords(time_cost=1, memory_kib=8192, parallelism=1)

    def drop_schema(self):
        for connection in self.connections:
            connection.close()
        with self.psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA {self.schema} CASCADE')

    def connect(self):
        connection = self.psycopg.connect(DSN, options=f'-c search_path={self.schema}')
        self.connections.append(connection)
        return connection

    def sql(self, statement, *args):
        with self.psycopg.connect(DSN, options=f'-c search_path={self.schema}', autocommit=True) as connection:
            cursor = connection.execute(statement, args or None)
            return cursor.fetchall() if cursor.description else cursor.rowcount

    def make_store(self, **kwargs):
        store = self.PostgresAuthStore(self.connect, **kwargs)
        self.addCleanup(store.close)
        return store

    def add(self, store, name='ana', role='analyst', password=PASSWORD):
        return store.add_user(name, self.passwords.hash(password), role, actor='system:test')

    def test_login_resolve_logout_with_audit_rows(self):
        store = self.make_store()
        self.add(store)
        auth = Authenticator(store, self.passwords)
        result = auth.login('ana', PASSWORD)
        self.assertEqual(result.outcome, 'ok')
        self.assertRegex(result.token, r'^[A-Za-z0-9_-]{43}$')
        stored = self.sql('SELECT token_sha256, expires_at - created_at FROM auth_sessions')
        self.assertEqual(len(stored), 1)
        self.assertNotEqual(stored[0][0], result.token, 'only the digest is stored')
        self.assertEqual(stored[0][1].total_seconds(), 12 * 3600)
        principal = store.resolve(result.token)
        self.assertEqual((principal.username, principal.role), ('ana', 'analyst'))
        self.assertAlmostEqual(principal.expires_ts, result.principal.expires_ts, places=3)
        self.assertIsNone(store.resolve(result.token[:-1] + ('A' if result.token[-1] != 'A' else 'B')))
        self.assertIsNone(store.resolve('short'))
        self.assertEqual(auth.login('ana', 'wrong password!').outcome, 'invalid')
        self.assertTrue(store.close_session(result.token, principal))
        self.assertIsNone(store.resolve(result.token))
        self.assertFalse(store.close_session(result.token, principal), 'already closed')
        rows = [(r['actor'], r['action'], r['outcome'], r['detail']) for r in reversed(store.recent_audit())]
        self.assertEqual(rows, [('system:test', 'user.add', 'ok', {'username': 'ana', 'role': 'analyst'}),
                                ('ana', 'auth.login', 'ok', {}),
                                ('anonymous', 'auth.login', 'refused', {'reason': 'wrong_password', 'username': 'ana'}),
                                ('ana', 'auth.logout', 'ok', {})])

    def test_idle_absolute_disabled_and_password_change_end_sessions(self):
        store = self.make_store(idle_s=600, absolute_s=3600)
        self.add(store)
        self.add(store, 'boss', 'admin')
        auth = Authenticator(store, self.passwords)

        def fresh(name='ana'):
            result = auth.login(name, PASSWORD)
            self.assertEqual(result.outcome, 'ok')
            return result.token
        idle = fresh()
        self.sql("UPDATE auth_sessions SET last_seen_at = now() - interval '601 seconds', "
                 "created_at = now() - interval '700 seconds'")
        self.assertIsNone(store.resolve(idle), 'idle too long')
        absolute = fresh()
        # Used a moment ago (not idle) but past its absolute end.
        self.sql("UPDATE auth_sessions SET expires_at = now() - interval '1 second', "
                 "created_at = now() - interval '2 hours' WHERE last_seen_at > now() - interval '1 minute'")
        self.assertIsNone(store.resolve(absolute), 'past the absolute lifetime')
        a, b = fresh(), fresh('boss')
        # The idle session is revoked too: it has not reached its absolute end.
        self.assertEqual(store.set_disabled('ana', True, actor='system:test'), 2)
        self.assertIsNone(store.resolve(a))
        self.assertIsNotNone(store.resolve(b), 'other users keep their sessions')
        self.assertEqual(auth.login('ana', PASSWORD).outcome, 'invalid', 'disabled')
        store.set_disabled('ana', False, actor='system:test')
        self.assertIsNone(store.resolve(a), 'enabling does not bring a revoked session back')
        self.assertEqual(store.set_password('boss', self.passwords.hash('another long secret'), actor='system:test'), 1)
        self.assertIsNone(store.resolve(b))
        self.assertEqual(auth.login('boss', PASSWORD).outcome, 'invalid')
        self.assertEqual(auth.login('boss', 'another long secret').outcome, 'ok')
        actions = [(r['action'], r['detail'].get('sessions_revoked')) for r in reversed(store.recent_audit())
                   if r['actor'] == 'system:test']
        self.assertEqual(actions, [('user.add', None), ('user.add', None), ('user.disable', 2), ('user.enable', 0),
                                   ('user.password', 1)])

    def test_last_seen_is_written_at_most_once_a_minute(self):
        store = self.make_store()
        self.add(store)
        token = Authenticator(store, self.passwords).login('ana', PASSWORD).token
        before = self.sql('SELECT last_seen_at FROM auth_sessions')[0][0]
        store.resolve(token)
        self.assertEqual(self.sql('SELECT last_seen_at FROM auth_sessions')[0][0], before, 'no write per request')
        self.sql("UPDATE auth_sessions SET created_at = created_at - interval '2 minutes', "
                 "last_seen_at = last_seen_at - interval '2 minutes'")
        store.resolve(token)
        self.assertGreater(self.sql('SELECT last_seen_at FROM auth_sessions')[0][0], before)

    def test_a_login_racing_a_password_change_is_refused(self):
        store = self.make_store()
        self.add(store)
        user = store.find_user('ana')
        store.set_password('ana', self.passwords.hash('another long secret'), actor='system:test')
        self.assertIsNone(store.open_session(user), 'the verified hash is no longer current')
        self.assertEqual(self.sql('SELECT count(*) FROM auth_sessions')[0][0], 0)

    def test_an_outdated_hash_is_upgraded(self):
        store = self.make_store()
        self.add(store)  # cheap parameters
        strong = Argon2Passwords(time_cost=2, memory_kib=8192, parallelism=1)
        self.assertEqual(Authenticator(store, strong).login('ana', PASSWORD).outcome, 'ok')
        self.assertIn('t=2', self.sql('SELECT password_hash FROM users')[0][0])
        self.assertEqual(Authenticator(store, strong).login('ana', PASSWORD).outcome, 'ok', 'still the same password')

    def test_account_errors(self):
        store = self.make_store()
        self.add(store)
        with self.assertRaises(AuthStoreError) as conflict:
            self.add(store)
        self.assertEqual(conflict.exception.reason, 'conflict')
        for call in (lambda: store.set_disabled('nobody', True, actor='system:test'),
                     lambda: store.set_password('Bad Name', 'x', actor='system:test')):
            with self.assertRaises(AuthStoreError) as missing:
                call()
            self.assertEqual(missing.exception.reason, 'not_found')
        with self.assertRaises(ValueError):
            store.add_user('Ana', self.passwords.hash(PASSWORD), 'analyst', actor='system:test')
        with self.assertRaises(ValueError):
            store.add_user('eve', self.passwords.hash(PASSWORD), 'root', actor='system:test')
        self.assertEqual([u['username'] for u in store.list_users()], ['ana'], 'nothing half-written')
        self.assertIsNone(store.resolve('A' * 43), 'still usable after the errors')

    def test_sessions_ended_over_a_day_ago_are_purged_at_login(self):
        store = self.make_store()
        self.add(store)
        user_id = self.sql("SELECT id FROM users WHERE username = 'ana'")[0][0]
        for i, age in enumerate(('3 days', '25 hours', '23 hours')):
            self.sql(f"INSERT INTO auth_sessions (token_sha256, user_id, created_at, last_seen_at, expires_at) "
                     f"VALUES (repeat('{i}', 64), %s, now() - interval '{age}' - interval '12 hours', "
                     f"now() - interval '{age}' - interval '12 hours', now() - interval '{age}')", user_id)
        self.assertEqual(Authenticator(store, self.passwords).login('ana', PASSWORD).outcome, 'ok')
        kept = sorted(row[0][0] for row in self.sql('SELECT token_sha256 FROM auth_sessions'))
        self.assertEqual(len(kept), 2, 'the 3-day and 25-hour sessions are gone; 23 hours and the new one stay')
        self.assertIn('2', kept)

    def test_a_role_without_delete_skips_the_purge_but_still_logs_in(self):
        """Re-review M: the purge runs in a savepoint and never refuses a login."""
        self.add(self.make_store())
        user_id = self.sql("SELECT id FROM users WHERE username = 'ana'")[0][0]
        self.sql("INSERT INTO auth_sessions (token_sha256, user_id, created_at, last_seen_at, expires_at) "
                 "VALUES (repeat('a', 64), %s, now() - interval '4 days', now() - interval '4 days', "
                 "now() - interval '3 days')", user_id)
        role = f'ava_nodelete_{uuid.uuid4().hex[:8]}'
        self.sql(f'CREATE ROLE {role}')
        self.addCleanup(self.sql, f'DROP OWNED BY {role}; DROP ROLE {role}')
        for grant in (f'USAGE ON SCHEMA {self.schema}', 'SELECT, INSERT, UPDATE ON users, auth_sessions',
                      'SELECT, INSERT ON audit_log', 'SELECT ON schema_migrations',
                      f'USAGE ON ALL SEQUENCES IN SCHEMA {self.schema}'):
            self.sql(f'GRANT {grant} TO {role}')

        def connect():
            connection = self.connect()
            connection.execute(f'SET ROLE {role}')
            connection.commit()
            return connection
        store = self.PostgresAuthStore(connect)
        self.addCleanup(store.close)
        result = Authenticator(store, self.passwords).login('ana', PASSWORD)
        self.assertEqual(result.outcome, 'ok')
        self.assertEqual(store.purge_failures, 1)
        self.assertIsNotNone(store.resolve(result.token), 'the session and its audit row were committed')
        self.assertEqual(self.sql("SELECT count(*) FROM auth_sessions WHERE token_sha256 = repeat('a', 64)")[0][0], 1)
        self.assertEqual(self.sql("SELECT count(*) FROM audit_log WHERE action = 'auth.login'")[0][0], 1)

    def test_reserved_names_and_a_busy_connection(self):
        store = self.make_store(wait_s=0.1)
        with self.assertRaises(ValueError):
            store.add_user('anonymous', self.passwords.hash(PASSWORD), 'admin', actor='system:test')
        with self.assertRaises(self.psycopg.errors.CheckViolation):
            self.sql("INSERT INTO users (username, password_hash, role) VALUES ('system', '$argon2id$x', 'admin')")
        self.assertTrue(store._lock.acquire(timeout=1))  # another call holds the connection
        try:
            with self.assertRaises(AuthStoreError) as busy:
                store.resolve('A' * 43)
            self.assertEqual(busy.exception.reason, 'busy')
        finally:
            store._lock.release()
        self.assertIsNone(store.resolve('A' * 43), 'usable once the connection is free')

    def test_the_schema_refuses_bad_rows_written_around_the_store(self):
        Check = self.psycopg.errors.CheckViolation
        bad = ["INSERT INTO users (username, password_hash, role) VALUES ('Ana', '$argon2id$x', 'analyst')",
               "INSERT INTO users (username, password_hash, role) VALUES ('ana', 'plain text', 'analyst')",
               "INSERT INTO users (username, password_hash, role) VALUES ('ana', '$argon2i$x', 'analyst')",
               "INSERT INTO users (username, password_hash, role) VALUES ('ana', '$argon2id$x', 'root')",
               "INSERT INTO audit_log (actor, action, outcome) VALUES ('Robert', 'auth.login', 'ok')",
               "INSERT INTO audit_log (actor, action, outcome) VALUES ('system:', 'auth.login', 'ok')",
               "INSERT INTO audit_log (actor, action, outcome) VALUES ('ana', 'auth login', 'ok')",
               "INSERT INTO audit_log (actor, action, outcome) VALUES ('ana', 'auth.login', 'maybe')",
               "INSERT INTO audit_log (actor, action, outcome, detail) VALUES ('ana', 'x', 'ok', '[1]')",
               f"INSERT INTO audit_log (actor, action, outcome, detail) VALUES ('ana', 'x', 'ok', "
               f"jsonb_build_object('v', repeat('x', 5000)))"]
        for statement in bad:
            with self.assertRaises(Check, msg=statement):
                self.sql(statement)

    def test_an_unmigrated_database_is_reported_as_schema(self):
        from services.store import migrate
        schema = f'ava_test_{uuid.uuid4().hex[:12]}'
        self.sql(f'CREATE SCHEMA {schema}')
        self.addCleanup(self.sql, f'DROP SCHEMA {schema} CASCADE')

        def connect():
            connection = self.psycopg.connect(DSN, options=f'-c search_path={schema}')
            self.connections.append(connection)
            return connection
        migrate.apply_migrations(connect(), migrate.load_migrations()[:2])
        store = self.PostgresAuthStore(connect)
        self.addCleanup(store.close)
        with self.assertRaises(AuthStoreError) as error:
            store.resolve('A' * 43)
        self.assertEqual(error.exception.reason, 'schema')

    def test_the_command_line_manages_accounts_without_passwords_in_arguments(self):
        from services.auth.__main__ import EXIT_OK, EXIT_REFUSED, main
        dsn = f'{DSN}{"&" if "?" in DSN else "?"}options=-c%20search_path%3D{self.schema}'
        environ = {'AVA_DATABASE_URL': dsn}

        def run(*argv, stdin=''):
            out, err = io.StringIO(), io.StringIO()
            code = main(['--database-url-env', 'AVA_DATABASE_URL', *argv], environ=environ,
                        stdin=io.StringIO(stdin), stdout=out, stderr=err, passwords=self.passwords)
            text = (out.getvalue() + err.getvalue()).strip()
            return code, json.loads(text) if text else None

        self.assertEqual(run('migrate'), (EXIT_OK, {'event': 'migrated', 'applied': []}))
        self.assertEqual(run('add-user', 'ana', '--role', 'admin', stdin=f'{PASSWORD}\r\n'),
                         (EXIT_OK, {'event': 'user_added', 'username': 'ana', 'role': 'admin'}))
        code, record = run('add-user', 'ana', '--role', 'admin', stdin=f'{PASSWORD}\n')
        self.assertEqual((code, record['reason']), (EXIT_REFUSED, 'a user with this name exists'))
        code, record = run('add-user', 'eve', '--role', 'analyst', stdin='short\n')
        self.assertEqual((code, record['reason']), (EXIT_REFUSED, 'password must be 12 to 256 characters'))
        code, record = run('add-user', 'eve', '--role', 'analyst')
        self.assertEqual((code, record['reason']), (EXIT_REFUSED, 'no password on standard input'))
        code, record = run('add-user', 'anonymous', '--role', 'admin', stdin=f'{PASSWORD}\n')
        self.assertEqual(code, EXIT_REFUSED, 'an audit actor name is not an account')
        store = self.make_store()
        auth = Authenticator(store, self.passwords)
        token = auth.login('ana', PASSWORD).token
        self.assertIsNotNone(token, 'the CRLF line ending was not part of the password')
        self.assertEqual(run('set-password', 'ana', stdin='a different long one\n'),
                         (EXIT_OK, {'event': 'password_set', 'username': 'ana', 'sessions_revoked': 1}))
        self.assertIsNone(store.resolve(token))
        self.assertEqual(run('disable', 'nobody')[1]['reason'], 'no such user')
        self.assertEqual(run('set-role', 'ana', '--role', 'analyst')[0], EXIT_OK)
        code, record = run('list-users')
        self.assertEqual([(u['username'], u['role'], u['disabled'], u['active_sessions']) for u in record['users']],
                         [('ana', 'analyst', False, 0)])
        code, record = run('audit', '--limit', '3')
        self.assertEqual([r['action'] for r in record['rows']], ['user.role', 'user.password', 'auth.login'])
        everything = json.dumps(run('audit', '--limit', '100')[1])
        for secret in (PASSWORD, 'a different long one', 'short'):
            self.assertNotIn(secret, everything)
        self.assertEqual(main(['--database-url-env', 'NOPE', 'list-users'], environ={}, stderr=io.StringIO()),
                         EXIT_REFUSED)

    def test_the_api_end_to_end_on_the_real_store(self):
        if TestClient is None:
            self.skipTest('requirements-api.txt not installed')
        store = self.make_store()
        self.add(store)
        auth = Authenticator(store, self.passwords)
        service = analysis.AnalysisService(lambda: api_fixture.Detector(), model_id='fx-model')
        with TestClient(create_app(service, auth=auth), base_url='https://testserver') as client:
            self.assertEqual(client.get('/v1/auth/me').status_code, 401)
            response = client.post('/v1/auth/login', json={'username': 'ana', 'password': PASSWORD})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(client.get('/v1/auth/me').json()['username'], 'ana')
            self.assertEqual(client.post('/v1/auth/logout').status_code, 204)
            self.assertEqual(client.get('/v1/auth/me').status_code, 401)
        actions = [(r['actor'], r['action']) for r in reversed(store.recent_audit())]
        self.assertEqual(actions, [('system:test', 'user.add'), ('ana', 'auth.login'), ('ana', 'auth.logout')])

    def test_concurrent_logins_share_one_connection_safely(self):
        store = self.make_store(wait_s=10)
        self.add(store)
        auth = Authenticator(store, self.passwords, max_hashing=8)
        results, errors = [], []

        def worker():
            try:
                results.append(auth.login('ana', PASSWORD).outcome)
            except Exception as error:  # pragma: no cover - reported below
                errors.append(error)
        threads = [threading.Thread(target=worker) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual((errors, sorted(results)), ([], ['ok'] * 6))
        self.assertEqual(self.sql('SELECT count(*) FROM auth_sessions')[0][0], 6)


if __name__ == '__main__':
    unittest.main()
