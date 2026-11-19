import { Fragment, useCallback, useEffect, useRef, useState } from 'react';
import type { FormEvent, ReactNode } from 'react';
import { ApiError } from './api';
import { SESSION_CHECK_EVENT, SESSION_ENDED_EVENT, checkLoginForm, fetchSession, login, logout,
         recheckDelayMs } from './auth';
import type { Session } from './auth';
import { SessionContext } from './session';

// PRD FR-U7 login in the dashboard (backlog 7.3, 19 Nov 2026). The gate asks the API who
// is signed in. A server without accounts (404) shows the pages unchanged and is not
// asked again; with accounts the pages are mounted only while a session is valid, so no
// page reads data or opens the event socket before sign-in or after sign-out or expiry.
//
// When the session is re-checked (review H1/M2): at once when a page gets a 401 or the
// live socket closes, when the tab regains focus, at the session's absolute end, and on
// a timer just after the server's idle limit. Nothing polls more often, so these checks
// never keep an idle session alive. (An open live board's socket is re-checked by the
// server every minute, which does count as activity: a watched board stays signed in.)

type GateState =
  | { phase: 'checking' }
  | { phase: 'failed'; message: string }
  | { phase: 'ready'; session: Session; notice: string | null; unverified: string | null };

const ENDED = 'Your session has ended. Sign in again.';

/** Same signed-in session (the time left, `endsInMs`, differs at every check). */
function sameUser(a: Session, b: Session): boolean {
  return a.kind === 'user' && b.kind === 'user' && a.username === b.username && a.role === b.role
    && a.expiresTs === b.expiresTs && a.idleS === b.idleS;
}

function LoginForm({ notice, onLogin }: { notice: string | null; onLogin: (session: Session) => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [refocus, setRefocus] = useState(0);
  const password = useRef<HTMLInputElement>(null);
  const controller = useRef<AbortController | null>(null);
  useEffect(() => () => controller.current?.abort(), []);
  useEffect(() => { document.title = 'AVA sign in'; }, []);
  // After a refusal, once the inputs are enabled again (review L1).
  useEffect(() => { if (refocus) password.current?.select(); }, [refocus]);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const username = String(form.get('username') ?? '').trim();
    const secret = String(form.get('password') ?? '');
    const refusal = checkLoginForm(username, secret);
    if (refusal) {
      setError(refusal);
      return;
    }
    controller.current?.abort();
    const own = new AbortController();
    controller.current = own;
    setBusy(true);
    setError(null);
    try {
      const session = await login(username, secret, own.signal);
      if (!own.signal.aborted) onLogin(session);
    } catch (failure) {
      if (own.signal.aborted) return;
      setBusy(false);
      setError(failure instanceof ApiError ? failure.message : 'Unexpected error while signing in.');
      setRefocus(n => n + 1);
    }
  };

  return (
    <section className="login" aria-labelledby="login-heading" data-testid="login">
      <h1 id="login-heading">Sign in to AVA</h1>
      {notice && <p role="status" className="notice" data-testid="login-notice">{notice}</p>}
      <form onSubmit={event => { void submit(event); }} noValidate>
        <label>Username
          <input name="username" autoComplete="username" autoCapitalize="none" spellCheck={false} required
                 maxLength={64} disabled={busy} autoFocus />
        </label>
        <label>Password
          <input name="password" type="password" autoComplete="current-password" required maxLength={256}
                 disabled={busy} ref={password} />
        </label>
        <button type="submit" disabled={busy}>{busy ? 'Signing in…' : 'Sign in'}</button>
      </form>
      {error && <p role="alert" className="error" data-testid="login-error">{error}</p>}
      <p className="muted">Accounts are created by the operator. Contact them to reset a password.</p>
    </section>
  );
}

export function AuthGate({ children }: { children: ReactNode }) {
  const [state, setState] = useState<GateState>({ phase: 'checking' });
  const [loggingOut, setLoggingOut] = useState(false);
  const [logoutError, setLogoutError] = useState<string | null>(null);
  const checking = useRef<AbortController | null>(null);

  /** Start a new check; any check still in flight is dropped (review M1). */
  const check = useCallback(async () => {
    checking.current?.abort();
    const own = new AbortController();
    checking.current = own;
    try {
      const session = await fetchSession(own.signal);
      if (own.signal.aborted) return;
      setState(previous => {
        if (previous.phase === 'ready' && (previous.session.kind === session.kind && session.kind !== 'user'
                                           || sameUser(previous.session, session))) {
          return previous.unverified ? { ...previous, unverified: null } : previous; // unchanged
        }
        const ended = previous.phase === 'ready' && previous.session.kind === 'user' && session.kind === 'anonymous';
        return { phase: 'ready', session, notice: ended ? ENDED : null, unverified: null };
      });
    } catch (failure) {
      if (own.signal.aborted) return;
      const message = failure instanceof ApiError ? failure.message : 'The session could not be checked.';
      // A failed first check shows the reason instead of any page. A failed re-check keeps
      // the page with a banner: the same store failure refuses every data read anyway.
      setState(previous => previous.phase === 'ready' ? { ...previous, unverified: message } : { phase: 'failed', message });
    }
  }, []);

  useEffect(() => {
    void check();
    return () => checking.current?.abort();
  }, [check]);

  const session = state.phase === 'ready' ? state.session : null;
  const kind = session?.kind;
  const expiresTs = session?.kind === 'user' ? session.expiresTs : null;
  const idleS = session?.kind === 'user' ? session.idleS : null;
  const endsInMs = session?.kind === 'user' ? session.endsInMs : null;

  // Event-driven checks. A server without accounts is re-checked only on a 401 (it was
  // restarted with accounts on), never on focus or socket closes (review L2, N-L1).
  useEffect(() => {
    if (kind === undefined) return undefined;
    const onEvent = () => { void check(); };
    const events = kind === 'off' ? [SESSION_ENDED_EVENT] : ['focus', SESSION_ENDED_EVENT, SESSION_CHECK_EVENT];
    for (const name of events) window.addEventListener(name, onEvent);
    return () => { for (const name of events) window.removeEventListener(name, onEvent); };
  }, [kind, check]);

  // Timers while signed in: just after the idle limit (re-check), and at the absolute
  // end (the pages go at once, whatever the server answers then). The end is timed from
  // the server's clock (`endsInMs`), never from this PC's date (re-review N-M1).
  useEffect(() => {
    if (expiresTs === null || idleS === null || endsInMs === null) return undefined;
    const idle = window.setInterval(() => { void check(); }, recheckDelayMs(idleS));
    const untilEnd = endsInMs;
    const end = window.setTimeout(() => {
      checking.current?.abort();
      setState({ phase: 'ready', session: { kind: 'anonymous' }, notice: ENDED, unverified: null });
    }, Math.min(untilEnd, 2 ** 31 - 1));
    return () => {
      window.clearInterval(idle);
      window.clearTimeout(end);
    };
  }, [expiresTs, idleS, endsInMs, check]);

  const signOut = async () => {
    setLoggingOut(true);
    setLogoutError(null);
    try {
      await logout(new AbortController().signal);
      checking.current?.abort(); // a check sent before the logout could still answer "signed in"
      setState({ phase: 'ready', session: { kind: 'anonymous' }, notice: 'You have signed out.', unverified: null });
    } catch (failure) {
      setLogoutError(failure instanceof ApiError ? failure.message : 'Logging out failed.');
    } finally {
      setLoggingOut(false);
    }
  };

  if (state.phase === 'checking') return <main><p role="status" data-testid="session-checking">Checking the session…</p></main>;
  if (state.phase === 'failed') {
    return (
      <main>
        <p role="alert" className="error" data-testid="session-error">{state.message}</p>
        <button type="button" onClick={() => { setState({ phase: 'checking' }); void check(); }}>Try again</button>
      </main>
    );
  }
  const current = state.session;
  if (current.kind === 'off') return <SessionContext.Provider value={current}>{children}</SessionContext.Provider>;
  if (current.kind === 'anonymous') {
    return (
      <main>
        <LoginForm notice={state.notice} onLogin={next => {
          checking.current?.abort(); // a check sent before the login could answer "signed out"
          setState({ phase: 'ready', session: next, notice: null, unverified: null });
        }} />
      </main>
    );
  }
  return (
    <>
      <div className="account" data-testid="account">
        <span>Signed in as <strong data-testid="account-user">{current.username}</strong>
          {' '}(<span data-testid="account-role">{current.role}</span>)</span>
        <button type="button" onClick={() => { void signOut(); }} disabled={loggingOut} data-testid="logout">
          {loggingOut ? 'Signing out…' : 'Sign out'}
        </button>
        {logoutError && <span role="alert" className="error" data-testid="logout-error">{logoutError}</span>}
      </div>
      {state.unverified && (
        <p role="alert" className="error account" data-testid="session-unverified">
          The session could not be verified: {state.unverified} What is shown may be out of date.
        </p>
      )}
      {/* A different user signing in from another tab remounts the pages (review L3). */}
      <Fragment key={current.username}>
        <SessionContext.Provider value={current}>{children}</SessionContext.Provider>
      </Fragment>
    </>
  );
}
