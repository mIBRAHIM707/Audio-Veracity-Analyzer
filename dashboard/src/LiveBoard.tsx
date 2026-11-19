import { useCallback, useEffect, useReducer, useRef, useState } from 'react';
import { Timeline } from './Timeline';
import type { WindowScore } from './api';
import { reportSessionEnded, requestSessionCheck } from './api';
import {
  applyDetailAlerts, applyEnvelope, applySnapshot, connectionWasHealthy, EMPTY_BOARD, formatDuration, LiveDataError,
  orderedCalls, parseCallList, parseDetail, parseMessage, reconnectDelayMs,
} from './live';
import type { AlertItem, Board, CallCard, CallDetail, CallState, Envelope } from './live';

// Live call board, alert feed and call timeline (PRD FR-U1, FR-U2, FR-U3; backlog 7.1).
// Data: GET /v1/calls (snapshot) and WS /v1/events (FR-A4 envelopes), same origin only.

const THRESHOLD = 0.5; // window marking only; the scorer's own thresholds drive the states
const REQUEST_TIMEOUT_MS = 10000;
const SNAPSHOT_RETRY_MS = 5000;
const DETAIL_REFRESH_MS = 1000;
const MAX_DETAIL_FETCHES = 16; // open alerts loaded into the feed after a snapshot
const MAX_PENDING = 1000; // events queued while a snapshot loads
const MAX_TIMELINE_ROWS = 120; // newest rows in the table; the chart shows every window

const STATE_TEXT: Record<CallState, string> = {
  monitoring: 'Monitoring',
  suspicious: 'Suspicious',
  alert: 'Alert',
  no_speech: 'No speech',
  unmonitored: 'Unmonitored',
};

type Action =
  | { type: 'event'; envelope: Envelope }
  | { type: 'snapshot'; cards: CallCard[] }
  | { type: 'detail'; detail: CallDetail }
  | { type: 'invalid' };

function reducer(board: Board, action: Action): Board {
  switch (action.type) {
    case 'event': return applyEnvelope(board, action.envelope);
    case 'snapshot': return applySnapshot(board, action.cards);
    case 'detail': return applyDetailAlerts(board, action.detail);
    case 'invalid': return { ...board, invalidEvents: board.invalidEvents + 1 };
  }
}

type Connection =
  | { phase: 'connecting' }
  | { phase: 'open' }
  | { phase: 'closed'; reason: string; retryInMs: number };

async function getJson(url: string, signal: AbortSignal): Promise<{ status: number; body: unknown }> {
  const response = await fetch(url, { signal: AbortSignal.any([signal, AbortSignal.timeout(REQUEST_TIMEOUT_MS)]) });
  reportSessionEnded(response.status); // FR-U7: the sign-in gate re-checks the session
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    body = undefined;
  }
  return { status: response.status, body };
}

function eventsUrl(): string {
  const url = new URL('v1/events', window.location.href);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  url.hash = '';
  url.search = '';
  return url.toString();
}

/** A short tone through Web Audio (no audio file, works offline). Browsers allow sound
 * only after a user gesture, hence the explicit "Turn alert sound on" button. */
function useAlertSound() {
  const context = useRef<AudioContext | null>(null);
  const [enabled, setEnabled] = useState(false);
  const enable = useCallback(async () => {
    try {
      context.current ??= new AudioContext();
      await context.current.resume();
      setEnabled(true);
    } catch {
      setEnabled(false);
    }
  }, []);
  const disable = useCallback(() => setEnabled(false), []);
  const play = useCallback(() => {
    const ctx = context.current;
    if (!enabled || !ctx) return;
    // The browser may suspend an idle context; resuming is allowed once a gesture enabled it.
    if (ctx.state === 'suspended') void ctx.resume().catch(() => undefined);
    try {
      const oscillator = ctx.createOscillator();
      const gain = ctx.createGain();
      oscillator.frequency.value = 880;
      gain.gain.value = 0.15;
      oscillator.connect(gain).connect(ctx.destination);
      oscillator.start();
      oscillator.stop(ctx.currentTime + 0.25);
    } catch {
      /* sound is a courtesy; the visual notification is always shown */
    }
  }, [enabled]);
  useEffect(() => () => {
    void context.current?.close();
    context.current = null; // a remount must create a new context, not reuse a closed one
    setEnabled(false);
  }, []);
  return { enabled, enable, disable, play };
}

function Sparkline({ trend }: { trend: (number | null)[] }) {
  const W = 120, H = 28;
  if (trend.length === 0) return <span className="muted" data-testid="sparkline-empty">no scores yet</span>;
  const step = trend.length > 1 ? W / (trend.length - 1) : 0;
  const segments: string[] = [];
  let current: string[] = [];
  trend.forEach((s, i) => {
    if (s === null) {
      if (current.length) segments.push(current.join(' '));
      current = [];
    } else {
      current.push(`${(i * step).toFixed(1)},${(H - s * H).toFixed(1)}`);
    }
  });
  if (current.length) segments.push(current.join(' '));
  const scored = trend.filter((s): s is number => s !== null);
  return (
    <svg className="sparkline" viewBox={`0 0 ${W} ${H}`} width={W} height={H} role="img" data-testid="sparkline"
         data-points={trend.length}
         aria-label={`Last ${trend.length} window scores${scored.length ? `, newest ${scored[scored.length - 1].toFixed(2)}` : ''}`}>
      <line className="threshold" x1={0} x2={W} y1={H - THRESHOLD * H} y2={H - THRESHOLD * H} />
      {segments.map((points, i) => (
        points.includes(' ')
          ? <polyline key={i} points={points} fill="none" />
          : <circle key={i} cx={points.split(',')[0]} cy={points.split(',')[1]} r={1.5} />
      ))}
    </svg>
  );
}

function CallCardView({ call, nowTs, selected, onSelect }: { call: CallCard; nowTs: number; selected: boolean; onSelect: () => void }) {
  const end = call.endedTs ?? nowTs;
  const flags = [
    call.quality === 'low' ? 'Low audio quality' : null,
    call.degraded ? 'Degraded (reduced scoring rate)' : null,
    call.stale ? 'No events for 5 min' : null,
  ].filter((f): f is string => f !== null);
  return (
    <article className={`card state-${call.state}${selected ? ' selected' : ''}`} data-testid="call-card" data-call-id={call.callId}
             aria-label={`Call ${call.callId}`}>
      <header>
        <span className="filename" data-testid="call-id">{call.callId}</span>
        <span className={`state state-${call.state}`} data-testid="call-state">{STATE_TEXT[call.state]}</span>
      </header>
      <dl className="facts compact">
        <dt>Duration</dt><dd data-testid="call-duration">{formatDuration(end - call.startedTs)}</dd>
        <dt>Confidence</dt>
        <dd data-testid="call-confidence">{call.lastEma === null ? '—' : `${call.lastEma.toFixed(2)} (smoothed, uncalibrated)`}</dd>
        <dt>Alerts</dt><dd data-testid="call-alerts">{call.alertsOpen} open / {call.alertsTotal} total</dd>
        {call.status === 'ended' && (<><dt>Ended</dt><dd data-testid="call-end-reason">{call.endReason ?? 'unknown'}</dd></>)}
      </dl>
      <Sparkline trend={call.trend} />
      <p className="flags" data-testid="call-flags">
        {flags.length ? flags.map(f => <span key={f} className="flag">{f}</span>) : <span className="muted">No quality flags</span>}
      </p>
      <button type="button" onClick={onSelect} aria-pressed={selected}>{selected ? 'Hide timeline' : 'Show timeline'}</button>
    </article>
  );
}

function AlertFeed({ feed, onSelect }: { feed: AlertItem[]; onSelect: (callId: string) => void }) {
  return (
    <section aria-labelledby="feed-heading" className="feed">
      <h2 id="feed-heading">Alerts</h2>
      {feed.length === 0
        ? <p className="muted" data-testid="feed-empty">No alerts.</p>
        : (
          <ol data-testid="alert-feed">
            {feed.map(a => (
              <li key={a.key} data-testid="alert-item" className={`alert-${a.state}`} data-alert-id={a.alertId}>
                <strong>{a.state === 'open' ? 'Alert' : a.state === 'cleared' ? 'Cleared' : 'Open at call end'}</strong>{' '}
                <button type="button" className="link" onClick={() => onSelect(a.callId)}>
                  <span className="filename">{a.callId}</span>
                </button>
                {a.direction ? ` (${a.direction})` : ''}
                {a.raisedTs !== null ? ` at ${new Date(a.raisedTs * 1000).toLocaleTimeString()}` : ''}
                {a.ema !== null ? `, smoothed score ${a.ema.toFixed(2)}` : ''}
              </li>
            ))}
          </ol>
        )}
    </section>
  );
}

function detailWindows(detail: CallDetail): WindowScore[] {
  // Each scored update covers the newest stride of audio before it was published, so
  // a bar spans [t - stride, t] in seconds since the call started (event time).
  return detail.points
    .filter(p => p.score !== null)
    .map(p => {
      const end = Math.max(p.ts - detail.card.startedTs, 0);
      return { index: p.seq, t_start: Math.max(end - p.strideS, 0), t_end: end, partial: false,
               p_synthetic: p.score as number, note: p.direction ?? undefined };
    });
}

function CallTimeline({ callId, lastSeq, onDetail }: { callId: string; lastSeq: number; onDetail: (d: CallDetail) => void }) {
  const [detail, setDetail] = useState<CallDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const lastRead = useRef(-Infinity); // when the newest read started (ms)
  const onDetailRef = useRef(onDetail);
  onDetailRef.current = onDetail;
  // One read at a time, never aborted by a newer event (a slow service would otherwise
  // never finish one): a request made while a read is running is remembered and served
  // when it ends. Throttled, not debounced: at most one read per second, counted from
  // the previous read's start, so a call scoring faster still refreshes.
  const reader = useRef<{ request: () => void } | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let busy = false;
    let wanted = false;
    const read = async () => {
      timer = undefined;
      busy = true;
      wanted = false;
      lastRead.current = Date.now();
      try {
        const { status, body } = await getJson(`v1/calls/${encodeURIComponent(callId)}`, controller.signal);
        if (status === 404) throw new LiveDataError('This call is no longer known to the service.');
        if (status !== 200) throw new LiveDataError(`The service answered with HTTP ${status}.`);
        const parsed = parseDetail(body);
        if (controller.signal.aborted) return;
        setDetail(parsed);
        setError(null);
        onDetailRef.current(parsed);
      } catch (e) {
        if (controller.signal.aborted) return;
        setError(e instanceof LiveDataError ? e.message : 'The call details could not be loaded.');
      } finally {
        busy = false;
        if (wanted && !controller.signal.aborted) request();
      }
    };
    const request = () => {
      if (controller.signal.aborted || timer !== undefined) return; // a scheduled read will see the newest state
      if (busy) { wanted = true; return; }
      timer = setTimeout(() => void read(), Math.max(0, lastRead.current + DETAIL_REFRESH_MS - Date.now()));
    };
    reader.current = { request };
    return () => { reader.current = null; controller.abort(); clearTimeout(timer); };
  }, [callId]);
  useEffect(() => { reader.current?.request(); }, [callId, lastSeq]); // re-read when the call has new events
  const windows = detail ? detailWindows(detail) : [];
  const span = detail ? Math.max(...windows.map(w => w.t_end), 1) : 1;
  return (
    <section aria-labelledby="detail-heading" data-testid="call-detail" className="detail">
      <h2 id="detail-heading">Timeline for <span className="filename">{callId}</span></h2>
      {error && <p role="alert" className="error" data-testid="detail-error">{error}</p>}
      {detail && (
        <>
          <Timeline windows={windows} durationSeconds={span} threshold={THRESHOLD} maxRows={MAX_TIMELINE_ROWS}
                    indexHeading="Event"
                    caption="Bars: scored windows by the time their score was published (seconds since the call started); each covers the scoring stride (2 s normally). Note: direction." />
          {detail.buckets.length > 0 && (
            <p className="muted" data-testid="detail-buckets">
              {detail.buckets.length} older 10 s summaries (more than 30 minutes before the newest score)
              {detail.droppedBuckets ? `; ${detail.droppedBuckets} oldest no longer held in memory` : ''}.
            </p>
          )}
        </>
      )}
    </section>
  );
}

const CONNECTION_TEXT = {
  connecting: 'Connecting to live events…',
  open: 'Live',
};

export function LiveBoard() {
  const [board, dispatch] = useReducer(reducer, EMPTY_BOARD);
  const [connection, setConnection] = useState<Connection>({ phase: 'connecting' });
  const [snapshotError, setSnapshotError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [nowTs, setNowTs] = useState(() => Date.now() / 1000);
  const skew = useRef(0); // server ts minus local clock, from the newest envelope
  const sound = useAlertSound();
  const lastAlerts = useRef(0);
  // Undismissed new alerts: the newest and how many arrived since the last dismissal.
  const [banner, setBanner] = useState<{ item: AlertItem; count: number } | null>(null);

  useEffect(() => {
    const timer = setInterval(() => setNowTs(Date.now() / 1000 + skew.current), 1000);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    let stopped = false;
    let socket: WebSocket | null = null;
    let attempt = 0;
    let reconnectTimer: ReturnType<typeof setTimeout> | undefined;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    let generation = 0;
    let pending: Envelope[] | null = null; // buffered while a snapshot is loading
    let pendingOverflow = false;
    const controller = new AbortController();

    const resync = async () => {
      const mine = ++generation;
      pending ??= [];
      clearTimeout(retryTimer);
      try {
        const { status, body } = await getJson('v1/calls?status=all', controller.signal);
        if (status === 503) throw new LiveDataError('No live event source is connected to the service.');
        if (status !== 200) throw new LiveDataError(`The call list request failed (HTTP ${status}).`);
        const cards = parseCallList(body);
        if (stopped || mine !== generation) return;
        dispatch({ type: 'snapshot', cards });
        setSnapshotError(null);
        // Open alerts that predate the page, without a notification (bounded).
        const withAlerts = cards.filter(c => c.status === 'active' && c.alertsOpen > 0).slice(0, MAX_DETAIL_FETCHES);
        for (const card of withAlerts) {
          getJson(`v1/calls/${encodeURIComponent(card.callId)}`, controller.signal)
            .then(({ status: s, body: b }) => { if (s === 200 && !stopped) dispatch({ type: 'detail', detail: parseDetail(b) }); })
            .catch(() => { /* the card still shows its open alert count */ });
        }
      } catch (error) {
        if (stopped || mine !== generation) return;
        setSnapshotError(error instanceof LiveDataError ? error.message : 'The call list could not be loaded.');
        retryTimer = setTimeout(() => { if (socket?.readyState === WebSocket.OPEN) void resync(); }, SNAPSHOT_RETRY_MS);
      }
      if (stopped || mine !== generation) return;
      // Also after a failed read: queued events are newer than the board, so they are
      // applied (the retry above re-reads REST for anything lost).
      const queued = pending ?? [];
      pending = null;
      for (const envelope of queued) dispatch({ type: 'event', envelope });
      if (pendingOverflow) {
        pendingOverflow = false;
        void resync(); // events were dropped while queued: read the truth again
      }
    };

    const connect = () => {
      if (stopped) return;
      setConnection({ phase: 'connecting' });
      let ws: WebSocket;
      try {
        ws = new WebSocket(eventsUrl());
      } catch {
        scheduleReconnect('The live event address could not be opened.');
        return;
      }
      socket = ws;
      let openedAt: number | null = null;
      let received = false;
      ws.onopen = () => {
        openedAt = Date.now();
        setConnection({ phase: 'open' });
        void resync();
      };
      ws.onmessage = message => {
        received = true;
        const parsed = parseMessage(message.data);
        if (parsed.kind === 'gap') {
          void resync(); // events were lost: the snapshot is the truth again
        } else if (parsed.kind === 'invalid') {
          dispatch({ type: 'invalid' });
        } else {
          skew.current = parsed.envelope.ts - Date.now() / 1000;
          if (pending) {
            // Bounded: past the limit events are dropped and the snapshot is re-read
            // once this one has been applied.
            if (pending.length < MAX_PENDING) pending.push(parsed.envelope);
            else pendingOverflow = true;
          } else dispatch({ type: 'event', envelope: parsed.envelope });
        }
      };
      ws.onclose = event => {
        if (socket !== ws) return;
        socket = null;
        generation += 1; // a snapshot still loading belongs to the old connection
        pending = null;
        pendingOverflow = false; // the reconnect reads a fresh snapshot anyway
        if (connectionWasHealthy(openedAt, Date.now(), received)) attempt = 0;
        // Any close may mean the session ended (1008 from the server's re-check, 1006 for a
        // refused handshake): the sign-in gate re-checks it. Bounded by the back-off.
        requestSessionCheck();
        scheduleReconnect(event.reason || `Connection closed (code ${event.code}).`);
      };
    };

    const scheduleReconnect = (reason: string) => {
      if (stopped) return;
      const delay = reconnectDelayMs(attempt++, Math.random());
      setConnection({ phase: 'closed', reason, retryInMs: delay });
      reconnectTimer = setTimeout(connect, delay);
    };

    connect();
    return () => {
      stopped = true;
      controller.abort();
      clearTimeout(reconnectTimer);
      clearTimeout(retryTimer);
      if (socket) {
        const ws = socket;
        socket = null;
        ws.close();
      }
    };
  }, []);

  // FR-U2: sound and a visual notification for each new live alert.
  // Several alerts in one render (e.g. a drained queue) give one tone and a count; the
  // banner names the newest live alert even if it has since been cleared.
  const { play } = sound;
  useEffect(() => {
    if (board.liveAlerts > lastAlerts.current) {
      const added = board.liveAlerts - lastAlerts.current;
      lastAlerts.current = board.liveAlerts;
      const newest = board.feed.find(a => a.live) ?? null;
      setBanner(current => (newest ? { item: newest, count: added + (current?.count ?? 0) } : current));
      play();
    }
  }, [board.liveAlerts, board.feed, play]);

  const openAlerts = Object.values(board.calls).reduce((n, c) => n + (c.status === 'active' ? c.alertsOpen : 0), 0);
  useEffect(() => {
    document.title = openAlerts ? `(${openAlerts}) AVA live calls` : 'AVA live calls';
  }, [openAlerts]);

  const { active, ended } = orderedCalls(board);
  const select = (callId: string) => setSelected(current => (current === callId ? null : callId));
  const selectedCard = selected ? board.calls[selected] : undefined;

  return (
    <div className="live">
      <p className={`connection ${connection.phase}`} role="status" data-testid="connection">
        {connection.phase === 'closed'
          ? <>Live updates disconnected: {connection.reason} Retrying in {Math.round(connection.retryInMs / 1000)} s. The board may be out of date.</>
          : CONNECTION_TEXT[connection.phase]}
      </p>
      {snapshotError && <p role="alert" className="error" data-testid="snapshot-error">{snapshotError}</p>}
      {board.health && (
        <p className="badge" data-testid="health">
          System: {board.health.reason} at {new Date(board.health.ts * 1000).toLocaleTimeString()}
        </p>
      )}
      {banner && (
        <div role="alert" className="alert-banner" data-testid="alert-banner">
          {banner.count > 1 ? `${banner.count} new alerts; newest on call ` : 'New alert on call '}
          <span className="filename">{banner.item.callId}</span>{banner.item.direction ? ` (${banner.item.direction})` : ''}.
          <button type="button" onClick={() => setBanner(null)}>Dismiss</button>
        </div>
      )}
      <p className="sound">
        {sound.enabled
          ? <button type="button" onClick={sound.disable}>Turn alert sound off</button>
          : <button type="button" onClick={() => void sound.enable()}>Turn alert sound on</button>}
        <span className="muted"> Browsers play sound only after you allow it on this page.</span>
      </p>
      <div className="board-layout">
        <section aria-labelledby="active-heading">
          <h2 id="active-heading">Active calls ({active.length})</h2>
          {active.length === 0
            ? <p className="muted" data-testid="no-active">No active calls.</p>
            : <div className="cards" data-testid="active-calls">
                {active.map(c => <CallCardView key={c.callId} call={c} nowTs={nowTs} selected={selected === c.callId} onSelect={() => select(c.callId)} />)}
              </div>}
          {ended.length > 0 && (
            <>
              <h2>Recently ended</h2>
              <div className="cards" data-testid="ended-calls">
                {ended.map(c => <CallCardView key={c.callId} call={c} nowTs={nowTs} selected={selected === c.callId} onSelect={() => select(c.callId)} />)}
              </div>
            </>
          )}
        </section>
        <AlertFeed feed={board.feed} onSelect={select} />
      </div>
      {selected && (
        <CallTimeline key={selected} callId={selected} lastSeq={selectedCard?.lastSeq ?? 0}
                      onDetail={detail => dispatch({ type: 'detail', detail })} />
      )}
      {(board.invalidEvents > 0) && (
        <p className="muted" data-testid="invalid-events">{board.invalidEvents} unreadable event(s) were ignored.</p>
      )}
    </div>
  );
}
