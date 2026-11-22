# Local accounts and audit log (backlog 7.3; PRD FR-U7, §6.3; architecture §2.5, §6)

Added 19 Nov 2026 (daily run, attempt 7). FR-U7 is P1: "login with username and
password; two roles: `analyst` (read, feedback) and `admin` (settings)". PRD §6.3:
"role-based access and an audit log for logins, setting changes, feedback and exports".

| File | Role |
| --- | --- |
| `passwords.py` | Password rules for new passwords and Argon2id hashing (argon2-cffi) |
| `store.py` | `PostgresAuthStore`: users, sessions and audit rows (migration 0003) |
| `service.py` | `Authenticator`: login decisions (equal work for unknown names, lockout, bounded hashing) |
| `__main__.py` | Operator commands: `migrate`, `add-user`, `set-password`, `disable`, `enable`, `set-role`, `list-users`, `audit` |

The API side (login/logout/me routes, the session check on every /v1 route and the
WebSocket, cross-site refusal, audited CSV export) is in `services/api/app.py`;
`python -m services.api --auth on` turns it on (see `services/api/README.md`).

## Decisions

- **Hashing.** Argon2id, t=3, m=64 MiB, p=4, 16-byte salt, 32-byte tag: the RFC 9106
  second recommended option and argon2-cffi 25.1.0's default, pinned in code. Passwords
  are NFC-normalised before hashing. A login with an older-parameter hash re-hashes it.
  At most 2 hashes run at once per API process (`busy` beyond that: each takes 64 MiB).
- **New passwords.** 12-256 characters, no control characters, no leading/trailing
  space, must not contain the username. No composition rules (NIST SP 800-63B style).
- **Usernames.** Lower-case ASCII, `a-z` first, then `a-z0-9._-`, at most 64. No
  look-alike Unicode names.
- **Sessions.** A random 32-byte token in an `ava_session` cookie (HttpOnly,
  SameSite=Strict, Secure by default, Path=/); the database keeps only its SHA-256.
  Idle limit 30 min, absolute limit 12 h, both checked with the database clock;
  `last_seen_at` is written at most once a minute. Disabling a user, changing its
  password or role revokes its sessions in the same transaction. An open WebSocket
  re-checks its session every 60 s and closes 1008 when it has ended.
- **Refusals.** Unknown name, wrong password and disabled user give one answer (401
  `Invalid username or password`) and cost one Argon2 verify each (a dummy hash for
  unknown names). Five failures for a name within 15 min lock that name for 15 min
  (429), known or not, so the lock does not reveal which names exist. The failure table
  is in memory per API process (a restart forgets it) and bounded to 10,000 names.
  Accepted limit: anyone can lock a name out for 15 min; there is no per-address limit
  (behind a proxy every client has the proxy's address).
- **Cross-site requests.** Besides SameSite=Strict, a POST/PUT/PATCH/DELETE or
  WebSocket handshake whose `Origin` is another host:port, or with
  `Sec-Fetch-Site: cross-site`, is refused 403 (login included).
- **Audit log.** One row per login (`auth.login ok`, in the session's transaction),
  refused login (`auth.login refused` with the reason and, for an existing account
  only, the name tried: an unknown "name" may be a password typed into the wrong field;
  not for attempts on a name already locked), logout, CSV export (`alerts.export` with
  filters and row count), alert feedback (`alerts.feedback` with alert ID, label and
  feedback ID, in the label's transaction; refused/failed submissions get the
  middleware's row), every other mutating request with a session (architecture
  §2.5 audit middleware: `api.analyze` for uploads, `api.request` otherwise; route,
  method and status, never a body), and every operator change (`user.add`,
  `user.password`, `user.disable`, `user.enable`, `user.role`, actor `system:cli`).
  Logins, logouts, exports and operator changes do not happen if their row cannot be
  written (an export is not sent: 503). An upload's row is written after it is
  answered (the outcome is only known then); a failed write is counted in
  `ava_audit_write_failures_total`. Rows are never updated or deleted by AVA code; an
  operator can also `REVOKE UPDATE, DELETE ON audit_log` from the API's role.
  Passwords and session tokens are never written; `anonymous` and `system` cannot be
  account names (they are audit actors).
- **Bounded work.** At most 8 session checks, logins or audit writes run in worker
  threads at once per API process (session checks and logins answer 503 busy beyond
  that; audit writes wait up to 2 s for a slot, then count as a failure, or refuse an
  export), so a stalled database cannot take the threads /healthz and /metrics need.
  Sessions that ended over a day ago are deleted in batches of 100 at each login, in a
  savepoint: a role without DELETE on `auth_sessions` skips the purge, never the login.
- **Roles.** Both roles can use every route that exists, including alert feedback
  (FR-A7, 19 Nov attempt 8; FR-U7 gives it to analysts, and an admin can label too).
  `admin` is reserved for the settings routes (FR-A9, FR-U7 settings page), not built yet.
- **Other modules' audited writes.** `PostgresAuthStore.transaction(work,
  required_migration=N)` runs one transaction on the account connection (same waits,
  timeouts and failure reasons; the schema version is read again while it is short of
  `N`, so a database migrated after start-up is seen without a restart), and
  `audit_in(db, ...)` appends an audit row inside it. `services/store/feedback.py` uses
  them so a label and its `alerts.feedback` row commit or roll back together.
- **Bootstrap.** No default account. The first admin is created by the operator:

```text
set AVA_DATABASE_URL=postgresql://...      (never on the command line)
uv run --no-project --python 3.11 --with-requirements requirements-store.txt --with-requirements requirements-auth.txt ^
    python -m services.auth --database-url-env AVA_DATABASE_URL migrate
uv run ... python -m services.auth --database-url-env AVA_DATABASE_URL add-user ana --role admin
```

`add-user` and `set-password` read the password as the first line of standard input
(a terminal prompts twice without echo). Output is one JSON record; refusals exit 2.

## Not done (recorded)

(The dashboard sign-in form is in `dashboard/src/AuthGate.tsx`; see `dashboard/README.md`.)

- Admin-only settings routes and page (FR-A9, FR-U7) and their audit rows; the
  dashboard's feedback buttons (the FR-A7 API is built); the PRD's "without FR-U7 the audit log records `actor=system`" path (no audit
  writer runs when auth is off).
- TLS: the operator's reverse proxy or Ingress (k3s README); Compose/k3s wiring of
  `--auth on` with a database role limited to the tables above.
- Password reset by users themselves (an admin or operator sets passwords).
- Audit log retention and export for the reviewer: rows are kept indefinitely (a
  refused-login flood can add about 40 rows/s at most); a retention rule is an
  operator/PRD decision.

## Deployment notes (review L5-L7)

- `--auth on` lets the API listen on a non-loopback address. `/healthz`, `/readyz`
  (which runs a detector warm-up the first time), `/metrics` and `/openapi.json` stay
  reachable without a login, and without a TLS proxy the password travels in clear:
  put the API behind the operator's TLS proxy or Ingress and keep `/metrics` on the
  internal network.
- The cross-site check compares `Origin` with `Host`: a proxy must forward the
  browser's `Host` header unchanged (nginx `proxy_set_header Host $host;`).
- History and accounts share `--database-url-env`, so with `--auth on` that role needs
  write access to the account tables; the history reads still run `READ ONLY`. A
  separate URL for each is not provided yet.

## Tests

`tests/unit/test_local_auth.py` (Claude-authored, 19 Nov 2026): pure rule and
`Authenticator` cases with fakes, Argon2 cases (need requirements-auth.txt), API cases
with a fake store, and PostgreSQL cases (`AVA_TEST_POSTGRES_DSN`, see
`services/store/README.md`): the real store and schema checks, the command line and
the API end to end. Mutants checked: no cross-origin check; no dummy verify for unknown
names (both fail the tests).
