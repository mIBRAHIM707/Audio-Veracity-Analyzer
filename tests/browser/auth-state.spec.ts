// Claude-authored unit cases (19 November 2026, attempt 7) for backlog 7.3 / PRD FR-U7:
// the pure sign-in rules in dashboard/src/auth.ts and the 401/audit texts of
// dashboard/src/history.ts and api.ts. Authored by Claude (Claude Code), not GPT-6 Astra.
// No browser or server: Playwright's runner only transpiles TypeScript here. Runs in the
// `foundation` project, so CI runs it on Linux.
import { expect, test } from '@playwright/test';
import { checkLoginForm, describeLoginError, parseUser, recheckDelayMs } from '../../dashboard/src/auth';
import { describeError } from '../../dashboard/src/api';
import { describeHistoryError } from '../../dashboard/src/history';

test('AS01 a session answer is read only when every field is valid', () => {
  expect(parseUser({ username: 'ana', role: 'analyst', expires_ts: 1000, idle_s: 600, now_ts: 400 }, 99999))
    .toEqual({ kind: 'user', username: 'ana', role: 'analyst', expiresTs: 1000, idleS: 600, endsInMs: 600000 });
  // Time left comes from the server's clock; without `now_ts`, from this PC's.
  expect(parseUser({ username: 'boss', role: 'admin', expires_ts: 2000 }, 1500))
    .toEqual({ kind: 'user', username: 'boss', role: 'admin', expiresTs: 2000, idleS: 1800, endsInMs: 500000 });
  expect(parseUser({ username: 'ana', role: 'analyst', expires_ts: 10, now_ts: 20 })?.endsInMs).toBe(0);
  expect(parseUser({ username: 'ana', role: 'analyst', expires_ts: 10, now_ts: 'x' })).toBeNull();
  for (const idle of [59, 7 * 86400 + 1, 1.5, '600', null]) {
    expect(parseUser({ username: 'ana', role: 'analyst', expires_ts: 1, idle_s: idle }), String(idle)).toBeNull();
  }
  for (const bad of [null, [], 'ana', { username: 'Ana', role: 'analyst', expires_ts: 1 },
    { username: '<img src=x>', role: 'analyst', expires_ts: 1 }, { username: 'ana', role: 'root', expires_ts: 1 },
    { username: 'ana', role: 'analyst', expires_ts: Number.NaN }, { username: 'ana', role: 'analyst', expires_ts: '1' },
    { username: 'a'.repeat(65), role: 'analyst', expires_ts: 1 }, { role: 'analyst', expires_ts: 1 }]) {
    expect(parseUser(bad), JSON.stringify(bad)).toBeNull();
  }
});

test('AS05 the timer re-check comes just after the idle limit (review H1)', () => {
  expect(recheckDelayMs(1800)).toBe(1805000);
  expect(recheckDelayMs(60)).toBe(65000);
});

test('AS02 login refusals never say whether the name exists', () => {
  expect(describeLoginError(401)).toBe('Invalid username or password.');
  expect(describeLoginError(429)).toMatch(/Too many failed attempts for this username/);
  expect(describeLoginError(429)).not.toMatch(/exist|unknown/i);
  expect(describeLoginError(503)).toMatch(/unavailable/);
  expect(describeLoginError(403)).toMatch(/own address/);
  expect(describeLoginError(422)).toMatch(/could not be sent/);
  expect(describeLoginError(500)).toBe('The service answered with HTTP 500.');
});

test('AS03 the form is checked before anything is sent', () => {
  expect(checkLoginForm('', 'x')).toBe('Enter a username and a password.');
  expect(checkLoginForm('ana', '')).toBe('Enter a username and a password.');
  expect(checkLoginForm('a'.repeat(65), 'x')).toBe('Invalid username or password.');
  expect(checkLoginForm('ana', 'x'.repeat(257))).toBe('Invalid username or password.');
  expect(checkLoginForm('ana', 'short')).toBeNull(); // the server decides; rules for new passwords differ
});

test('AS04 a 401 anywhere says the session ended; an unrecorded export says so', () => {
  expect(describeError(401, 'Login required')).toBe('Your session has ended. Sign in again.');
  expect(describeHistoryError(401, 'Login required')).toBe('Your session has ended. Sign in again.');
  expect(describeHistoryError(503, 'Export not recorded in the audit log (unavailable)'))
    .toBe('The export was not sent: it could not be recorded in the audit log. Try again shortly.');
  expect(describeHistoryError(503, 'Account store unavailable (busy)')).toMatch(/session could not be checked/);
  expect(describeHistoryError(503, 'Call history store unavailable (busy)')).toMatch(/too many history reads/);
});
