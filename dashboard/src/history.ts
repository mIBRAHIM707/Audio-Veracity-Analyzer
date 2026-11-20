// Stored call history and alert search (PRD FR-U6 on FR-A3/FR-A6; backlog 7.3).
// Pure parsing and query building, plus the three bounded reads. The page
// (HistoryPage.tsx) owns state and rendering. Sources, same origin only:
//   GET /v1/history/calls?status=&limit=&before=   stored sessions, newest first
//   GET /v1/calls/{id}/history                       every stored session of one call
//   GET /v1/alerts?state=&since=&until=&min_confidence=&limit=&cursor=[&format=csv]
// All three read PostgreSQL through services/store/history.py; without a store they
// answer 503. Times are Unix seconds from the server and are shown and entered in UTC.

import { ApiError, reportSessionEnded } from './api';

export const CALLS_PAGE = 50; // services/store/history.py DEFAULT_LIMIT
export const ALERTS_PAGE = 100; // DEFAULT_ALERT_LIMIT
export const CSV_EXPORT_LIMIT = 10000; // services/api/app.py MAX_ALERT_CSV_LIMIT
/** Rows a list keeps on the page; "Load more" stops there (narrow the filters instead). */
export const MAX_LISTED = 1000;
// The store bounds one read at about 25 s (15 s deadline + 10 s statement timeout) plus
// a reconnect; a shorter client limit would abandon reads that still succeed (review L2).
export const REQUEST_TIMEOUT_MS = 30000;
// Known limit (review L3): an abandoned read still runs on the server and holds the
// reader's single connection, so a quick retry may be answered `busy`.
export const CSV_TIMEOUT_MS = 60000;
export const MAX_ID = 128; // calls.call_id CHECK; alert IDs follow the same rule
const MAX_TS = 32503680000; // history.py MAX_TS (3000-01-01 UTC)

export type CallStatusFilter = 'all' | 'active' | 'ended';
export type AlertState = 'open' | 'cleared' | 'ended_open';
export type AlertStateFilter = 'all' | AlertState;

export interface SessionSummary {
  session: number;
  callId: string;
  status: 'active' | 'ended';
  startedTs: number | null;
  startedSeen: boolean;
  state: string | null;
  endedTs: number | null;
  lastEventTs: number | null;
  finalState: string | null;
  endReason: string | null;
  endInferred: boolean;
  speechSeconds: number | null;
  verdict: string | null;
  degraded: boolean;
  quality: string | null;
  /** FR-U5: the call's codec as the tap detected it (e.g. "alaw"); null = not detected or not recorded. */
  codec: string | null;
}

export interface CallRow extends SessionSummary {
  alertsTotal: number;
  alertsOpen: number;
  windowsTotal: number;
  windowsScored: number;
  scoreMax: number | null;
}

export interface CallPage { calls: CallRow[]; nextBefore: number | null }

export interface StoredAlert {
  alertId: string;
  direction: string | null;
  state: AlertState;
  raisedTs: number | null;
  raisedWindow: number | null;
  raisedEma: number | null;
  closedTs: number | null;
}

export interface AlertRow extends StoredAlert {
  callId: string;
  session: number;
  peakEma: number | null;
}

export interface AlertPage { alerts: AlertRow[]; nextCursor: string | null }

export interface StoredWindow {
  direction: string | null;
  window: number | null;
  ts: number | null;
  scored: boolean;
  score: number | null;
  ema: number | null;
  stride: number | null;
}

export interface StoredSession extends SessionSummary {
  alerts: StoredAlert[];
  alertsTotal: number;
  alertsTruncated: boolean;
  windows: StoredWindow[];
  windowsTotal: number;
  windowsTruncated: boolean;
}

export interface CallHistory {
  callId: string;
  sessions: StoredSession[];
  sessionsTotal: number;
  sessionsTruncated: boolean;
}

export interface AlertFilters {
  state: AlertStateFilter;
  /** Inclusive, Unix seconds. */
  since: number | null;
  /** Exclusive, Unix seconds. */
  until: number | null;
  minConfidence: number | null;
}

/** What the analyst typed: `datetime-local` values (read as UTC) and a number field. */
export interface AlertForm {
  state: string;
  since: string;
  until: string;
  minConfidence: string;
}

const ALERT_STATES = new Set<string>(['open', 'cleared', 'ended_open']);
const UNREADABLE = 'The service returned call history this page cannot read.';

export class HistoryDataError extends Error {
  constructor(message = UNREADABLE) {
    super(message);
    this.name = 'HistoryDataError';
  }
}

const isObject = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v);
const finite = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v);
/** 1-128 characters as PostgreSQL counts them (code points, not UTF-16 units: review L4). */
const validId = (v: unknown): v is string => typeof v === 'string' && v.length > 0 && [...v].length <= MAX_ID;

/** A number, null for null/absent; anything else is refused. */
function optNumber(v: unknown): number | null {
  if (v === null || v === undefined) return null;
  if (!finite(v)) throw new HistoryDataError();
  return v;
}

function optString(v: unknown): string | null {
  if (v === null || v === undefined) return null;
  if (typeof v !== 'string') throw new HistoryDataError();
  return v;
}

function count(v: unknown): number {
  if (!finite(v) || v < 0 || !Number.isInteger(v)) throw new HistoryDataError();
  return v;
}

/** An EMA or score shown as 0-1: out-of-range values are shown as unknown, not as a number. */
const unit = (v: unknown): number | null => {
  const n = optNumber(v);
  return n !== null && n >= 0 && n <= 1 ? n : null;
};

function parseSession(v: unknown): SessionSummary {
  if (!isObject(v) || !finite(v.session) || !Number.isInteger(v.session) || v.session < 1 || !validId(v.call_id)
      || (v.status !== 'active' && v.status !== 'ended') || typeof v.started_seen !== 'boolean') {
    throw new HistoryDataError();
  }
  return {
    session: v.session, callId: v.call_id, status: v.status, startedTs: optNumber(v.started_ts),
    startedSeen: v.started_seen, state: optString(v.state), endedTs: optNumber(v.ended_ts),
    lastEventTs: optNumber(v.last_event_ts), finalState: optString(v.final_state), endReason: optString(v.end_reason),
    endInferred: v.end_inferred === true, speechSeconds: optNumber(v.speech_seconds), verdict: optString(v.verdict),
    degraded: v.degraded === true, quality: optString(v.quality), codec: codecName(v.codec),
  };
}

/** FR-U5 detected codec: an Asterisk format name (the API checks the same rule); any
 *  other value is shown as not detected rather than refusing the whole answer. */
const CODEC = /^[a-z][a-z0-9_]{0,23}(\|[a-z][a-z0-9_]{0,23}){0,3}$/;
function codecName(v: unknown): string | null {
  return typeof v === 'string' && CODEC.test(v) ? v : null;
}

export function parseCallPage(v: unknown): CallPage {
  if (!isObject(v) || !Array.isArray(v.calls)) throw new HistoryDataError();
  const nextBefore = v.next_before;
  if (!(nextBefore === null || (finite(nextBefore) && Number.isInteger(nextBefore) && nextBefore >= 1))) {
    throw new HistoryDataError();
  }
  const calls = v.calls.map(row => {
    if (!isObject(row)) throw new HistoryDataError();
    return { ...parseSession(row), alertsTotal: count(row.alerts_total), alertsOpen: count(row.alerts_open),
             windowsTotal: count(row.windows_total), windowsScored: count(row.windows_scored),
             scoreMax: unit(row.score_max) };
  });
  return { calls, nextBefore };
}

function parseAlert(v: unknown): StoredAlert {
  if (!isObject(v) || !validId(v.alert_id) || !ALERT_STATES.has(v.state as string)) throw new HistoryDataError();
  return {
    alertId: v.alert_id, direction: optString(v.direction), state: v.state as AlertState,
    raisedTs: optNumber(v.raised_ts), raisedWindow: optNumber(v.raised_window), raisedEma: unit(v.raised_ema),
    closedTs: optNumber(v.closed_ts),
  };
}

export function parseAlertPage(v: unknown): AlertPage {
  if (!isObject(v) || !Array.isArray(v.alerts)) throw new HistoryDataError();
  const cursor = v.next_cursor;
  if (!(cursor === null || (typeof cursor === 'string' && /^[A-Za-z0-9_-]{1,1024}$/.test(cursor)))) {
    throw new HistoryDataError();
  }
  const alerts = v.alerts.map(row => {
    if (!isObject(row) || !validId(row.call_id) || !finite(row.session) || !Number.isInteger(row.session)
        || row.session < 1) {
      throw new HistoryDataError();
    }
    return { ...parseAlert(row), callId: row.call_id, session: row.session, peakEma: unit(row.peak_ema) };
  });
  return { alerts, nextCursor: cursor };
}

function parseWindow(v: unknown): StoredWindow {
  if (!isObject(v) || typeof v.scored !== 'boolean') throw new HistoryDataError();
  return { direction: optString(v.direction), window: optNumber(v.window), ts: optNumber(v.ts), scored: v.scored,
           score: unit(v.score), ema: unit(v.ema), stride: optNumber(v.effective_stride_s) };
}

export function parseCallHistory(v: unknown): CallHistory {
  if (!isObject(v) || !validId(v.call_id) || !Array.isArray(v.sessions)) throw new HistoryDataError();
  const sessions = v.sessions.map(s => {
    if (!isObject(s) || !Array.isArray(s.alerts) || !Array.isArray(s.windows)) throw new HistoryDataError();
    return { ...parseSession(s), alerts: s.alerts.map(parseAlert), alertsTotal: count(s.alerts_total),
             alertsTruncated: s.alerts_truncated === true, windows: s.windows.map(parseWindow),
             windowsTotal: count(s.windows_total), windowsTruncated: s.windows_truncated === true };
  });
  return { callId: v.call_id, sessions, sessionsTotal: count(v.sessions_total),
           sessionsTruncated: v.sessions_truncated === true };
}

/** Pure: `datetime-local` text (seconds optional) read as UTC -> Unix seconds; '' -> null. */
export function parseUtcInput(text: string): number | null {
  const value = text.trim();
  if (!value) return null;
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,3}))?)?$/.exec(value);
  if (!match) throw new RangeError('Times must be written as YYYY-MM-DDTHH:MM (UTC).');
  const [, y, mo, d, h, mi, s = '0', ms = '0'] = match;
  if (+y < 1970) throw new RangeError('Times must be between 1970 and 3000.'); // Date.UTC maps 0-99 to 1900+
  const t = Date.UTC(+y, +mo - 1, +d, +h, +mi, +s, +ms.padEnd(3, '0'));
  const back = new Date(t);
  // Date.UTC rolls 2026-02-30 into March: refuse dates that do not exist.
  if (back.getUTCFullYear() !== +y || back.getUTCMonth() !== +mo - 1 || back.getUTCDate() !== +d || +h > 23 || +mi > 59
      || +s > 59) {
    throw new RangeError('That date or time does not exist.');
  }
  const seconds = t / 1000;
  if (seconds < 0 || seconds > MAX_TS) throw new RangeError('Times must be between 1970 and 3000.');
  return seconds;
}

/** Pure: form text -> filters the API accepts, or RangeError with a message to show. */
export function filtersFromForm(form: AlertForm): AlertFilters {
  const state = form.state === 'all' || ALERT_STATES.has(form.state) ? form.state as AlertStateFilter : null;
  if (state === null) throw new RangeError('Choose an alert state.');
  const since = parseUtcInput(form.since);
  const until = parseUtcInput(form.until);
  if (since !== null && until !== null && since >= until) throw new RangeError('"From" must be earlier than "to".');
  let minConfidence: number | null = null;
  if (form.minConfidence.trim()) {
    const n = Number(form.minConfidence);
    if (!Number.isFinite(n) || n < 0 || n > 1) throw new RangeError('Minimum confidence must be from 0 to 1.');
    minConfidence = n;
  }
  return { state, since, until, minConfidence };
}

/** Pure: the query string of an alert search (no leading `?`). */
export function alertQuery(filters: AlertFilters, options: { limit: number; cursor?: string | null; csv?: boolean }): string {
  const q = new URLSearchParams();
  if (filters.state !== 'all') q.set('state', filters.state);
  if (filters.since !== null) q.set('since', String(filters.since));
  if (filters.until !== null) q.set('until', String(filters.until));
  if (filters.minConfidence !== null) q.set('min_confidence', String(filters.minConfidence));
  q.set('limit', String(options.limit));
  if (options.cursor) q.set('cursor', options.cursor);
  if (options.csv) q.set('format', 'csv');
  return q.toString();
}

export function callsQuery(status: CallStatusFilter, limit: number, before: number | null): string {
  const q = new URLSearchParams();
  if (status !== 'all') q.set('status', status);
  q.set('limit', String(limit));
  if (before !== null) q.set('before', String(before));
  return q.toString();
}

const pad = (n: number) => String(n).padStart(2, '0');

/** Pure: Unix seconds as `YYYY-MM-DD HH:MM:SS UTC`; null or out of range -> '—'. */
export function formatUtc(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds) || seconds < 0 || seconds > MAX_TS) return '—';
  const d = new Date(Math.floor(seconds) * 1000);
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} `
    + `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())} UTC`;
}

/** Pure: appends a page to a list without exceeding MAX_LISTED and without repeating a
 * key. `capped` means a new row was left out because the list was full. */
export function appendBounded<T>(list: T[], page: T[], key: (item: T) => string): { items: T[]; capped: boolean } {
  const seen = new Set(list.map(key));
  const items = [...list];
  for (const item of page) {
    const k = key(item);
    if (seen.has(k)) continue;
    if (items.length >= MAX_LISTED) return { items, capped: true };
    seen.add(k);
    items.push(item);
  }
  return { items, capped: false };
}

export const alertKey = (a: AlertRow) => `${a.session}\u0000${a.alertId}`;
export const callKey = (c: CallRow) => String(c.session);

/** Pure: the message for a refused history read. Server detail is shown as text only. */
export function describeHistoryError(status: number, detail: unknown): string {
  const text = typeof detail === 'string' && detail.trim() ? detail.trim().slice(0, 200) : '';
  if (status === 401) return 'Your session has ended. Sign in again.';
  if (status === 503 && /not recorded in the audit log/i.test(text)) {
    return 'The export was not sent: it could not be recorded in the audit log. Try again shortly.';
  }
  if (status === 503 && /account store unavailable/i.test(text)) {
    return 'Your session could not be checked (the account store is unavailable). Try again shortly.';
  }
  if (status === 503) {
    if (/no call history store/i.test(text)) {
      return 'Call history is not available: this server has no history database configured.';
    }
    const reason = /\((unavailable|busy|timeout|schema|error)\)/.exec(text)?.[1];
    const why: Record<string, string> = {
      unavailable: 'the history database cannot be reached',
      busy: 'too many history reads are running; try again shortly',
      timeout: 'the read took too long; narrow the filters',
      schema: 'the history database is not migrated',
      error: 'the history database answered with an error',
    };
    return `Call history is not available: ${reason ? why[reason] : 'the service is unavailable'}.`;
  }
  if (status === 404) return 'No stored history for this call.';
  if (status === 422) return `The service refused the search${text ? `: ${text}` : '.'}`;
  return `The service answered with HTTP ${status}${text ? ` (${text})` : ''}.`;
}

/** One JSON read bounded by a time limit and the caller's signal. A caller abort is
 * rethrown unchanged; time-outs and network failures become ApiError(0). */
export async function readJson(url: string, signal: AbortSignal, timeoutMs = REQUEST_TIMEOUT_MS): Promise<unknown> {
  const limit = AbortSignal.timeout(timeoutMs);
  const combined = AbortSignal.any([signal, limit]);
  let response: Response;
  let body: unknown;
  try {
    response = await fetch(url, { signal: combined, headers: { Accept: 'application/json' } });
    reportSessionEnded(response.status);
    try {
      body = await response.json();
    } catch (error) {
      if (combined.aborted) throw error;
      body = undefined;
    }
  } catch (error) {
    if (signal.aborted) throw error;
    if (limit.aborted) throw new ApiError(0, 'The history service did not answer in time.');
    throw new ApiError(0, 'The history service could not be reached.');
  }
  if (!response.ok) {
    throw new ApiError(response.status, describeHistoryError(response.status, isObject(body) ? body.detail : undefined));
  }
  return body;
}

export async function fetchCalls(status: CallStatusFilter, before: number | null, signal: AbortSignal): Promise<CallPage> {
  return parseCallPage(await readJson(`v1/history/calls?${callsQuery(status, CALLS_PAGE, before)}`, signal));
}

export async function fetchAlerts(filters: AlertFilters, cursor: string | null, signal: AbortSignal): Promise<AlertPage> {
  return parseAlertPage(await readJson(`v1/alerts?${alertQuery(filters, { limit: ALERTS_PAGE, cursor })}`, signal));
}

export async function fetchCallHistory(callId: string, signal: AbortSignal): Promise<CallHistory> {
  const history = parseCallHistory(await readJson(`v1/calls/${encodeURIComponent(callId)}/history`, signal));
  if (history.callId !== callId) throw new HistoryDataError();
  return history;
}

export interface CsvExport { blob: Blob; truncated: boolean }

/** The CSV export of the newest CSV_EXPORT_LIMIT matches. `truncated` means the server
 * reported more matches (its `X-AVA-Next-Cursor` header). */
export async function fetchAlertCsv(filters: AlertFilters, signal: AbortSignal): Promise<CsvExport> {
  const limit = AbortSignal.timeout(CSV_TIMEOUT_MS);
  const combined = AbortSignal.any([signal, limit]);
  let response: Response;
  try {
    response = await fetch(`v1/alerts?${alertQuery(filters, { limit: CSV_EXPORT_LIMIT, csv: true })}`,
                           { signal: combined, headers: { Accept: 'text/csv' } });
    reportSessionEnded(response.status);
    if (!response.ok) {
      let detail: unknown;
      try { detail = (await response.json() as { detail?: unknown }).detail; } catch { detail = undefined; }
      throw new ApiError(response.status, describeHistoryError(response.status, detail));
    }
    if (!/^text\/csv\b/i.test(response.headers.get('content-type') ?? '')) {
      throw new ApiError(0, 'The service did not return a CSV file.');
    }
    const blob = await response.blob();
    return { blob, truncated: Boolean(response.headers.get('x-ava-next-cursor')) };
  } catch (error) {
    if (error instanceof ApiError || signal.aborted) throw error;
    if (limit.aborted) throw new ApiError(0, 'The export did not finish in time; narrow the filters.');
    throw new ApiError(0, 'The history service could not be reached.');
  }
}

/** Pure: a stored session's scored windows as timeline bars, in seconds since the session
 * started (each bar spans the scoring stride before the window's score time, as the live
 * timeline does). Unscored windows and windows without a time are left out. */
export function sessionTimeline(session: StoredSession): { index: number; t_start: number; t_end: number;
                                                             partial: boolean; p_synthetic: number; note: string }[] {
  // Without a stored start (implicit session), the first scored window's bar starts at 0.
  const first = session.windows.find(w => w.scored && w.score !== null && w.ts !== null);
  const firstStride = first?.stride !== null && first?.stride !== undefined && first.stride > 0 ? first.stride : 2;
  const origin = session.startedTs ?? (first ? (first.ts as number) - firstStride : 0);
  const bars = [];
  let position = 0;
  for (const w of session.windows) {
    position += 1;
    if (!w.scored || w.score === null || w.ts === null) continue;
    const stride = w.stride !== null && w.stride > 0 ? w.stride : 2;
    const end = Math.max(w.ts - origin, 0);
    bars.push({ index: position, t_start: Math.max(end - stride, 0), t_end: end, partial: false, p_synthetic: w.score,
                note: [w.direction, w.window === null ? '' : `window ${w.window}`].filter(Boolean).join(' ') });
  }
  return bars;
}
