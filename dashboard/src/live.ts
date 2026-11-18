// Live call board state (PRD FR-U1, FR-U2, FR-A3, FR-A4; backlog 7.1).
// Pure functions only: parsing what the API sends and folding it into the board. The
// component (LiveBoard.tsx) owns the WebSocket, timers and REST reads.
//
// Merge rule: the REST snapshot (`GET /v1/calls`) is the base; WebSocket envelopes are
// applied on top, per call, only when their hub `seq` is above the call's `last_seq`
// (the registry's own rule), so an event the snapshot already holds is not counted
// twice. A `stream.gap` control message means events were lost: the board re-reads REST.

export const CALL_STATES = ['monitoring', 'suspicious', 'alert', 'no_speech', 'unmonitored'] as const;
export type CallState = typeof CALL_STATES[number];
export type Quality = 'ok' | 'low';

export const TREND_POINTS = 20; // services/calls/registry.py TREND_POINTS
export const MAX_FEED = 50;
export const MAX_ENDED_SHOWN = 20;
const IDLE_END_REASON = 'idle_expired'; // services/calls/registry.py

export interface CallCard {
  callId: string;
  status: 'active' | 'ended';
  state: CallState;
  startedTs: number;
  /** The session's own `call.started` was seen (registry `started_seen`); false for a
   * call created from a later event, e.g. after an API restart. */
  startedSeen: boolean;
  endedTs: number | null;
  endReason: string | null;
  lastScore: number | null;
  lastEma: number | null;
  quality: Quality | null;
  degraded: boolean;
  stale: boolean;
  trend: (number | null)[];
  alertsOpen: number;
  alertsTotal: number;
  lastSeq: number;
  /** Alert IDs this page has counted for the call (bounded), independent of the
   * feed's own cap, so a repeated raise is never counted twice. */
  knownAlerts: string[];
}

const MAX_KNOWN_ALERTS = 256; // services/calls/registry.py DEFAULT_MAX_ALERTS

export interface AlertItem {
  key: string; // call ID + alert ID
  callId: string;
  alertId: string;
  direction: string | null;
  raisedTs: number | null;
  state: 'open' | 'cleared' | 'ended_open';
  ema: number | null;
  /** Arrived as a live event (sound/visual notification), not from a snapshot. */
  live: boolean;
}

export interface Envelope {
  type: string;
  call_id: string | null;
  seq: number;
  ts: number;
  data: Record<string, unknown>;
}

export interface Board {
  calls: Record<string, CallCard>;
  feed: AlertItem[];
  health: { reason: string; ts: number } | null;
  /** Events refused by the page (malformed), and events skipped as already applied. */
  invalidEvents: number;
  skippedEvents: number;
  /** Live alerts raised since the page opened (drives the notification). */
  liveAlerts: number;
  /** Calls that really ended and were pruned from the page (bounded), so a late event
   * for one is skipped instead of creating a new "active" card the registry would refuse. */
  forgotten: string[];
}

const MAX_FORGOTTEN = 256;

export const EMPTY_BOARD: Board = {
  calls: {}, feed: [], health: null, invalidEvents: 0, skippedEvents: 0, liveAlerts: 0, forgotten: [],
};

const isObject = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v);
const finite = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v);
const unit = (v: unknown): v is number => finite(v) && v >= 0 && v <= 1;
const isState = (v: unknown): v is CallState => typeof v === 'string' && (CALL_STATES as readonly string[]).includes(v);
const shortText = (v: unknown, max: number): string | null => (typeof v === 'string' && v.length > 0 && v.length <= max ? v : null);

export class LiveDataError extends Error {}

/** One `GET /v1/calls` summary; throws LiveDataError when a rendered field is unusable. */
export function parseSummary(v: unknown): CallCard {
  if (!isObject(v) || !shortText(v.call_id, 128) || (v.status !== 'active' && v.status !== 'ended')
      || !isState(v.state) || !finite(v.started_ts) || !Array.isArray(v.score_trend)
      || !v.score_trend.every(s => s === null || unit(s)) || typeof v.degraded !== 'boolean'
      || !finite(v.alerts_open) || !finite(v.alerts_total) || !finite(v.last_seq)) {
    throw new LiveDataError('The service returned a call summary this page cannot read.');
  }
  return {
    callId: v.call_id as string,
    status: v.status,
    state: v.state,
    startedTs: v.started_ts,
    startedSeen: v.started_seen === true,
    endedTs: finite(v.ended_ts) ? v.ended_ts : null,
    endReason: shortText(v.end_reason, 64),
    lastScore: unit(v.last_score) ? v.last_score : null,
    lastEma: unit(v.last_ema) ? v.last_ema : null,
    quality: v.quality === 'ok' || v.quality === 'low' ? v.quality : null,
    degraded: v.degraded,
    stale: v.stale === true,
    trend: (v.score_trend as (number | null)[]).slice(-TREND_POINTS),
    alertsOpen: v.alerts_open,
    alertsTotal: v.alerts_total,
    lastSeq: v.last_seq,
    knownAlerts: [],
  };
}

export function parseCallList(body: unknown): CallCard[] {
  if (!isObject(body) || !Array.isArray(body.calls)) {
    throw new LiveDataError('The service returned a call list this page cannot read.');
  }
  return body.calls.map(parseSummary);
}

export interface DetailPoint { ts: number; seq: number; score: number | null; state: CallState; direction: string | null; strideS: number }
export interface DetailBucket { startTs: number; endTs: number; scoreMax: number | null; state: string; direction: string | null }
export interface CallDetail {
  card: CallCard;
  alerts: AlertItem[];
  points: DetailPoint[];
  buckets: DetailBucket[];
  droppedBuckets: number;
}

/** `GET /v1/calls/{id}`. Malformed points or alerts are a LiveDataError, not a crash. */
export function parseDetail(v: unknown): CallDetail {
  const card = parseSummary(v);
  const body = v as Record<string, unknown>;
  const timeline = body.timeline;
  if (!Array.isArray(body.alerts) || !isObject(timeline) || !Array.isArray(timeline.full_resolution)
      || !Array.isArray(timeline.downsampled)) {
    throw new LiveDataError('The service returned call details this page cannot read.');
  }
  const alerts = body.alerts.map(a => {
    if (!isObject(a) || !shortText(a.alert_id, 128) || !['open', 'cleared', 'ended_open'].includes(a.state as string)) {
      throw new LiveDataError('The service returned an alert this page cannot read.');
    }
    return {
      key: `${card.callId}\u0000${a.alert_id}`, callId: card.callId, alertId: a.alert_id as string,
      direction: shortText(a.direction, 16), raisedTs: finite(a.raised_ts) ? a.raised_ts : null,
      state: a.state as AlertItem['state'], ema: null, live: false,
    };
  });
  const points = timeline.full_resolution.map(p => {
    if (!isObject(p) || !finite(p.ts) || !finite(p.seq) || !(p.score === null || unit(p.score)) || !isState(p.state)
        || !finite(p.effective_stride_s) || p.effective_stride_s <= 0) {
      throw new LiveDataError('The service returned a timeline this page cannot read.');
    }
    return { ts: p.ts, seq: p.seq, score: p.score as number | null, state: p.state, direction: shortText(p.direction, 16),
             strideS: p.effective_stride_s };
  });
  const buckets = timeline.downsampled.map(b => {
    if (!isObject(b) || !finite(b.start_ts) || !finite(b.end_ts) || !(b.score_max === null || unit(b.score_max))) {
      throw new LiveDataError('The service returned a timeline this page cannot read.');
    }
    return { startTs: b.start_ts, endTs: b.end_ts, scoreMax: b.score_max as number | null,
             state: typeof b.state === 'string' ? b.state : '', direction: shortText(b.direction, 16) };
  });
  const dropped = finite(timeline.dropped_buckets) ? timeline.dropped_buckets : 0;
  return { card, alerts, points, buckets, droppedBuckets: dropped };
}

export type Message = { kind: 'event'; envelope: Envelope } | { kind: 'gap' } | { kind: 'invalid' };

const EVENT_TYPES = new Set(['call.started', 'score.updated', 'alert.raised', 'alert.cleared', 'call.ended', 'system.health']);

/** One WebSocket text frame: an FR-A4 envelope, a `stream.gap` control message, or invalid. */
export function parseMessage(text: unknown): Message {
  if (typeof text !== 'string') return { kind: 'invalid' };
  let v: unknown;
  try {
    v = JSON.parse(text);
  } catch {
    return { kind: 'invalid' };
  }
  if (isObject(v) && v.type === 'stream.gap') return { kind: 'gap' };
  if (!isObject(v) || typeof v.type !== 'string' || !EVENT_TYPES.has(v.type) || !Number.isInteger(v.seq)
      || (v.seq as number) < 0 || !finite(v.ts) || !isObject(v.data)) {
    return { kind: 'invalid' };
  }
  if (v.type === 'system.health' ? !(v.call_id === null || typeof v.call_id === 'string') : !shortText(v.call_id, 128)) {
    return { kind: 'invalid' };
  }
  return { kind: 'event', envelope: { type: v.type, call_id: v.call_id as string | null, seq: v.seq as number, ts: v.ts, data: v.data } };
}

function newCard(callId: string, ts: number): CallCard {
  return { callId, status: 'active', state: 'monitoring', startedTs: ts, startedSeen: false, endedTs: null, endReason: null, lastScore: null,
           lastEma: null, quality: null, degraded: false, stale: false, trend: [], alertsOpen: 0, alertsTotal: 0, lastSeq: 0,
           knownAlerts: [] };
}

function remember(known: string[], alertId: string): string[] {
  return known.includes(alertId) ? known : [...known, alertId].slice(-MAX_KNOWN_ALERTS);
}

function upsertAlert(feed: AlertItem[], item: AlertItem): AlertItem[] {
  const rest = feed.filter(a => a.key !== item.key);
  return [item, ...rest].slice(0, MAX_FEED);
}

/** Folds one envelope into the board. Pure: returns a new board (or the same one when
 * nothing changed). Rules mirror services/calls/registry.py where they overlap. */
export function applyEnvelope(board: Board, e: Envelope): Board {
  if (e.type === 'system.health') {
    const reason = shortText(e.data.reason, 200);
    return reason ? { ...board, health: { reason, ts: e.ts } } : { ...board, invalidEvents: board.invalidEvents + 1 };
  }
  const callId = e.call_id as string;
  const existing = board.calls[callId];
  // After a real end a call takes no more events; one the registry only expired for
  // being idle resumes when an event arrives (registry `revived`).
  const finished = existing?.status === 'ended' && existing.endReason !== IDLE_END_REASON;
  const forgotten = !existing && board.forgotten.includes(callId);
  // A new monitoring session of the same call (18 Nov, 5.QA; registry `new_session`): a
  // live-process restart ends the call monitor_lost and the new process re-taps it (seq
  // restarts only for a consumer of the scorer's own hub; via the API it keeps rising).
  // A start later than the end (or for a call this page has
  // forgotten) opens a fresh card.
  // An abrupt stop (crash, SIGKILL) sends no call.ended (18 Nov, 5.QA M1; registry
  // `superseded_session`): a newer start (higher seq) later than an active card's start
  // replaces that session; its open alerts become ended_open.
  // An idle-expired card is replaced the same way, not revived (review M1).
  const superseded = e.type === 'call.started' && existing !== undefined
    && (existing.status === 'active' || existing.endReason === IDLE_END_REASON)
    && e.seq > existing.lastSeq && e.ts > existing.startedTs;
  const newSession = superseded || (e.type === 'call.started'
    && (forgotten || (existing !== undefined && finished && existing.endedTs !== null && e.ts > existing.endedTs)));
  if (!newSession && ((existing && (e.seq <= existing.lastSeq || finished)) || forgotten)) {
    return { ...board, skippedEvents: board.skippedEvents + 1 };
  }
  const d = e.data;
  // Validate before creating anything, so a malformed event cannot add a phantom call.
  if (e.type === 'score.updated' && (!isState(d.state) || !(d.score === null || unit(d.score)) || typeof d.degraded !== 'boolean')) {
    return { ...board, invalidEvents: board.invalidEvents + 1 };
  }
  if ((e.type === 'alert.raised' || e.type === 'alert.cleared') && !shortText(d.alert_id, 128)) {
    return { ...board, invalidEvents: board.invalidEvents + 1 };
  }
  if (e.type === 'call.ended' && !isState(d.final_state)) {
    return { ...board, invalidEvents: board.invalidEvents + 1 };
  }
  // A new session keeps the earlier session's alert count (its alerts stay in the feed).
  let card: CallCard = newSession
    ? { ...newCard(callId, e.ts), lastSeq: e.seq, alertsTotal: existing?.alertsTotal ?? 0,
        knownAlerts: existing?.knownAlerts ?? [] }
    : { ...(existing ?? newCard(callId, e.ts)), lastSeq: e.seq, stale: false };
  const stillForgotten = newSession ? board.forgotten.filter(id => id !== callId) : board.forgotten;
  let feed = superseded
    ? board.feed.map(a => (a.callId === callId && a.state === 'open' ? { ...a, state: 'ended_open' as const } : a))
    : board.feed;
  if (card.status === 'ended') {
    // Revived after an idle expiry: as in the registry, its alerts open at the expiry are
    // open again.
    const reopened = feed.filter(a => a.callId === callId && a.state === 'ended_open').length;
    card = { ...card, status: 'active', endedTs: null, endReason: null, alertsOpen: card.alertsOpen + reopened };
    feed = feed.map(a => (a.callId === callId && a.state === 'ended_open' ? { ...a, state: 'open' as const } : a));
  }
  let liveAlerts = board.liveAlerts;
  switch (e.type) {
    case 'call.started':
      card.startedTs = Math.min(card.startedTs, e.ts);
      card.startedSeen = true;
      break;
    case 'score.updated': {
      const score = d.score as number | null;
      card = {
        ...card, state: d.state as CallState, degraded: d.degraded as boolean,
        lastScore: score ?? card.lastScore,
        lastEma: unit(d.ema) ? d.ema : card.lastEma,
        quality: d.quality === 'ok' || d.quality === 'low' ? d.quality : card.quality,
        trend: [...card.trend, score].slice(-TREND_POINTS),
      };
      break;
    }
    case 'alert.raised': {
      const alertId = d.alert_id as string;
      const key = `${callId}\u0000${alertId}`;
      card.state = 'alert';
      if (!card.knownAlerts.includes(alertId) && !feed.some(a => a.key === key)) {
        card.alertsOpen += 1;
        card.alertsTotal += 1;
        card.knownAlerts = remember(card.knownAlerts, alertId);
        liveAlerts += 1;
        feed = upsertAlert(feed, { key, callId, alertId, direction: shortText(d.direction, 16), raisedTs: e.ts, state: 'open',
                                   ema: unit(d.ema) ? d.ema : null, live: true });
      }
      break;
    }
    case 'alert.cleared': {
      const key = `${callId}\u0000${d.alert_id as string}`;
      const item = feed.find(a => a.key === key);
      if (item && item.state === 'open') {
        card.alertsOpen = Math.max(0, card.alertsOpen - 1);
        feed = feed.map(a => (a.key === key ? { ...a, state: 'cleared' } : a));
      } else if (!item && card.alertsOpen > 0) {
        card.alertsOpen -= 1; // raised before the snapshot's alerts were loaded
      }
      break;
    }
    case 'call.ended':
      card = { ...card, status: 'ended', state: d.final_state as CallState, endedTs: e.ts, endReason: shortText(d.reason, 64),
               alertsOpen: 0 };
      feed = feed.map(a => (a.callId === callId && a.state === 'open' ? { ...a, state: 'ended_open' } : a));
      break;
    default:
      return { ...board, invalidEvents: board.invalidEvents + 1 };
  }
  const pruned = pruneEnded({ ...board.calls, [callId]: card }, stillForgotten);
  return { ...board, ...pruned, feed, liveAlerts };
}

/** Keeps every active call and the most recent MAX_ENDED_SHOWN ended ones. Calls that
 * really ended (not idle-expired, which may resume) are remembered as forgotten. */
function pruneEnded(calls: Record<string, CallCard>, forgotten: string[]): { calls: Record<string, CallCard>; forgotten: string[] } {
  const ended = Object.values(calls).filter(c => c.status === 'ended');
  if (ended.length <= MAX_ENDED_SHOWN) return { calls, forgotten };
  const keep = new Set(ended.sort((a, b) => (b.endedTs ?? 0) - (a.endedTs ?? 0)).slice(0, MAX_ENDED_SHOWN).map(c => c.callId));
  const dropped = ended.filter(c => !keep.has(c.callId) && c.endReason !== IDLE_END_REASON).map(c => c.callId);
  return {
    calls: Object.fromEntries(Object.entries(calls).filter(([, c]) => c.status === 'active' || keep.has(c.callId))),
    forgotten: [...forgotten.filter(id => !dropped.includes(id)), ...dropped].slice(-MAX_FORGOTTEN),
  };
}

/** Replaces the calls with a REST snapshot (the page's own counted alert IDs carry over).
 * Feed items stay but are reconciled with it: open items of a call the snapshot shows
 * ended become ended_open; those of an active call with no open alert became cleared
 * while the page was not listening. */
export function applySnapshot(board: Board, cards: CallCard[]): Board {
  // A call the snapshot holds is known again, so it is no longer in `forgotten`.
  const { calls, forgotten } = pruneEnded(Object.fromEntries(cards.map(c => [c.callId, {
    ...c, knownAlerts: board.calls[c.callId]?.knownAlerts ?? c.knownAlerts }])),
    board.forgotten.filter(id => !cards.some(c => c.callId === id)));
  const feed = board.feed.map(a => {
    const call = calls[a.callId];
    if (a.state !== 'open' || !call) return a;
    if (call.status === 'ended') return { ...a, state: 'ended_open' as const };
    // Raised before the call's current session began (a superseded session, 5.QA M1;
    // review M2): ended_open, as in the registry. Only when the start was seen: a call
    // recreated implicitly after an API restart has a later start but the same session.
    if (call.startedSeen && a.raisedTs !== null && a.raisedTs < call.startedTs) return { ...a, state: 'ended_open' as const };
    return call.alertsOpen === 0 ? { ...a, state: 'cleared' as const } : a;
  });
  return { ...board, calls, feed, forgotten };
}

/** Adds a call's alerts from its detail to the feed (no notification), unless the board
 * already holds newer events for that call than the detail does. Open alerts of a call
 * the board shows ended are added as ended_open. */
export function applyDetailAlerts(board: Board, detail: CallDetail): Board {
  const card = board.calls[detail.card.callId];
  if (card && card.lastSeq > detail.card.lastSeq) return board;
  const ended = card?.status === 'ended' || detail.card.status === 'ended';
  let feed = board.feed;
  let known = card?.knownAlerts ?? [];
  for (const alert of detail.alerts) {
    known = remember(known, alert.alertId);
    if (!feed.some(a => a.key === alert.key)) {
      feed = [...feed, ended && alert.state === 'open' ? { ...alert, state: 'ended_open' as const } : alert];
    }
  }
  if (feed === board.feed && (!card || known === card.knownAlerts)) return board;
  feed = [...feed].sort((a, b) => (b.raisedTs ?? 0) - (a.raisedTs ?? 0)).slice(0, MAX_FEED);
  const calls = card ? { ...board.calls, [card.callId]: { ...card, knownAlerts: known } } : board.calls;
  return { ...board, feed, calls };
}

/** Active calls oldest first, then ended calls most recent first. */
export function orderedCalls(board: Board): { active: CallCard[]; ended: CallCard[] } {
  const all = Object.values(board.calls);
  return {
    active: all.filter(c => c.status === 'active').sort((a, b) => a.startedTs - b.startedTs),
    ended: all.filter(c => c.status === 'ended').sort((a, b) => (b.endedTs ?? 0) - (a.endedTs ?? 0)),
  };
}

export function formatDuration(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  const h = Math.floor(s / 3600);
  const mm = String(Math.floor((s % 3600) / 60)).padStart(2, '0');
  const ss = String(s % 60).padStart(2, '0');
  return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

/** Back-off before reconnect attempt `n` (0-based): 1, 2, 4, 8, then 10 s, each scaled
 * by a jitter factor in [0.75, 1.25) from `random` (in [0, 1)) so many pages that lost
 * the server together do not reconnect in step. */
export function reconnectDelayMs(n: number, random: number = 0.5): number {
  const r = Number.isFinite(random) ? Math.min(Math.max(random, 0), 1) : 0.5;
  return Math.round(Math.min(1000 * 2 ** Math.max(0, n), 10000) * (0.75 + 0.5 * r));
}

/** A connection counts as healthy (the back-off restarts from 1 s) only if it delivered
 * a frame or stayed open this long. A server that accepts and at once closes (no live
 * source, too many subscribers) therefore still backs off. */
export const STABLE_CONNECTION_MS = 5000;

export function connectionWasHealthy(openedAtMs: number | null, closedAtMs: number, receivedFrame: boolean): boolean {
  return receivedFrame || (openedAtMs !== null && closedAtMs - openedAtMs >= STABLE_CONNECTION_MS);
}
