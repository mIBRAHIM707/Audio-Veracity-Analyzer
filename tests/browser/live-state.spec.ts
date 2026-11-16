// Claude-authored unit cases (16 November 2026) for backlog 7.1: the pure live-board
// merge rules in dashboard/src/live.ts (PRD FR-U1, FR-U2, FR-A3, FR-A4). Authored by
// Claude (Claude Code), not GPT-6 Astra. No browser or server: Playwright's runner is
// used only because it already transpiles TypeScript here (the dashboard has no unit
// test runner). Runs in the `foundation` project, so CI runs it on Linux.
import { expect, test } from '@playwright/test';
import {
  applyDetailAlerts, applyEnvelope, applySnapshot, connectionWasHealthy, EMPTY_BOARD, MAX_ENDED_SHOWN, MAX_FEED,
  parseMessage, parseSummary, reconnectDelayMs, STABLE_CONNECTION_MS, TREND_POINTS,
} from '../../dashboard/src/live';
import type { Board, CallCard, CallDetail, Envelope } from '../../dashboard/src/live';

let seq = 0;
const ev = (type: string, callId: string | null, data: Record<string, unknown> = {}, at?: number): Envelope =>
  ({ type, call_id: callId, seq: at ?? ++seq, ts: 1000 + seq, data });
const scoreData = (score: number | null, extra: Record<string, unknown> = {}) =>
  ({ score, ema: score, state: 'monitoring', degraded: false, effective_stride_s: 2, quality: 'ok', ...extra });
const fold = (board: Board, events: Envelope[]) => events.reduce(applyEnvelope, board);

function summary(callId: string, extra: Record<string, unknown> = {}): Record<string, unknown> {
  return { call_id: callId, status: 'active', state: 'monitoring', started_ts: 1000, ended_ts: null, end_reason: null,
           score_trend: [0.1], degraded: false, alerts_open: 0, alerts_total: 0, last_seq: 0, last_score: 0.1,
           last_ema: 0.1, quality: 'ok', stale: false, ...extra };
}
const cardOf = (callId: string, extra: Record<string, unknown> = {}): CallCard => parseSummary(summary(callId, extra));

function detailOf(card: CallCard, alerts: { id: string; state: 'open' | 'cleared' | 'ended_open'; ts: number }[]): CallDetail {
  return { card, points: [], buckets: [], droppedBuckets: 0,
           alerts: alerts.map(a => ({ key: `${card.callId}\u0000${a.id}`, callId: card.callId, alertId: a.id, direction: 'caller',
                                      raisedTs: a.ts, state: a.state, ema: null, live: false })) };
}

test('LS01 parseMessage: envelopes, the gap control message and unreadable frames', () => {
  expect(parseMessage('{"type":"stream.gap","missed":3}')).toEqual({ kind: 'gap' });
  for (const bad of [42, 'not json', '[]', '{"type":"score.updated"}',
    JSON.stringify({ type: 'call.party', call_id: 'a', seq: 1, ts: 1, data: {} }),
    JSON.stringify({ type: 'score.updated', call_id: 'a', seq: -1, ts: 1, data: {} }),
    JSON.stringify({ type: 'score.updated', call_id: 'a', seq: 1.5, ts: 1, data: {} }),
    JSON.stringify({ type: 'score.updated', call_id: '', seq: 1, ts: 1, data: {} }),
    JSON.stringify({ type: 'score.updated', call_id: 'x'.repeat(129), seq: 1, ts: 1, data: {} }),
    JSON.stringify({ type: 'score.updated', call_id: null, seq: 1, ts: 1, data: {} }),
    JSON.stringify({ type: 'score.updated', call_id: 'a', seq: 1, ts: 1, data: [] })]) {
    expect(parseMessage(bad)).toEqual({ kind: 'invalid' });
  }
  const health = parseMessage(JSON.stringify({ type: 'system.health', call_id: null, seq: 0, ts: 5, data: { reason: 'redis down' } }));
  expect(health).toEqual({ kind: 'event', envelope: { type: 'system.health', call_id: null, seq: 0, ts: 5, data: { reason: 'redis down' } } });
});

test('LS02 parseSummary: refuses unusable rendered fields, keeps the newest trend points', () => {
  for (const bad of [null, summary('a', { status: 'gone' }), summary('a', { state: 'odd' }), summary('a', { score_trend: [2] }),
    summary('a', { last_seq: 'x' }), summary('a', { degraded: 'no' }), summary('')]) {
    expect(() => parseSummary(bad)).toThrow();
  }
  const card = cardOf('a', { score_trend: Array.from({ length: TREND_POINTS + 5 }, (_, i) => (i % 2 ? null : 0.5)),
                             last_ema: 7, quality: 'bad' });
  expect(card.trend).toHaveLength(TREND_POINTS);
  expect(card.lastEma).toBeNull(); // out of range: shown as unknown, not as a number
  expect(card.quality).toBeNull();
});

test('LS03 applyEnvelope: per-call seq rule, and malformed events create no call', () => {
  let b = fold(EMPTY_BOARD, [ev('call.started', 'a', {}, 10), ev('score.updated', 'a', scoreData(0.2), 12)]);
  expect(b.calls.a.trend).toEqual([0.2]);
  // seq is hub-wide: an event for another call with a lower seq still applies to that call.
  b = applyEnvelope(b, ev('call.started', 'b', {}, 11));
  expect(b.calls.b.status).toBe('active');
  const before = b;
  b = fold(b, [ev('score.updated', 'a', scoreData(0.9), 12), ev('score.updated', 'a', scoreData(0.9), 3)]);
  expect(b.calls.a.trend).toEqual([0.2]);
  expect(b.skippedEvents).toBe(before.skippedEvents + 2);
  b = fold(b, [ev('score.updated', 'c', { score: 3, state: 'monitoring', degraded: false }, 20),
               ev('alert.raised', 'd', {}, 21), ev('call.ended', 'e', { final_state: 'nope' }, 22)]);
  expect(Object.keys(b.calls).sort()).toEqual(['a', 'b']);
  expect(b.invalidEvents).toBe(3);
});

test('LS04 alerts: one count per alert ID, clears close it, live alerts drive the notification', () => {
  let b = fold(EMPTY_BOARD, [ev('call.started', 'a'), ev('alert.raised', 'a', { alert_id: 'x', direction: 'caller', ema: 0.8 }),
    ev('alert.raised', 'a', { alert_id: 'x', direction: 'caller', ema: 0.8 })]);
  expect(b.calls.a).toMatchObject({ state: 'alert', alertsOpen: 1, alertsTotal: 1 });
  expect(b.liveAlerts).toBe(1);
  expect(b.feed.map(f => [f.alertId, f.state, f.live])).toEqual([['x', 'open', true]]);
  b = applyEnvelope(b, ev('alert.cleared', 'a', { alert_id: 'x' }));
  expect(b.calls.a.alertsOpen).toBe(0);
  expect(b.feed[0].state).toBe('cleared');
  // A clear for an alert the page never saw lowers the snapshot's open count, not below 0.
  b = applySnapshot(b, [cardOf('a', { alerts_open: 1, alerts_total: 2, last_seq: b.calls.a.lastSeq, state: 'alert' })]);
  b = fold(b, [ev('alert.cleared', 'a', { alert_id: 'unseen' }), ev('alert.cleared', 'a', { alert_id: 'unseen2' })]);
  expect(b.calls.a.alertsOpen).toBe(0);
  // The feed is bounded.
  b = fold(b, Array.from({ length: MAX_FEED + 10 }, (_, i) => ev('alert.raised', 'a', { alert_id: `n${i}` })));
  expect(b.feed).toHaveLength(MAX_FEED);
  expect(b.feed[0].alertId).toBe(`n${MAX_FEED + 9}`);
});

test('LS05 snapshot then queued events: newer ones apply, older ones are skipped', () => {
  // The snapshot holds the call up to seq 50; the page queued 49 (already in it), 51, 52.
  let b = applySnapshot(EMPTY_BOARD, [cardOf('a', { last_seq: 50, score_trend: [0.1, 0.2] })]);
  b = fold(b, [ev('score.updated', 'a', scoreData(0.3), 49), ev('score.updated', 'a', scoreData(0.4), 51),
               ev('score.updated', 'a', scoreData(0.5, { state: 'suspicious' }), 52)]);
  expect(b.calls.a.trend).toEqual([0.1, 0.2, 0.4, 0.5]);
  expect(b.calls.a.state).toBe('suspicious');
  expect(b.calls.a.lastSeq).toBe(52);
  expect(b.skippedEvents).toBe(1);
});

test('LS06 a call that really ended takes no more events; an idle-expired one resumes with its alerts open again', () => {
  let b = fold(EMPTY_BOARD, [ev('call.started', 'a'), ev('call.ended', 'a', { final_state: 'monitoring', reason: 'bye' })]);
  b = applyEnvelope(b, ev('score.updated', 'a', scoreData(0.4)));
  expect(b.calls.a.status).toBe('ended');
  b = fold(b, [ev('call.started', 'q'), ev('alert.raised', 'q', { alert_id: 'k' }),
               ev('call.ended', 'q', { final_state: 'alert', reason: 'idle_expired' })]);
  expect(b.calls.q.alertsOpen).toBe(0);
  expect(b.feed.find(f => f.alertId === 'k')?.state).toBe('ended_open');
  b = applyEnvelope(b, ev('score.updated', 'q', scoreData(0.9, { state: 'alert' })));
  expect(b.calls.q).toMatchObject({ status: 'active', endedTs: null, endReason: null, alertsOpen: 1 });
  expect(b.feed.find(f => f.alertId === 'k')?.state).toBe('open');
});

test('LS07 pruning keeps the newest ended calls; late events for a pruned call make no phantom card', () => {
  let b = EMPTY_BOARD;
  for (let i = 0; i <= MAX_ENDED_SHOWN; i += 1) {
    b = fold(b, [ev('call.started', `e${i}`), ev('call.ended', `e${i}`, { final_state: 'monitoring', reason: 'bye' })]);
  }
  b = fold(b, [ev('call.started', 'idle'), ev('call.ended', 'idle', { final_state: 'monitoring', reason: 'idle_expired' })]);
  // e0 and e1 are the oldest; e0 went first, then e1 when the idle call ended.
  expect(b.calls.e0).toBeUndefined();
  expect(b.calls.e1).toBeUndefined();
  expect(Object.values(b.calls).filter(c => c.status === 'ended')).toHaveLength(MAX_ENDED_SHOWN);
  expect(b.forgotten).toEqual(['e0', 'e1']);
  b = applyEnvelope(b, ev('score.updated', 'e0', scoreData(0.2)));
  expect(b.calls.e0).toBeUndefined();
  // A snapshot that lists the call again makes it known (and its rules apply again).
  b = applySnapshot(b, [cardOf('e0', { status: 'ended', ended_ts: 2000, end_reason: 'bye', last_seq: 1 })]);
  expect(b.forgotten).toEqual(['e1']);
  expect(b.calls.e0.status).toBe('ended');
});

test('LS08 snapshot reconciliation of the feed after a disconnection', () => {
  let b = fold(EMPTY_BOARD, [ev('call.started', 'a'), ev('alert.raised', 'a', { alert_id: 'x' }),
                             ev('call.started', 'b'), ev('alert.raised', 'b', { alert_id: 'y' }),
                             ev('call.started', 'c'), ev('alert.raised', 'c', { alert_id: 'z' })]);
  b = applySnapshot(b, [cardOf('a', { alerts_open: 0, alerts_total: 1, last_seq: 900 }),
                        cardOf('b', { status: 'ended', ended_ts: 2000, end_reason: 'bye', alerts_total: 1, last_seq: 900 }),
                        cardOf('c', { alerts_open: 1, alerts_total: 1, state: 'alert', last_seq: 900 })]);
  const state = (id: string) => b.feed.find(f => f.alertId === id)?.state;
  expect([state('x'), state('y'), state('z')]).toEqual(['cleared', 'ended_open', 'open']);
  // Counted alert IDs survive the snapshot, so a repeated raise is not counted again.
  b = applyEnvelope(b, ev('alert.raised', 'c', { alert_id: 'z' }, 901));
  expect(b.calls.c.alertsTotal).toBe(1);
  expect(b.liveAlerts).toBe(3);
});

test('LS09 detail alerts join the feed silently, unless the board is already newer', () => {
  let b = applySnapshot(EMPTY_BOARD, [cardOf('a', { alerts_open: 1, alerts_total: 2, state: 'alert', last_seq: 10 })]);
  const stale = detailOf(cardOf('a', { last_seq: 9 }), [{ id: 'old', state: 'open', ts: 1 }]);
  expect(applyDetailAlerts(b, stale)).toBe(b);
  b = applyDetailAlerts(b, detailOf(cardOf('a', { last_seq: 10 }), [{ id: 'p', state: 'cleared', ts: 1 }, { id: 'q', state: 'open', ts: 2 }]));
  expect(b.feed.map(f => [f.alertId, f.state, f.live])).toEqual([['q', 'open', false], ['p', 'cleared', false]]);
  expect(b.liveAlerts).toBe(0);
  expect(applyEnvelope(b, ev('alert.raised', 'a', { alert_id: 'q' }, 11)).liveAlerts).toBe(0); // already known
  // A call shown ended: its open alerts are added as open at call end.
  let e = applySnapshot(EMPTY_BOARD, [cardOf('z', { status: 'ended', ended_ts: 5, end_reason: 'bye', last_seq: 4 })]);
  e = applyDetailAlerts(e, detailOf(cardOf('z', { last_seq: 4 }), [{ id: 'r', state: 'open', ts: 1 }]));
  expect(e.feed[0].state).toBe('ended_open');
});

test('LS10 reconnect back-off grows with jitter; only a healthy connection resets it', () => {
  expect([0, 1, 2, 3, 4, 9].map(n => reconnectDelayMs(n))).toEqual([1000, 2000, 4000, 8000, 10000, 10000]);
  expect(reconnectDelayMs(0, 0)).toBe(750);
  expect(reconnectDelayMs(1, 0.999999)).toBe(2500);
  expect(reconnectDelayMs(9, 1)).toBe(12500);
  expect(reconnectDelayMs(-3, Number.NaN)).toBe(1000);
  expect(reconnectDelayMs(2, -4)).toBe(3000);
  expect(connectionWasHealthy(null, 10, false)).toBe(false); // never opened
  expect(connectionWasHealthy(0, 50, false)).toBe(false); // accepted then closed at once
  expect(connectionWasHealthy(0, 50, true)).toBe(true);
  expect(connectionWasHealthy(0, STABLE_CONNECTION_MS - 1, false)).toBe(false);
  expect(connectionWasHealthy(0, STABLE_CONNECTION_MS, false)).toBe(true);
});
