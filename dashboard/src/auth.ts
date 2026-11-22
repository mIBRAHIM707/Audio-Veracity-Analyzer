// Local accounts in the dashboard (PRD FR-U7; backlog 7.3, 19 Nov 2026). The API turns
// accounts on with `--auth on` (services/auth/README.md); without them `/v1/auth/me` is
// 404 and the dashboard works as before. The session is an HttpOnly cookie the page
// never reads: these calls only learn who is signed in.

import { ApiError, SESSION_CHECK_EVENT, SESSION_ENDED_EVENT } from './api';

export { SESSION_CHECK_EVENT, SESSION_ENDED_EVENT };

export type Role = 'analyst' | 'admin';
export type Session =
  | { kind: 'off' } // the server has no accounts
  | { kind: 'anonymous' }
  | { kind: 'user'; username: string; role: Role; expiresTs: number; idleS: number; endsInMs: number };

export const AUTH_TIMEOUT_MS = 15 * 1000;
export const DEFAULT_IDLE_S = 30 * 60; // services/auth/store.py IDLE_S, if the server does not say

/** Pure: when to re-check a signed-in session on a timer. Just after the server's idle
 * limit: a session that saw no other request since the last check has then expired, so
 * the check itself never keeps an idle session alive (review H1). */
export function recheckDelayMs(idleS: number): number {
  return (idleS + 5) * 1000;
}
export const MAX_USERNAME = 64;
export const MAX_PASSWORD = 256;

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** Pure: the signed-in user from a `/v1/auth/me` or login answer, or null if malformed.
 * `endsInMs`: time left before the absolute end, from the server's own clock (`now_ts`)
 * so a wrong clock on this PC cannot end or stretch the session (re-review N-M1); a
 * server that does not send `now_ts` is compared with `localNowS`. */
export function parseUser(value: unknown, localNowS: number = Date.now() / 1000): Extract<Session, { kind: 'user' }> | null {
  if (!isObject(value) || typeof value.username !== 'string' || !/^[a-z][a-z0-9._-]{0,63}$/.test(value.username)
      || (value.role !== 'analyst' && value.role !== 'admin')
      || typeof value.expires_ts !== 'number' || !Number.isFinite(value.expires_ts)
      || !(value.idle_s === undefined || (Number.isInteger(value.idle_s) && (value.idle_s as number) >= 60
                                          && (value.idle_s as number) <= 7 * 86400))
      || !(value.now_ts === undefined || (typeof value.now_ts === 'number' && Number.isFinite(value.now_ts)))) {
    return null;
  }
  const serverNow = (value.now_ts as number | undefined) ?? localNowS;
  return { kind: 'user', username: value.username, role: value.role, expiresTs: value.expires_ts,
           idleS: (value.idle_s as number | undefined) ?? DEFAULT_IDLE_S,
           endsInMs: Math.max(0, (value.expires_ts - serverNow) * 1000) };
}

/** Pure: the message for a refused login. Never says whether the name exists. */
export function describeLoginError(status: number): string {
  if (status === 401) return 'Invalid username or password.';
  if (status === 429) return 'Too many failed attempts for this username. Wait 15 minutes and try again.';
  if (status === 403) return 'The login was refused because it did not come from this dashboard\'s own address.';
  if (status === 503) return 'Login is unavailable right now (the account store is busy or cannot be reached). Try again shortly.';
  if (status === 422 || status === 413) return 'The username or password could not be sent.';
  return `The service answered with HTTP ${status}.`;
}

/** Pure: a reason not to send the form, or null. Matches the server's username rule. */
export function checkLoginForm(username: string, password: string): string | null {
  if (!username || !password) return 'Enter a username and a password.';
  if (username.length > MAX_USERNAME || password.length > MAX_PASSWORD) return 'Invalid username or password.';
  return null;
}

async function send(url: string, init: RequestInit, signal: AbortSignal): Promise<{ status: number; body: unknown }> {
  const limit = AbortSignal.timeout(AUTH_TIMEOUT_MS);
  const combined = AbortSignal.any([signal, limit]);
  try {
    const response = await fetch(url, { ...init, signal: combined, credentials: 'same-origin' });
    let body: unknown;
    try { body = await response.json(); } catch (error) { if (combined.aborted) throw error; body = undefined; }
    return { status: response.status, body };
  } catch (error) {
    if (signal.aborted) throw error;
    if (limit.aborted) throw new ApiError(0, 'The service did not answer in time.');
    throw new ApiError(0, 'The service could not be reached.');
  }
}

/** Who is signed in: 404 means the server has no accounts; 401 nobody. */
export async function fetchSession(signal: AbortSignal): Promise<Session> {
  const { status, body } = await send('v1/auth/me', { headers: { Accept: 'application/json' } }, signal);
  if (status === 404) return { kind: 'off' };
  if (status === 401) return { kind: 'anonymous' };
  if (status !== 200) throw new ApiError(status, status === 503
    ? 'The account store is unavailable, so the session cannot be checked. Try again shortly.'
    : `The service answered with HTTP ${status}.`);
  const user = parseUser(body);
  if (!user) throw new ApiError(0, 'The service returned a session the dashboard cannot read.');
  return user;
}

export async function login(username: string, password: string, signal: AbortSignal): Promise<Session> {
  const { status, body } = await send('v1/auth/login', {
    method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify({ username, password }),
  }, signal);
  if (status !== 200) throw new ApiError(status, describeLoginError(status));
  const user = parseUser(body);
  if (!user) throw new ApiError(0, 'The service returned a session the dashboard cannot read.');
  return user;
}

export async function logout(signal: AbortSignal): Promise<void> {
  const { status } = await send('v1/auth/logout', { method: 'POST' }, signal);
  // 401: the session had already ended, which is what logging out wants.
  if (status !== 204 && status !== 401) {
    throw new ApiError(status, status === 503
      ? 'Logging out failed: the account store is unavailable, so the session is still open. Try again.'
      : `Logging out failed (HTTP ${status}).`);
  }
}
