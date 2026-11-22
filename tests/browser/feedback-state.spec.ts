// Claude-authored unit cases (19 November 2026, attempt 9) for backlog 7.3 / PRD FR-A7
// (analyst feedback UI, plan item 3.25): the pure rules in dashboard/src/feedback.ts.
// Authored by Claude (Claude Code), not GPT-6 Astra. No browser or server: Playwright's
// runner only transpiles TypeScript here. Runs in the `foundation` project (CI, Linux).
import { expect, test } from '@playwright/test';
import {
  describeFeedbackError, LABEL_TEXT, parseFeedbackList, parseFeedbackRecord, validAlertId, withNewLabel,
} from '../../dashboard/src/feedback';

const entry = (id: number, label = 'true_positive', extra: Record<string, unknown> = {}) => ({
  feedback_id: id, label, username: 'ana', ts: 1763500100 + id, alert_state: 'cleared', windows_stored: 2,
  windows_total: 2, ...extra,
});
const answer = (feedback: unknown[], extra: Record<string, unknown> = {}) => {
  const body: Record<string, unknown> = {
    alert_id: 'a-1', current: (feedback[0] as { label?: string } | undefined)?.label ?? null, feedback,
    feedback_total: feedback.length, ...extra,
  };
  // As the server computes it, unless a case sets it.
  if (!('feedback_truncated' in extra)) body.feedback_truncated = (body.feedback_total as number) > feedback.length;
  return body;
};

test('FS01 a label list is read only when it is the asked alert\'s, newest first and consistent', () => {
  expect(parseFeedbackList(answer([]), 'a-1')).toEqual({ alertId: 'a-1', current: null, feedback: [], total: 0,
                                                          truncated: false });
  const list = parseFeedbackList(answer([entry(7, 'false_positive'), entry(3)], { feedback_total: 5 }), 'a-1');
  expect(list.current).toBe('false_positive');
  expect(list.feedback.map(f => [f.feedbackId, f.label, f.alertState, f.windowsStored])).toEqual([
    [7, 'false_positive', 'cleared', 2], [3, 'true_positive', 'cleared', 2]]);
  expect([list.total, list.truncated]).toEqual([5, true]);
  const refused: [string, unknown][] = [
    ['another alert', answer([entry(1)], { alert_id: 'a-2' })],
    ['oldest first', answer([entry(1), entry(2)])],
    ['same ID twice', answer([entry(2), entry(2)])],
    ['current is not the newest', answer([entry(2), entry(1, 'false_positive')], { current: 'false_positive' })],
    ['current without labels', answer([], { current: 'true_positive' })],
    ['total below the rows', answer([entry(2), entry(1)], { feedback_total: 1 })],
    ['truncated flag disagrees (review N2)', answer([entry(2)], { feedback_total: 3, feedback_truncated: false })],
    ['truncated flag when complete', answer([entry(2)], { feedback_truncated: true })],
    ['unknown label', answer([entry(1, 'maybe')], { current: 'maybe' })],
    ['more windows than total', answer([entry(1, 'true_positive', { windows_stored: 3 })])],
    ['negative count', answer([entry(1, 'true_positive', { windows_total: -1 })])],
    ['fractional ID', answer([entry(1.5)])],
    ['no username', answer([entry(1, 'true_positive', { username: '' })])],
    ['NaN time', answer([entry(1, 'true_positive', { ts: Number.NaN })])],
    ['string time', answer([entry(1, 'true_positive', { ts: '1' })])],
    ['not an object', [entry(1)]],
    ['null', null],
  ];
  for (const [name, bad] of refused) {
    expect(() => parseFeedbackList(bad, 'a-1'), name).toThrow('The service returned feedback this page cannot read.');
  }
});

test('FS02 a stored label is read only for the alert and label that were sent', () => {
  const record = { ...entry(9, 'false_positive'), alert_id: 'a-1', windows_total: 3600, windows_stored: 3600 };
  expect(parseFeedbackRecord(record, 'a-1', 'false_positive')).toEqual({
    feedbackId: 9, label: 'false_positive', username: 'ana', ts: 1763500109, alertState: 'cleared',
    windowsStored: 3600, windowsTotal: 3600 });
  expect(() => parseFeedbackRecord(record, 'a-1', 'true_positive')).toThrow();
  expect(() => parseFeedbackRecord(record, 'a-2', 'false_positive')).toThrow();
  expect(() => parseFeedbackRecord({ ...record, feedback_id: 0 }, 'a-1', 'false_positive')).toThrow();
});

test('FS03 a new label becomes current, earlier ones stay; a repeat of the same record is not added twice', () => {
  const list = parseFeedbackList(answer([entry(4)]), 'a-1');
  const added = parseFeedbackRecord({ ...entry(6, 'false_positive'), alert_id: 'a-1' }, 'a-1', 'false_positive');
  const next = withNewLabel(list, added);
  expect([next.current, next.total, next.truncated, next.feedback.map(f => f.feedbackId)])
    .toEqual(['false_positive', 2, false, [6, 4]]);
  expect(withNewLabel(next, added)).toBe(next);
  // A list showing 100 of 120: the newest is added, the oldest shown drops out, still truncated.
  const many = parseFeedbackList(answer(Array.from({ length: 100 }, (_, i) => entry(200 - i)), { feedback_total: 120 }), 'a-1');
  const grown = withNewLabel(many, parseFeedbackRecord({ ...entry(300), alert_id: 'a-1' }, 'a-1', 'true_positive'));
  expect([grown.feedback.length, grown.feedback[0].feedbackId, grown.feedback[99].feedbackId, grown.total, grown.truncated])
    .toEqual([100, 300, 102, 121, true]);
});

test('FS04 every refusal has a plain message; store reasons are named; an uncertain write says so', () => {
  expect(describeFeedbackError(401, 'Login required', true)).toBe('Your session has ended. Sign in again.');
  expect(describeFeedbackError(403, 'Cross-origin request refused', true)).toMatch(/own address/);
  expect(describeFeedbackError(404, 'No stored alert with this ID', false)).toBe('This alert is not in the AVA database.');
  expect(describeFeedbackError(409, 'This alert has the most labels allowed', true))
    .toBe('This alert already has 20 labels, the most the server keeps; no further label was stored.');
  expect(describeFeedbackError(422, 'label must be one of', true)).toBe('The service refused the label: label must be one of');
  expect(describeFeedbackError(413, undefined, true)).toBe('The service refused the label.');
  expect(describeFeedbackError(503, 'Feedback not stored (busy)', true))
    .toBe('The label was not stored: the account store is busy; try again shortly.');
  expect(describeFeedbackError(503, 'Feedback not stored (schema)', true))
    .toBe('The label was not stored: the database is not migrated for feedback.');
  expect(describeFeedbackError(503, 'Feedback store unavailable (timeout)', false))
    .toBe('Labels could not be read: the database took too long; try again shortly.');
  // Review L1: `unavailable`/`error` (a connection lost at COMMIT) or an unknown reason may
  // follow a stored label; a read is simply not answered.
  expect(describeFeedbackError(503, 'Feedback not stored (unavailable)', true))
    .toBe('The label may or may not have been stored (the database cannot be reached): reload the labels to check.');
  expect(describeFeedbackError(503, 'Feedback not stored (error)', true)).toMatch(/^The label may or may not have been stored/);
  expect(describeFeedbackError(503, 'Feedback not stored (other)', true))
    .toBe('The label may or may not have been stored (the service is unavailable): reload the labels to check.');
  expect(describeFeedbackError(503, 'Feedback not stored (timeout)', true))
    .toBe('The label was not stored: the database took too long; try again shortly.');
  expect(describeFeedbackError(503, 'Feedback store unavailable (unavailable)', false))
    .toBe('Labels could not be read: the database cannot be reached.');
  expect(describeFeedbackError(503, 'Account store unavailable (unavailable)', true))
    .toBe('Your session could not be checked: the database cannot be reached.');
  expect(describeFeedbackError(503, 'Alert feedback needs local accounts and the call store', false))
    .toMatch(/Feedback is not available/);
  // Server detail is cut to 200 characters; any other 5xx on a write is an unknown outcome.
  expect(describeFeedbackError(500, 'x'.repeat(500), false)).toBe(`The service answered with HTTP 500 (${'x'.repeat(200)}).`);
  expect(describeFeedbackError(502, undefined, true))
    .toBe('The service answered with HTTP 502. The label may or may not have been stored: reload the labels to check.');
  expect(describeFeedbackError(418, undefined, true)).toBe('The service answered with HTTP 418.');
});

test('FS05 alert IDs follow the server rule; label texts say what each label means', () => {
  expect(validAlertId('a-1')).toBe(true);
  expect(validAlertId('ک'.repeat(128))).toBe(true);
  expect(validAlertId('😀'.repeat(128))).toBe(true); // 128 code points, 256 UTF-16 units
  for (const bad of ['', 'a'.repeat(129), 'a\u0000b', 7, null]) expect(validAlertId(bad), String(bad)).toBe(false);
  expect(LABEL_TEXT).toEqual({ true_positive: 'True positive (synthetic speech)',
                               false_positive: 'False positive (not synthetic speech)' });
});
