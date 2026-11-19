// Claude-authored unit cases (19 November 2026) for backlog 7.3 / PRD FR-U6: the pure
// history and alert-search rules in dashboard/src/history.ts (response validation, UTC
// filter parsing, query building, list bounds, error text, stored timeline). Authored by
// Claude (Claude Code), not GPT-6 Astra. No browser or server: Playwright's runner only
// transpiles TypeScript here. Runs in the `foundation` project, so CI runs it on Linux.
import { expect, test } from '@playwright/test';
import {
  alertKey, alertQuery, appendBounded, callsQuery, describeHistoryError, filtersFromForm, formatUtc, HistoryDataError,
  MAX_LISTED, parseAlertPage, parseCallHistory, parseCallPage, parseUtcInput, sessionTimeline,
} from '../../dashboard/src/history';
import type { AlertRow } from '../../dashboard/src/history';

const T0 = 1763500000; // 2025-11-18T21:06:40Z

function session(extra: Record<string, unknown> = {}): Record<string, unknown> {
  return { session: 7, call_id: 'k1', status: 'ended', started_ts: T0, started_seen: true, state: 'monitoring',
           driving_direction: 'caller', quality: 'ok', degraded: false, effective_stride_s: 2, last_event_ts: T0 + 30,
           ended_ts: T0 + 30, final_state: 'monitoring', end_reason: 'bye', end_inferred: false, speech_seconds: 7.5,
           verdict: 'genuine', ...extra };
}
const callRow = (extra: Record<string, unknown> = {}) =>
  session({ alerts_total: 2, alerts_open: 1, windows_total: 10, windows_scored: 9, score_max: 0.9, ...extra });
function alertRow(extra: Record<string, unknown> = {}): Record<string, unknown> {
  return { alert_id: 'a1', direction: 'caller', state: 'cleared', raised_ts: T0 + 6, raised_window: 1, raised_ema: 0.55,
           closed_ts: T0 + 8, cleared_window: 2, cleared_ema: 0.3, peak_ema: 0.8, call_id: 'k1', session: 7, ...extra };
}
const win = (extra: Record<string, unknown> = {}) => ({
  direction: 'caller', window: 0, ts: T0 + 4, seq: 2, scored: true, score: 0.2, ema: 0.2, provisional: true,
  direction_state: 'monitoring', state: 'monitoring', quality: 'ok', degraded: false, effective_stride_s: 2,
  concealed_fraction: 0, ...extra });

test('HS01 parseCallPage: reads the API summary and refuses unusable rows', () => {
  const page = parseCallPage({ calls: [callRow(), callRow({ session: 6, status: 'active', ended_ts: null, end_reason: null,
                                                            score_max: 1.7, end_inferred: true })], next_before: 6 });
  expect(page.nextBefore).toBe(6);
  expect(page.calls[0]).toMatchObject({ session: 7, callId: 'k1', status: 'ended', endReason: 'bye', alertsOpen: 1,
                                        alertsTotal: 2, windowsScored: 9, windowsTotal: 10, scoreMax: 0.9 });
  expect(page.calls[1].scoreMax).toBeNull(); // out of 0-1: shown as unknown, never as 1.70
  expect(page.calls[1].endInferred).toBe(true);
  // 128 characters as PostgreSQL counts them, even when each takes two UTF-16 units (review L4).
  const wide = '😀'.repeat(128);
  expect(parseCallPage({ calls: [callRow({ call_id: wide })], next_before: null }).calls[0].callId).toBe(wide);
  expect(() => parseCallPage({ calls: [callRow({ call_id: wide + 'x' })], next_before: null })).toThrow(HistoryDataError);
  expect(parseCallPage({ calls: [], next_before: null })).toEqual({ calls: [], nextBefore: null });
  for (const bad of [null, [], { calls: {} }, { calls: [], next_before: 0 }, { calls: [], next_before: 1.5 },
    { calls: [], next_before: '3' }, { calls: [callRow({ session: 0 })] }, { calls: [callRow({ session: 2.5 })] },
    { calls: [callRow({ call_id: '' })] }, { calls: [callRow({ call_id: 'x'.repeat(129) })] },
    { calls: [callRow({ status: 'gone' })] }, { calls: [callRow({ alerts_total: -1 })] },
    { calls: [callRow({ windows_scored: 1.5 })] }, { calls: [callRow({ started_ts: 'yesterday' })] },
    { calls: [callRow({ started_seen: 'yes' })] }, { calls: [callRow({ verdict: 3 })] }, { calls: [42] }]) {
    expect(() => parseCallPage({ next_before: null, ...(bad as object) })).toThrow(HistoryDataError);
  }
});

test('HS02 parseAlertPage: states, cursor alphabet, peak EMA range and required call ID', () => {
  const page = parseAlertPage({ alerts: [alertRow(), alertRow({ alert_id: 'a0', state: 'open', direction: null,
                                                                 raised_ema: null, peak_ema: Number.MAX_VALUE })],
                                next_cursor: 'MTc2MzUwMDAwNjAwMDAwMDphMQ' });
  expect(page.alerts[0]).toEqual({ alertId: 'a1', direction: 'caller', state: 'cleared', raisedTs: T0 + 6, raisedWindow: 1,
                                   raisedEma: 0.55, closedTs: T0 + 8, callId: 'k1', session: 7, peakEma: 0.8 });
  expect(page.alerts[1]).toMatchObject({ direction: null, raisedEma: null, peakEma: null, state: 'open' });
  expect(page.nextCursor).toBe('MTc2MzUwMDAwNjAwMDAwMDphMQ');
  for (const bad of [{ alerts: [alertRow({ state: 'closed' })] }, { alerts: [alertRow({ call_id: null })] },
    { alerts: [alertRow({ session: 0 })] }, { alerts: [alertRow({ alert_id: '' })] },
    { alerts: [alertRow({ raised_ts: 'x' })] }, { alerts: [], next_cursor: 'a+b/c=' }, { alerts: [], next_cursor: '' },
    { alerts: [], next_cursor: 'x'.repeat(1025) }, { alerts: [], next_cursor: 5 }, { alerts: 'none' }]) {
    expect(() => parseAlertPage({ next_cursor: null, ...bad })).toThrow(HistoryDataError);
  }
});

test('HS03 parseCallHistory: sessions with alerts, windows and every truncation flag', () => {
  const history = parseCallHistory({
    call_id: 'k1', sessions_total: 3, sessions_truncated: true,
    sessions: [{ ...session(), alerts: [alertRow()], alerts_total: 300, alerts_truncated: true,
                 windows: [win(), win({ window: 1, scored: false, score: null, ema: null })], windows_total: 11000,
                 windows_truncated: true }],
  });
  expect(history).toMatchObject({ callId: 'k1', sessionsTotal: 3, sessionsTruncated: true });
  const [s] = history.sessions;
  expect(s).toMatchObject({ alertsTotal: 300, alertsTruncated: true, windowsTotal: 11000, windowsTruncated: true });
  expect(s.alerts[0].alertId).toBe('a1');
  expect(s.windows.map(w => [w.window, w.scored, w.score])).toEqual([[0, true, 0.2], [1, false, null]]);
  for (const bad of [{ call_id: 'k1', sessions: 'x', sessions_total: 1 },
    { call_id: 'k1', sessions: [{ ...session(), alerts: [], windows: [{ scored: 'yes' }], alerts_total: 0, windows_total: 1 }],
      sessions_total: 1 },
    { call_id: 'k1', sessions: [{ ...session(), alerts: [], windows: [], alerts_total: 0, windows_total: 0 }] }]) {
    expect(() => parseCallHistory(bad)).toThrow(HistoryDataError);
  }
});

test('HS04 parseUtcInput: datetime-local text read as UTC; impossible or malformed times refused', () => {
  expect(parseUtcInput('')).toBeNull();
  expect(parseUtcInput('  ')).toBeNull();
  expect(parseUtcInput('2025-11-18T21:06')).toBe(1763499960);
  expect(parseUtcInput('2025-11-18T21:06:40')).toBe(T0);
  expect(parseUtcInput('2025-11-18T21:06:40.5')).toBe(T0 + 0.5);
  expect(parseUtcInput('1970-01-01T00:00')).toBe(0);
  for (const bad of ['2026-02-30T10:00', '2026-13-01T00:00', '2026-01-01T24:00', '2026-01-01T10:60',
    '2026-01-01T10:00:60', '1969-12-31T23:59', '2026-01-01', '2026-01-01 10:00', 'yesterday', '2026-01-01T10:00Z',
    '+02026-01-01T10:00']) {
    expect(() => parseUtcInput(bad), bad).toThrow(RangeError);
  }
  expect(() => parseUtcInput('0050-01-01T00:00')).toThrow('Times must be between 1970 and 3000.');
});

test('HS05 filtersFromForm: state, ordered range and a 0-1 minimum confidence', () => {
  expect(filtersFromForm({ state: 'all', since: '', until: '', minConfidence: '' }))
    .toEqual({ state: 'all', since: null, until: null, minConfidence: null });
  expect(filtersFromForm({ state: 'ended_open', since: '2025-11-18T21:06:40', until: '2025-11-18T21:07', minConfidence: '0.7' }))
    .toEqual({ state: 'ended_open', since: T0, until: T0 + 20, minConfidence: 0.7 });
  expect(filtersFromForm({ state: 'open', since: '', until: '', minConfidence: '0' }).minConfidence).toBe(0);
  expect(filtersFromForm({ state: 'open', since: '', until: '', minConfidence: '1' }).minConfidence).toBe(1);
  const refused = [{ state: 'closed' }, { since: '2025-11-18T21:07', until: '2025-11-18T21:07' },
    { since: '2025-11-18T21:08', until: '2025-11-18T21:07' }, { minConfidence: '1.01' }, { minConfidence: '-0.1' },
    { minConfidence: 'abc' }, { minConfidence: 'NaN' }, { minConfidence: 'Infinity' }];
  for (const change of refused) {
    expect(() => filtersFromForm({ state: 'all', since: '', until: '', minConfidence: '', ...change }), JSON.stringify(change))
      .toThrow(RangeError);
  }
});

test('HS06 alertQuery and callsQuery: only the filters given, encoded; cursor and CSV format', () => {
  expect(alertQuery({ state: 'all', since: null, until: null, minConfidence: null }, { limit: 100 })).toBe('limit=100');
  expect(alertQuery({ state: 'open', since: T0, until: T0 + 0.5, minConfidence: 0 }, { limit: 10000, csv: true }))
    .toBe(`state=open&since=${T0}&until=${T0 + 0.5}&min_confidence=0&limit=10000&format=csv`);
  expect(alertQuery({ state: 'all', since: null, until: null, minConfidence: null }, { limit: 5, cursor: 'Ab-_9' }))
    .toBe('limit=5&cursor=Ab-_9');
  expect(callsQuery('all', 50, null)).toBe('limit=50');
  expect(callsQuery('ended', 50, 17)).toBe('status=ended&limit=50&before=17');
});

test('HS07 formatUtc: fixed UTC text, unknown or out-of-range times as a dash', () => {
  expect(formatUtc(T0)).toBe('2025-11-18 21:06:40 UTC');
  expect(formatUtc(T0 + 0.999)).toBe('2025-11-18 21:06:40 UTC');
  expect(formatUtc(0)).toBe('1970-01-01 00:00:00 UTC');
  for (const unknown of [null, -1, Number.NaN, Number.POSITIVE_INFINITY, 4e10]) expect(formatUtc(unknown)).toBe('—');
});

test('HS08 appendBounded: no repeated keys across pages and never more than MAX_LISTED rows', () => {
  const row = (session: number, alertId: string) => ({ session, alertId } as AlertRow);
  const first = appendBounded([], [row(1, 'a'), row(1, 'b')], alertKey);
  expect(first).toEqual({ items: [row(1, 'a'), row(1, 'b')], capped: false });
  // The same alert ID in another session is another alert; a repeated row is dropped.
  const second = appendBounded(first.items, [row(1, 'b'), row(2, 'b')], alertKey);
  expect(second.items.map(alertKey)).toEqual(['1\u0000a', '1\u0000b', '2\u0000b']);
  const many = Array.from({ length: MAX_LISTED + 5 }, (_, i) => row(i + 1, 'x'));
  const capped = appendBounded(first.items, many, alertKey);
  expect(capped.items).toHaveLength(MAX_LISTED);
  expect(capped.capped).toBe(true);
  expect(appendBounded(capped.items, [row(99999, 'y')], alertKey)).toMatchObject({ capped: true });
  expect(appendBounded(capped.items, [row(99999, 'y')], alertKey).items).toHaveLength(MAX_LISTED);
  // Exactly full with nothing left out is not capped (review L5); a repeat at the cap neither.
  const exact = appendBounded([], many.slice(0, MAX_LISTED), alertKey);
  expect(exact).toMatchObject({ capped: false });
  expect(appendBounded(exact.items, [many[0]], alertKey).capped).toBe(false);
});

test('HS09 describeHistoryError: a plain reason per store failure; server text bounded', () => {
  expect(describeHistoryError(503, 'No call history store is configured')).toMatch(/no history database configured/);
  expect(describeHistoryError(503, 'Call history store unavailable (timeout)')).toMatch(/narrow the filters/);
  expect(describeHistoryError(503, 'Call history store unavailable (schema)')).toMatch(/not migrated/);
  expect(describeHistoryError(503, 'Call history store unavailable (busy)')).toMatch(/try again shortly/);
  expect(describeHistoryError(503, undefined)).toBe('Call history is not available: the service is unavailable.');
  expect(describeHistoryError(404, 'x')).toBe('No stored history for this call.');
  expect(describeHistoryError(422, 'since must be earlier than until'))
    .toBe('The service refused the search: since must be earlier than until');
  expect(describeHistoryError(500, 'x'.repeat(500))).toHaveLength('The service answered with HTTP 500 ().'.length + 200);
});

test('HS10 sessionTimeline: scored windows only, seconds since the session start, stride-wide bars', () => {
  const parsed = parseCallHistory({ call_id: 'k1', sessions_total: 1, sessions: [{
    ...session(), alerts: [], alerts_total: 0, windows_total: 5,
    windows: [win({ ts: T0 + 4 }), win({ window: 1, ts: T0 + 6, direction: 'agent', score: 0.9 }),
              win({ window: 2, ts: T0 + 8, scored: false, score: null }), win({ window: 3, ts: null }),
              win({ window: 4, ts: T0 + 1, effective_stride_s: 4 })],
  }] });
  expect(sessionTimeline(parsed.sessions[0])).toEqual([
    { index: 1, t_start: 2, t_end: 4, partial: false, p_synthetic: 0.2, note: 'caller window 0' },
    { index: 2, t_start: 4, t_end: 6, partial: false, p_synthetic: 0.9, note: 'agent window 1' },
    { index: 5, t_start: 0, t_end: 1, partial: false, p_synthetic: 0.2, note: 'caller window 4' },
  ]);
  // No start time stored (implicit session): the first scored bar starts at 0, a stride wide.
  const implicit = { ...parsed.sessions[0], startedTs: null };
  expect(sessionTimeline(implicit).slice(0, 2)).toMatchObject([{ t_start: 0, t_end: 2 }, { t_start: 2, t_end: 4 }]);
});
