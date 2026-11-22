-- Local accounts, login sessions and the audit log (backlog 7.3, 19 Nov 2026; PRD FR-U7
-- "login with username and password; two roles: analyst and admin", PRD §6.3 "role-based
-- access and an audit log for logins, setting changes, feedback and exports";
-- architecture §2.5 auth, §2.7 `users`, `audit_log`, §6 access control). Written by
-- services/auth/store.py. Never edit after it is applied; add a new numbered file instead.

-- Usernames are lower-case ASCII (no look-alike names); `anonymous` and `system` are
-- audit actors, not accounts. Only Argon2id hashes are stored.
CREATE TABLE users (
    id BIGSERIAL PRIMARY KEY,
    username TEXT NOT NULL UNIQUE
        CHECK (username ~ '^[a-z][a-z0-9._-]{0,63}$' AND username NOT IN ('anonymous', 'system')),
    password_hash TEXT NOT NULL CHECK (password_hash LIKE '$argon2id$%' AND char_length(password_hash) <= 512),
    role TEXT NOT NULL CHECK (role IN ('analyst', 'admin')),
    disabled BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    password_changed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The cookie holds a random token; only its SHA-256 is stored, so a copy of this table
-- cannot be replayed as a login.
CREATE TABLE auth_sessions (
    token_sha256 TEXT PRIMARY KEY CHECK (token_sha256 ~ '^[0-9a-f]{64}$'),
    user_id BIGINT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    CHECK (expires_at > created_at AND last_seen_at >= created_at)
);
CREATE INDEX auth_sessions_user ON auth_sessions (user_id);
-- Each login deletes a bounded batch of sessions that ended over a day ago.
CREATE INDEX auth_sessions_expires ON auth_sessions (expires_at);

-- Append-only by the code that writes it (no UPDATE or DELETE is issued); an operator
-- can also REVOKE UPDATE, DELETE on it from the API's role. `actor` is a username,
-- `anonymous` (a refused login) or `system:<tool>` (an operator command).
CREATE TABLE audit_log (
    id BIGSERIAL PRIMARY KEY,
    at TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor TEXT NOT NULL CHECK (actor ~ '^(anonymous|system:[a-z][a-z0-9_-]{0,31}|[a-z][a-z0-9._-]{0,63})$'),
    action TEXT NOT NULL CHECK (action ~ '^[a-z][a-z0-9_]{0,31}(\.[a-z][a-z0-9_]{0,31}){0,2}$'),
    outcome TEXT NOT NULL CHECK (outcome IN ('ok', 'refused', 'failed')),
    detail JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(detail) = 'object' AND octet_length(detail::text) <= 4096)
);
CREATE INDEX audit_log_at ON audit_log (at, id);
