// Analyst feedback on stored alerts (PRD FR-A7 UI, plan item 3.25; backlog 7.3, 19 Nov
// 2026, attempt 9). Pure parsing and messages, plus the two bounded calls:
//   GET  /v1/alerts/{id}/feedback   the alert's labels, newest first (`current` = newest)
//   POST /v1/alerts/{id}/feedback   {"label": "true_positive" | "false_positive"} -> 201
// Both exist only on a server with local accounts (FR-U7): every label names who gave it,
// and the server stores it with a copy of the alert's window scores and an audit row in
// one transaction (services/store/feedback.py). Labels are appended, never edited: a
// second label is the alert's new current label and the older ones stay as history.

import { ApiError, reportSessionEnded } from './api';
import { MAX_ID } from './history';

export type FeedbackLabel = 'true_positive' | 'false_positive';
export const FEEDBACK_LABELS: readonly FeedbackLabel[] = ['true_positive', 'false_positive'];
export const MAX_LABELS_PER_ALERT = 20; // services/store/feedback.py MAX_LABELS
export const MAX_FEEDBACK_LISTED = 100; // services/store/feedback.py MAX_LIST
// A label waits up to 2 s for the alert row and its account slot, then copies at most
// 3,600 windows; the store's own statement timeout is 10 s. 30 s leaves a margin.
export const FEEDBACK_TIMEOUT_MS = 30000;

export const LABEL_TEXT: Record<FeedbackLabel, string> = {
  true_positive: 'True positive (synthetic speech)',
  false_positive: 'False positive (not synthetic speech)',
};

export interface FeedbackEntry {
  feedbackId: number;
  label: FeedbackLabel;
  username: string;
  ts: number;
  /** The alert's state when it was labelled (`open`, `cleared` or `ended_open`). */
  alertState: string;
  windowsStored: number;
  windowsTotal: number;
}

export interface FeedbackList {
  alertId: string;
  current: FeedbackLabel | null;
  feedback: FeedbackEntry[];
  total: number;
  truncated: boolean;
}

const UNREADABLE = 'The service returned feedback this page cannot read.';

const isObject = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v);
const isLabel = (v: unknown): v is FeedbackLabel => v === 'true_positive' || v === 'false_positive';
const count = (v: unknown): v is number => typeof v === 'number' && Number.isInteger(v) && v >= 0;
/** 1-128 code points without NUL, as the server's alert ID rule. */
export const validAlertId = (v: unknown): v is string =>
  typeof v === 'string' && v.length > 0 && [...v].length <= MAX_ID && !v.includes('\u0000');

function unreadable(): ApiError {
  return new ApiError(0, UNREADABLE);
}

function parseEntry(v: unknown): FeedbackEntry {
  if (!isObject(v) || !count(v.feedback_id) || v.feedback_id < 1 || !isLabel(v.label)
      || typeof v.username !== 'string' || !v.username || typeof v.ts !== 'number' || !Number.isFinite(v.ts)
      || typeof v.alert_state !== 'string' || !count(v.windows_stored) || !count(v.windows_total)
      || v.windows_stored > v.windows_total) {
    throw unreadable();
  }
  return { feedbackId: v.feedback_id, label: v.label, username: v.username, ts: v.ts, alertState: v.alert_state,
           windowsStored: v.windows_stored, windowsTotal: v.windows_total };
}

/** Pure: a `GET .../feedback` answer for `alertId`, or ApiError(0) if it is not one. */
export function parseFeedbackList(v: unknown, alertId: string): FeedbackList {
  if (!isObject(v) || v.alert_id !== alertId || !Array.isArray(v.feedback) || !count(v.feedback_total)
      || !(v.current === null || isLabel(v.current))) {
    throw unreadable();
  }
  const feedback = v.feedback.map(parseEntry);
  // Newest first by ID; `current` is the newest label (none when there is no label).
  for (let i = 1; i < feedback.length; i += 1) if (feedback[i].feedbackId >= feedback[i - 1].feedbackId) throw unreadable();
  if ((feedback[0]?.label ?? null) !== v.current || feedback.length > v.feedback_total
      || v.feedback_truncated !== v.feedback_total > feedback.length) {
    throw unreadable();
  }
  return { alertId, current: v.current, feedback, total: v.feedback_total, truncated: v.feedback_total > feedback.length };
}

/** Pure: a `POST .../feedback` 201 answer for this alert and label. */
export function parseFeedbackRecord(v: unknown, alertId: string, label: FeedbackLabel): FeedbackEntry {
  if (!isObject(v) || v.alert_id !== alertId || v.label !== label) throw unreadable();
  return parseEntry(v);
}

/** Pure: a list with a just-stored label added as the newest (the server's answer). */
export function withNewLabel(list: FeedbackList, entry: FeedbackEntry): FeedbackList {
  if (list.feedback.some(f => f.feedbackId === entry.feedbackId)) return list;
  const feedback = [entry, ...list.feedback].sort((a, b) => b.feedbackId - a.feedbackId).slice(0, MAX_FEEDBACK_LISTED);
  const total = list.total + 1;
  return { ...list, current: feedback[0].label, feedback, total, truncated: total > feedback.length };
}

const STORE_REASONS: Record<string, string> = {
  busy: 'the account store is busy; try again shortly',
  unavailable: 'the database cannot be reached',
  timeout: 'the database took too long; try again shortly',
  schema: 'the database is not migrated for feedback',
  error: 'the database answered with an error',
};

/** Pure: the message for a refused feedback call. Server detail is shown as text only. */
export function describeFeedbackError(status: number, detail: unknown, writing: boolean): string {
  const text = typeof detail === 'string' && detail.trim() ? detail.trim().slice(0, 200) : '';
  const reason = /\((busy|unavailable|timeout|schema|error)\)/.exec(text)?.[1];
  const why = reason ? STORE_REASONS[reason] : 'the service is unavailable';
  if (status === 401) return 'Your session has ended. Sign in again.';
  if (status === 403) return 'The label was refused because it did not come from this dashboard\'s own address.';
  if (status === 404) return 'This alert is not in the AVA database.';
  if (status === 409) {
    return `This alert already has ${MAX_LABELS_PER_ALERT} labels, the most the server keeps; no further label was stored.`;
  }
  if (status === 413 || status === 422) return `The service refused the label${text ? `: ${text}` : '.'}`;
  if (status === 503) {
    if (/needs local accounts/i.test(text)) return 'Feedback is not available: this server has no accounts and call store.';
    if (/account store unavailable/i.test(text)) return `Your session could not be checked: ${why}.`;
    if (!writing) return `Labels could not be read: ${why}.`;
    // `unavailable`/`error` can come from a connection lost during COMMIT, so the label
    // may have been stored after all (review L1). busy/timeout/schema fail before it.
    if (reason === 'unavailable' || reason === 'error' || reason === undefined) {
      return `The label may or may not have been stored (${why}): reload the labels to check.`;
    }
    return `The label was not stored: ${why}.`;
  }
  // Any other server or proxy error on a write (500, 502, 504) may follow a stored label.
  const uncertain = writing && status >= 500 ? ' The label may or may not have been stored: reload the labels to check.' : '';
  return `The service answered with HTTP ${status}${text ? ` (${text})` : ''}.${uncertain}`;
}

function feedbackUrl(alertId: string): string {
  if (!validAlertId(alertId)) throw new ApiError(0, 'This alert ID cannot be labelled.'); // nothing is sent
  return `v1/alerts/${encodeURIComponent(alertId)}/feedback`;
}

async function call(url: string, init: RequestInit, signal: AbortSignal, writing: boolean)
  : Promise<{ status: number; body: unknown }> {
  const limit = AbortSignal.timeout(FEEDBACK_TIMEOUT_MS);
  const combined = AbortSignal.any([signal, limit]);
  let response: Response;
  let body: unknown;
  try {
    response = await fetch(url, { ...init, signal: combined, credentials: 'same-origin' });
    reportSessionEnded(response.status);
    try {
      body = await response.json();
    } catch (error) {
      if (combined.aborted) throw error;
      body = undefined;
    }
  } catch (error) {
    if (signal.aborted) throw error;
    // A write whose answer never arrived may still have been stored: say so (no retry).
    const unknown = writing ? ' The label may or may not have been stored: reload the labels to check.' : '';
    if (limit.aborted) throw new ApiError(0, `The service did not answer in time.${unknown}`);
    throw new ApiError(0, `The service could not be reached.${unknown}`);
  }
  return { status: response.status, body };
}

export async function fetchFeedback(alertId: string, signal: AbortSignal): Promise<FeedbackList> {
  const { status, body } = await call(feedbackUrl(alertId), { headers: { Accept: 'application/json' } }, signal, false);
  if (status !== 200) throw new ApiError(status, describeFeedbackError(status, isObject(body) ? body.detail : undefined, false));
  return parseFeedbackList(body, alertId);
}

/** One label, sent once: never retried, so a label cannot be stored twice by the page. */
export async function submitFeedback(alertId: string, label: FeedbackLabel, signal: AbortSignal): Promise<FeedbackEntry> {
  const { status, body } = await call(feedbackUrl(alertId), {
    method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify({ label }),
  }, signal, true);
  if (status !== 201) throw new ApiError(status, describeFeedbackError(status, isObject(body) ? body.detail : undefined, true));
  try {
    return parseFeedbackRecord(body, alertId, label);
  } catch {
    throw new ApiError(0, 'The label was stored, but the answer could not be read: reload the labels to see it.');
  }
}
