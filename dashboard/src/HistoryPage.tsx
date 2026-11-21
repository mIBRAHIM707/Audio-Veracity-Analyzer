import { useCallback, useEffect, useRef, useState } from 'react';
import type { FormEvent, ReactNode } from 'react';
import { ApiError } from './api';
import { AlertFeedback } from './AlertFeedback';
import { AlertSnippet } from './AlertSnippet';
import { Timeline } from './Timeline';
import {
  alertKey, appendBounded, callKey, CSV_EXPORT_LIMIT, fetchAlertCsv, fetchAlerts, fetchCallHistory, fetchCalls,
  filtersFromForm, formatUtc, HistoryDataError, MAX_LISTED, sessionTimeline,
} from './history';
import type {
  AlertFilters, AlertForm, AlertRow, CallHistory, CallRow, CallStatusFilter, StoredSession,
} from './history';

// History and search (PRD FR-U6; backlog 7.3): stored sessions, alert search with CSV
// export (FR-A6), and the stored detail of one call (FR-A3), all from PostgreSQL through
// the API. Read only. Times are UTC; scores and EMAs are uncalibrated.

const THRESHOLD = 0.5; // window marking only, as on the live timeline
const MAX_TIMELINE_ROWS = 120;
const ALL_ALERTS: AlertFilters = { state: 'all', since: null, until: null, minConfidence: null };

const ALERT_STATE_TEXT: Record<string, string> = { open: 'Open', cleared: 'Cleared', ended_open: 'Open at call end' };

function messageOf(error: unknown): string {
  if (error instanceof ApiError || error instanceof HistoryDataError || error instanceof RangeError) return error.message;
  return 'Unexpected error while reading call history.';
}

const fixed = (n: number | null, digits = 2) => (n === null ? '—' : n.toFixed(digits));

type Phase = 'idle' | 'loading' | 'ready' | 'error';

/** A list read in pages: the newest request wins; an older answer is dropped. */
function usePaged<T, Q>(read: (query: Q, after: string | number | null, signal: AbortSignal)
                         => Promise<{ items: T[]; next: string | number | null }>, key: (item: T) => string) {
  const [items, setItems] = useState<T[]>([]);
  const [next, setNext] = useState<string | number | null>(null);
  const [capped, setCapped] = useState(false);
  const [phase, setPhase] = useState<Phase>('idle');
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState<Q | null>(null);
  const controller = useRef<AbortController | null>(null);
  useEffect(() => () => controller.current?.abort(), []);

  const load = useCallback(async (q: Q, after: string | number | null, base: T[]) => {
    controller.current?.abort();
    const own = new AbortController();
    controller.current = own;
    setPhase('loading');
    setError(null);
    try {
      const page = await read(q, after, own.signal);
      if (own.signal.aborted) return;
      const merged = appendBounded(base, page.items, key);
      // Capped only when rows exist beyond the cap (dropped now, or a further page).
      const full = merged.capped || (merged.items.length >= MAX_LISTED && page.next !== null);
      setItems(merged.items);
      setCapped(full);
      setNext(full ? null : page.next);
      setQuery(q);
      setPhase('ready');
    } catch (e) {
      if (own.signal.aborted) return;
      setError(messageOf(e));
      setPhase('error');
    }
  }, [read, key]);

  const search = useCallback((q: Q) => {
    setItems([]);
    setNext(null);
    setCapped(false);
    setQuery(null);
    void load(q, null, []);
  }, [load]);
  const more = useCallback(() => {
    if (query !== null && next !== null) void load(query, next, items);
  }, [load, query, next, items]);
  return { items, next, capped, phase, error, query, search, more };
}

const readCalls = (status: CallStatusFilter, before: string | number | null, signal: AbortSignal) =>
  fetchCalls(status, before as number | null, signal).then(p => ({ items: p.calls, next: p.nextBefore }));
const readAlerts = (filters: AlertFilters, cursor: string | number | null, signal: AbortSignal) =>
  fetchAlerts(filters, cursor as string | null, signal).then(p => ({ items: p.alerts, next: p.nextCursor }));

function endText(row: { status: string; endReason: string | null; endInferred: boolean }): string {
  if (row.status === 'active') return 'Open (no end stored)';
  return `${row.endReason ?? 'unknown'}${row.endInferred ? ' (inferred: monitor lost)' : ''}`;
}

/** `notice` (the limitations text) is placed after the page heading. */
export function HistoryPage({ notice }: { notice?: ReactNode }) {
  useEffect(() => { document.title = 'AVA history'; }, []);
  const [selected, setSelected] = useState<string | null>(null);
  const opener = useRef<HTMLElement | null>(null);
  // The API's history reader serves one read at a time and refuses a read that waits
  // over 2 s (`busy`), so the alert search starts after the calls list has answered.
  const [callsSettled, setCallsSettled] = useState(false);
  const settled = useCallback(() => setCallsSettled(true), []);
  const open = (callId: string) => {
    opener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setSelected(callId);
  };
  const close = () => {
    setSelected(null);
    const back = opener.current;
    if (back?.isConnected) back.focus();
  };
  return (
    <>
      <header>
        <h1>AVA call history</h1>
        <p className="muted">
          Stored calls and alerts from the AVA database, read only. Times are UTC. Scores and smoothed scores
          (EMA) are uncalibrated: they are not probabilities you can rely on.
        </p>
      </header>
      {notice}
      <CallsSection onOpen={open} onSettled={settled} />
      <AlertsSection onOpen={open} start={callsSettled} />
      {selected !== null && <CallDetailSection key={selected} callId={selected} onClose={close} />}
    </>
  );
}

function CallsSection({ onOpen, onSettled }: { onOpen: (callId: string) => void; onSettled: () => void }) {
  const [status, setStatus] = useState<CallStatusFilter>('all');
  const list = usePaged<CallRow, CallStatusFilter>(readCalls, callKey);
  const { search, phase } = list;
  useEffect(() => { search(status); }, [search, status]);
  useEffect(() => { if (phase === 'ready' || phase === 'error') onSettled(); }, [phase, onSettled]);
  return (
    <section aria-labelledby="calls-heading" data-testid="history-calls">
      <h2 id="calls-heading">Stored calls</h2>
      <label>
        Show{' '}
        <select value={status} data-testid="calls-filter"
                onChange={e => setStatus(e.currentTarget.value as CallStatusFilter)}>
          <option value="all">All</option>
          <option value="active">Without a stored end</option>
          <option value="ended">Ended</option>
        </select>
      </label>{' '}
      <button type="button" onClick={() => search(status)}>Refresh</button>
      <ListStatus list={list} noun="stored calls" testId="calls" />
      {list.items.length > 0 && (
        <table data-testid="calls-table">
          <thead>
            <tr>
              <th scope="col">Call</th><th scope="col">Started</th><th scope="col">Ended</th>
              <th scope="col">End</th><th scope="col">Final state</th><th scope="col">Alerts (open / total)</th>
              <th scope="col">Windows (scored / stored)</th><th scope="col">Highest score (uncalibrated)</th>
            </tr>
          </thead>
          <tbody>
            {list.items.map(row => (
              <tr key={row.session} data-testid="call-row" data-call-id={row.callId} data-session={row.session}>
                <td><button type="button" className="link" onClick={() => onOpen(row.callId)}>{row.callId}</button></td>
                <td>{formatUtc(row.startedTs)}{row.startedSeen ? '' : ' (start not seen)'}</td>
                <td>{formatUtc(row.endedTs)}</td>
                <td data-testid="call-end">{endText(row)}</td>
                <td>{row.finalState ?? '—'}</td>
                <td data-testid="call-alerts">{row.alertsOpen} / {row.alertsTotal}</td>
                <td>{row.windowsScored} / {row.windowsTotal}</td>
                <td>{fixed(row.scoreMax, 3)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {list.next !== null && (
        <button type="button" onClick={list.more} disabled={list.phase === 'loading'} data-testid="calls-more">
          Load older calls
        </button>
      )}
    </section>
  );
}

/** Loading/empty/capped notes in a polite status region; an error in its own alert region. */
function ListStatus({ list, noun, testId }: {
  list: { phase: Phase; error: string | null; items: unknown[]; capped: boolean }; noun: string; testId: string;
}) {
  return (
    <>
      <div role="status" aria-live="polite" data-testid={`${testId}-status`}>
        {list.phase === 'loading' && <p className="muted">Loading {noun}…</p>}
        {list.phase === 'ready' && list.items.length === 0 && <p className="muted">No {noun} match.</p>}
        {list.capped && (
          <p className="muted" data-testid={`${testId}-capped`}>
            Showing the first {MAX_LISTED.toLocaleString('en')} {noun}; narrow the filters to see others.
          </p>
        )}
      </div>
      {list.phase === 'error' && <p role="alert" className="error" data-testid={`${testId}-error`}>{list.error}</p>}
    </>
  );
}

const EMPTY_FORM: AlertForm = { state: 'all', since: '', until: '', minConfidence: '' };
const INPUT_LABELS: Record<string, string> = {
  since: '"Raised from"', until: '"to"', minConfidence: '"Minimum confidence"',
};

function AlertsSection({ onOpen, start }: { onOpen: (callId: string) => void; start: boolean }) {
  const [form, setForm] = useState<AlertForm>(EMPTY_FORM);
  // Inputs the browser holds as unfinished or invalid (`validity.badInput`): their value
  // reads as '' and would otherwise be taken as "no filter" (review M1). Read at submit:
  // typing half a date changes no value, so no change event reports it.
  const inputs = useRef<Record<string, HTMLInputElement | null>>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [exporting, setExporting] = useState<'idle' | 'busy' | 'done' | 'error'>('idle');
  const [exportNote, setExportNote] = useState<string | null>(null);
  const exportController = useRef<AbortController | null>(null);
  const list = usePaged<AlertRow, AlertFilters>(readAlerts, alertKey);
  const { search } = list;
  const started = useRef(false);
  useEffect(() => {
    if (start && !started.current) {
      started.current = true;
      search(ALL_ALERTS);
    }
  }, [search, start]);
  useEffect(() => () => exportController.current?.abort(), []);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const bad = Object.keys(INPUT_LABELS).filter(name => inputs.current[name]?.validity.badInput);
    if (bad.length) {
      setFormError(`Complete or correct ${bad.map(name => INPUT_LABELS[name]).join(', ')}: the browser cannot read it.`);
      return;
    }
    try {
      const filters = filtersFromForm(form);
      setFormError(null);
      started.current = true; // the deferred default search must not replace this one (re-review N1)
      search(filters);
    } catch (e) {
      setFormError(messageOf(e));
    }
  };

  // Exports the filters of the results on screen (review L1), never unsearched form edits.
  const exportCsv = async (filters: AlertFilters) => {
    exportController.current?.abort();
    const own = new AbortController();
    exportController.current = own;
    setExporting('busy');
    setExportNote(null);
    try {
      const { blob, truncated } = await fetchAlertCsv(filters, own.signal);
      if (own.signal.aborted) return;
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = 'ava-alerts.csv';
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 60000);
      setExporting('done');
      setExportNote(truncated
        ? `Exported the newest ${CSV_EXPORT_LIMIT.toLocaleString('en')} alerts matching the search shown; more match. `
          + 'Narrow the time range to export the rest.'
        : 'Exported every alert matching the search shown.');
    } catch (e) {
      if (own.signal.aborted) return;
      setExporting('error');
      setExportNote(messageOf(e));
    }
  };

  const field = (name: keyof AlertForm) => ({
    value: form[name],
    onChange: (e: { currentTarget: { value: string } }) => {
      const value = e.currentTarget.value;
      setForm(f => ({ ...f, [name]: value }));
    },
  });
  const inputRef = (name: string) => (element: HTMLInputElement | null) => { inputs.current[name] = element; };

  return (
    <section aria-labelledby="alerts-heading" data-testid="history-alerts">
      <h2 id="alerts-heading">Alert search</h2>
      <form onSubmit={submit} className="filters" aria-label="Alert filters" noValidate>
        <label>State{' '}
          <select {...field('state')} data-testid="alert-state">
            <option value="all">All</option>
            <option value="open">Open</option>
            <option value="cleared">Cleared</option>
            <option value="ended_open">Open at call end</option>
          </select>
        </label>
        <label>Raised from (UTC){' '}
          <input type="datetime-local" step={1} {...field('since')} ref={inputRef('since')} data-testid="alert-since" />
        </label>
        <label>to, not including (UTC){' '}
          <input type="datetime-local" step={1} {...field('until')} ref={inputRef('until')} data-testid="alert-until" />
        </label>
        <label>Minimum confidence (peak EMA, 0–1){' '}
          <input type="number" min={0} max={1} step={0.01} inputMode="decimal" {...field('minConfidence')} ref={inputRef('minConfidence')}
                 data-testid="alert-min" />
        </label>
        {/* Never disabled: a new search replaces one still running (its answer is dropped). */}
        <button type="submit">Search</button>
        <button type="button" data-testid="alert-export"
                disabled={exporting === 'busy' || list.query === null}
                onClick={() => { if (list.query !== null) void exportCsv(list.query); }}>
          Export these results as CSV
        </button>
      </form>
      <p className="muted">
        Confidence is the highest smoothed score (EMA) the alert reached between being raised and closed; it is
        uncalibrated, and an open alert&apos;s value can still rise. The export uses the search shown below.
      </p>
      {formError && <p role="alert" className="error" data-testid="alert-form-error">{formError}</p>}
      <div role="status" aria-live="polite" data-testid="alert-export-status">
        {exporting === 'busy' && <p className="muted">Preparing the CSV export…</p>}
        {exporting === 'done' && <p className="muted">{exportNote}</p>}
      </div>
      {exporting === 'error' && <p role="alert" className="error" data-testid="alert-export-error">{exportNote}</p>}
      <ListStatus list={list} noun="alerts" testId="alerts" />
      {list.items.length > 0 && (
        <table data-testid="alerts-table">
          <thead>
            <tr>
              <th scope="col">Raised</th><th scope="col">Call</th><th scope="col">Direction</th>
              <th scope="col">State</th><th scope="col">Peak EMA</th><th scope="col">EMA at raise</th>
              <th scope="col">Closed</th>
            </tr>
          </thead>
          <tbody>
            {list.items.map(a => (
              <tr key={alertKey(a)} data-testid="alert-row" data-alert-id={a.alertId} data-call-id={a.callId}>
                <td>{formatUtc(a.raisedTs)}</td>
                <td><button type="button" className="link" onClick={() => onOpen(a.callId)}>{a.callId}</button></td>
                <td>{a.direction ?? '—'}</td>
                <td data-testid="alert-state-text">{ALERT_STATE_TEXT[a.state]}</td>
                <td data-testid="alert-peak">{fixed(a.peakEma)}</td>
                <td>{fixed(a.raisedEma)}</td>
                <td>{a.state === 'open' ? '—' : formatUtc(a.closedTs)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {list.next !== null && (
        <button type="button" onClick={list.more} disabled={list.phase === 'loading'} data-testid="alerts-more">
          Load more alerts
        </button>
      )}
    </section>
  );
}

function CallDetailSection({ callId, onClose }: { callId: string; onClose: () => void }) {
  const [state, setState] = useState<{ phase: 'loading' } | { phase: 'ready'; history: CallHistory }
    | { phase: 'error'; message: string }>({ phase: 'loading' });
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    const own = new AbortController();
    fetchCallHistory(callId, own.signal)
      .then(history => { if (!own.signal.aborted) setState({ phase: 'ready', history }); })
      .catch(e => { if (!own.signal.aborted) setState({ phase: 'error', message: messageOf(e) }); });
    heading.current?.focus();
    return () => own.abort();
  }, [callId]);
  return (
    <section aria-labelledby="detail-heading" className="detail" data-testid="history-detail" data-call-id={callId}>
      <h2 id="detail-heading" tabIndex={-1} ref={heading}>Stored history of <span className="filename">{callId}</span></h2>
      <button type="button" onClick={onClose}>Close</button>
      {state.phase === 'loading' && <p className="muted" role="status">Loading the stored history…</p>}
      {state.phase === 'error' && <p role="alert" className="error" data-testid="detail-error">{state.message}</p>}
      {state.phase === 'ready' && (
        <>
          {state.history.sessionsTruncated && (
            <p className="muted" data-testid="sessions-truncated">
              Showing the newest {state.history.sessions.length} of {state.history.sessionsTotal} sessions.
            </p>
          )}
          {state.history.sessions.length > 1 && (
            <p className="muted">
              This call ID has {state.history.sessionsTotal} stored monitoring sessions (for example after AVA
              restarted during the call); the newest is first.
            </p>
          )}
          {state.history.sessions.map(s => <SessionView key={s.session} session={s} />)}
        </>
      )}
    </section>
  );
}

function SessionView({ session }: { session: StoredSession }) {
  const bars = sessionTimeline(session);
  const duration = session.startedTs !== null && (session.endedTs ?? session.lastEventTs) !== null
    ? Math.max((session.endedTs ?? session.lastEventTs ?? 0) - session.startedTs, 0) : 0;
  return (
    <article className="session" data-testid="session" data-session={session.session}>
      <h3>Session {session.session}</h3>
      <dl className="facts compact">
        <dt>Started</dt><dd>{formatUtc(session.startedTs)}{session.startedSeen ? '' : ' (start not seen)'}</dd>
        <dt>Ended</dt><dd>{formatUtc(session.endedTs)}</dd>
        <dt>End</dt><dd data-testid="session-end">{endText(session)}</dd>
        <dt>Final state</dt><dd>{session.finalState ?? session.state ?? '—'}</dd>
        <dt>Speech</dt><dd>{session.speechSeconds === null ? '—' : `${session.speechSeconds.toFixed(1)} s`}</dd>
        <dt>Verdict</dt><dd data-testid="session-verdict">{session.verdict ?? '—'}</dd>
        <dt>Codec detected</dt>
        <dd data-testid="session-codec">{session.codec ?? 'Not detected (not recorded for this session)'}</dd>
      </dl>
      <h4>Alerts</h4>
      {session.alerts.length === 0
        ? <p className="muted">No alert was raised.</p>
        : (
          <ul data-testid="session-alerts">
            {session.alerts.map(a => (
              <li key={a.alertId} data-alert-id={a.alertId}>
                {a.alertId} ({a.direction ?? 'call'}): {ALERT_STATE_TEXT[a.state]}, raised {formatUtc(a.raisedTs)} at
                EMA {fixed(a.raisedEma)}{a.state === 'open' ? '' : `, closed ${formatUtc(a.closedTs)}`}
                <AlertSnippet alertId={a.alertId} />
                <AlertFeedback alertId={a.alertId} alertOpen={a.state === 'open'} />
              </li>
            ))}
          </ul>
        )}
      {session.alertsTruncated && (
        <p className="muted">Showing {session.alerts.length} of {session.alertsTotal} alerts.</p>
      )}
      <h4>Stored window scores</h4>
      {session.windowsTruncated && (
        <p className="muted" data-testid="windows-truncated">
          Showing {session.windows.length} of {session.windowsTotal} stored windows (the oldest).
        </p>
      )}
      <Timeline windows={bars} durationSeconds={duration} threshold={THRESHOLD} maxRows={MAX_TIMELINE_ROWS}
                indexHeading="Row" scoreHeading="Score (uncalibrated)" markText={`at or above ${THRESHOLD}`}
                ariaLabel="Per-window detector score (uncalibrated, not a probability)"
                caption="Bars: scored windows, each spanning the scoring stride before its score time, in seconds since the session started. Unscored windows are left out. Scores are uncalibrated." />
    </article>
  );
}
