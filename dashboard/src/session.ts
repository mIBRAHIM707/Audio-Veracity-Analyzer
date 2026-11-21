import { createContext } from 'react';
import type { Session } from './auth';

/** The session the pages run under (AuthGate provides it): `off` on a server without
 * accounts, otherwise the signed-in user. Pages use it only to show account-only controls
 * (FR-A7 feedback); the server still checks every request itself. Kept apart from auth.ts
 * so that module's pure tests do not load React. */
export const SessionContext = createContext<Session>({ kind: 'off' });
