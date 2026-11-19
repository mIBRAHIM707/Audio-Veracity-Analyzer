import { useContext, useEffect, useRef, useState } from 'react';
import { ApiError } from './api';
import { fetchSnippet } from './snippet';
import { SessionContext } from './session';

// FR-U5 playback of one stored alert's retained snippet (backlog 7.3, 19 Nov 2026,
// attempt 10). Shown only to a signed-in user: the route exists only with accounts.
// Nothing is fetched until "Load snippet" is pressed, because each playback is an
// audited audio export. The audio stays in an in-memory blob URL, released when the
// player is closed, the call detail changes or the page goes.

type State = { phase: 'idle' } | { phase: 'loading' } | { phase: 'ready'; url: string; seconds: number; sampleRate: number }
  | { phase: 'error'; message: string };

export function AlertSnippet({ alertId }: { alertId: string }) {
  const session = useContext(SessionContext);
  const [state, setState] = useState<State>({ phase: 'idle' });
  const pending = useRef<AbortController | null>(null);
  const url = useRef<string | null>(null);

  const release = () => {
    pending.current?.abort();
    pending.current = null;
    if (url.current !== null) URL.revokeObjectURL(url.current);
    url.current = null;
  };
  useEffect(() => release, []);
  useEffect(() => {
    release();
    setState({ phase: 'idle' });
  }, [alertId]);

  if (session.kind !== 'user') return null;

  const load = async () => {
    if (pending.current !== null) return; // one request at a time (a ref: two clicks in one task)
    const own = new AbortController();
    pending.current = own;
    setState({ phase: 'loading' });
    try {
      const snippet = await fetchSnippet(alertId, own.signal);
      if (own.signal.aborted) return;
      const made = URL.createObjectURL(snippet.blob);
      url.current = made;
      setState({ phase: 'ready', url: made, seconds: snippet.seconds, sampleRate: snippet.sampleRate });
    } catch (error) {
      if (own.signal.aborted) return;
      setState({ phase: 'error', message: error instanceof ApiError ? error.message : 'Unexpected error while loading the snippet.' });
    } finally {
      if (pending.current === own) pending.current = null;
    }
  };

  const close = () => {
    release();
    setState({ phase: 'idle' });
  };

  return (
    <div className="snippet" data-testid="alert-snippet" data-alert-id={alertId}>
      {state.phase !== 'ready' && (
        <button type="button" className="link" data-testid="snippet-load" aria-disabled={state.phase === 'loading' || undefined}
                onClick={() => { void load(); }}>
          {state.phase === 'error' ? 'Try the snippet again' : 'Load retained audio'}
        </button>
      )}
      <div role="status" aria-live="polite">
        {state.phase === 'loading' && <p className="muted">Loading the snippet…</p>}
      </div>
      {state.phase === 'error' && <p role="alert" className="error" data-testid="snippet-error">{state.message}</p>}
      {state.phase === 'ready' && (
        <div data-testid="snippet-player">
          <audio controls preload="auto" src={state.url} aria-label={`Retained audio of alert ${alertId}`} />
          <p className="muted" data-testid="snippet-info">
            {state.seconds.toFixed(1)} s of retained audio at {(state.sampleRate / 1000).toFixed(0)} kHz, as the
            telephone system carried it, ending shortly after the alert. This playback was recorded in the audit log.
          </p>
          <button type="button" className="link" data-testid="snippet-close" onClick={close}>Close the player</button>
        </div>
      )}
    </div>
  );
}
