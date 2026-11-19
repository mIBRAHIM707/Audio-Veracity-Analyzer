import { useContext, useEffect, useId, useRef, useState } from 'react';
import { ApiError } from './api';
import { FEEDBACK_LABELS, fetchFeedback, LABEL_TEXT, MAX_LABELS_PER_ALERT, submitFeedback, withNewLabel } from './feedback';
import type { FeedbackLabel, FeedbackList } from './feedback';
import { formatUtc } from './history';
import { SessionContext } from './session';

// FR-A7 feedback controls for one stored alert (backlog 7.3, 19 Nov 2026, attempt 9).
// Shown only to a signed-in user: a server without accounts has no feedback routes.
// Collapsed until asked, so opening a call with many alerts sends no feedback reads; the
// labels are read when the panel opens and each button sends exactly one label, never
// retried. Read and write share the account store's single connection on the server.

const STATE_TEXT: Record<string, string> = { open: 'open', cleared: 'cleared', ended_open: 'open at call end' };

const message = (error: unknown, fallback: string) => (error instanceof ApiError ? error.message : fallback);

type Read = { phase: 'loading' } | { phase: 'ready'; list: FeedbackList } | { phase: 'error'; message: string };
type Write = { phase: 'idle' } | { phase: 'saving'; label: FeedbackLabel } | { phase: 'saved'; label: FeedbackLabel }
  | { phase: 'error'; message: string };

export function AlertFeedback({ alertId, alertOpen }: { alertId: string; alertOpen: boolean }) {
  const session = useContext(SessionContext);
  const [expanded, setExpanded] = useState(false);
  // While a label is being stored the panel cannot be hidden, so its answer is always
  // shown (review M1). `aria-disabled`, not `disabled`: focus stays on the control (L2).
  const [saving, setSaving] = useState(false);
  const panelId = useId(); // alert IDs may hold spaces, which `aria-controls` cannot (L3)
  if (session.kind !== 'user') return null;
  return (
    <div className="feedback" data-testid="alert-feedback" data-alert-id={alertId}>
      <button type="button" className="link" aria-expanded={expanded} aria-controls={expanded ? panelId : undefined}
              aria-disabled={saving || undefined} data-testid="feedback-toggle"
              onClick={() => { if (!saving) setExpanded(e => !e); }}>
        {expanded ? 'Hide labels' : 'Label this alert'}
      </button>
      {expanded && <FeedbackPanel id={panelId} alertId={alertId} alertOpen={alertOpen} onSaving={setSaving} />}
    </div>
  );
}

function FeedbackPanel({ id, alertId, alertOpen, onSaving }: {
  id: string; alertId: string; alertOpen: boolean; onSaving: (saving: boolean) => void;
}) {
  const [read, setRead] = useState<Read>({ phase: 'loading' });
  const [write, setWrite] = useState<Write>({ phase: 'idle' });
  const [generation, setGeneration] = useState(0);
  const writing = useRef<object | null>(null);
  // A write is never aborted: if the page goes (another page, sign-out, session end) the
  // request still completes on the server and only the on-screen answer is dropped (M1).
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  useEffect(() => {
    const own = new AbortController();
    setRead({ phase: 'loading' });
    fetchFeedback(alertId, own.signal)
      .then(list => { if (!own.signal.aborted) setRead({ phase: 'ready', list }); })
      .catch(error => {
        if (!own.signal.aborted) setRead({ phase: 'error', message: message(error, 'Unexpected error while reading labels.') });
      });
    return () => own.abort();
  }, [alertId, generation]);

  const reload = () => {
    setWrite({ phase: 'idle' });
    setGeneration(g => g + 1);
  };

  const give = async (label: FeedbackLabel) => {
    // One label at a time. A ref, not state: two clicks handled before React re-renders
    // (same task) would both still see the old state and send two labels.
    if (writing.current !== null) return;
    const own = {};
    writing.current = own;
    setWrite({ phase: 'saving', label });
    onSaving(true);
    try {
      const entry = await submitFeedback(alertId, label, new AbortController().signal);
      if (!mounted.current) return;
      setRead(previous => previous.phase === 'ready' ? { phase: 'ready', list: withNewLabel(previous.list, entry) } : previous);
      setWrite({ phase: 'saved', label });
    } catch (error) {
      if (!mounted.current) return;
      setWrite({ phase: 'error', message: message(error, 'Unexpected error while storing the label.') });
    } finally {
      if (writing.current === own) writing.current = null;
      // Unmounted only together with the toggle (it cannot hide the panel while saving),
      // so skipping this then leaves no toggle stuck. Known limit (re-review): a panel
      // remounted before this answer does not know about it; labels are append-only,
      // capped and audited, so a second label is the worst case.
      if (mounted.current) onSaving(false);
    }
  };

  const list = read.phase === 'ready' ? read.list : null;
  const full = list !== null && list.total >= MAX_LABELS_PER_ALERT;
  const current = list?.feedback[0] ?? null;
  return (
    <div id={id} className="feedback-panel" role="group" aria-label={`Feedback on alert ${alertId}`}>
      {read.phase === 'loading' && <p className="muted" role="status">Loading labels…</p>}
      {read.phase === 'error' && (
        <p role="alert" className="error" data-testid="feedback-read-error">{read.message}</p>
      )}
      {list !== null && (
        <>
          <p data-testid="feedback-current">
            {current === null
              ? 'No label yet.'
              : <>Current label: <strong>{LABEL_TEXT[current.label]}</strong>, by {current.username} at {formatUtc(current.ts)}
                  {' '}(alert {STATE_TEXT[current.alertState] ?? 'state unknown'} when labelled)</>}
          </p>
          {alertOpen && (
            <p className="muted">
              The alert was still open when this call detail was loaded: a label stores the window scores received
              until it is given.
            </p>
          )}
          {full && (
            <p className="muted" data-testid="feedback-full">
              This alert has {MAX_LABELS_PER_ALERT} labels, the most the server keeps; no further label can be added.
            </p>
          )}
          <div className="feedback-actions">
            {FEEDBACK_LABELS.map(label => (
              <button key={label} type="button" data-testid={`feedback-${label}`} disabled={full}
                      aria-disabled={write.phase === 'saving' || undefined}
                      onClick={() => { void give(label); }}>
                Mark as {LABEL_TEXT[label].charAt(0).toLowerCase()}{LABEL_TEXT[label].slice(1)}
              </button>
            ))}
          </div>
          {/* Below the buttons, so a message never moves them under a second click. */}
          <div role="status" aria-live="polite" data-testid="feedback-status">
            {write.phase === 'saving' && <p className="muted">Storing “{LABEL_TEXT[write.label]}”…</p>}
            {write.phase === 'saved' && <p className="muted" data-testid="feedback-saved">Stored: {LABEL_TEXT[write.label]}.</p>}
          </div>
          {write.phase === 'error' && (
            <p role="alert" className="error" data-testid="feedback-error">{write.message}</p>
          )}
          {list.feedback.length > 1 && (
            <details>
              <summary>Earlier labels ({list.total - 1})</summary>
              <ol data-testid="feedback-history">
                {list.feedback.slice(1).map(f => (
                  <li key={f.feedbackId}>{LABEL_TEXT[f.label]}, by {f.username} at {formatUtc(f.ts)}</li>
                ))}
              </ol>
              {list.truncated && <p className="muted">Showing the newest {list.feedback.length} of {list.total} labels.</p>}
            </details>
          )}
          <p className="muted">
            Labels are kept with a copy of the alert&apos;s window scores and recorded in the audit log. A new label
            replaces the current one; earlier labels stay as history.
          </p>
        </>
      )}
      {(read.phase === 'error' || write.phase === 'error') && (
        <button type="button" onClick={reload} data-testid="feedback-reload">Reload labels</button>
      )}
    </div>
  );
}
