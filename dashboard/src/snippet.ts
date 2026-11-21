// Playback of an alert's retained audio snippet (PRD FR-U5 "playback of retained
// snippets if retention is on; an expired or missing snippet shows a clear message";
// backlog 7.3, 19 Nov 2026, attempt 10). One bounded call:
//   GET /v1/alerts/{id}/snippet   PCM16 mono WAV, decrypted in memory on the server
// It exists only on a server with local accounts (FR-U7) and a retention folder and key
// (services/api/__main__.py --retention-dir/--retention-key-file); every playback is an
// audited audio export, so the audio is fetched only when the user asks for it, never
// cached by the browser (no-store) and held as an in-memory blob URL that the page
// releases when the player goes away.

import { ApiError, reportSessionEnded } from './api';
import { validAlertId } from './feedback';

// The server decrypts at most 8 MB and waits up to 2 s for its audit slot.
export const SNIPPET_TIMEOUT_MS = 30000;
export const MAX_SNIPPET_BYTES = 8_000_000 + 44;

const isObject = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v);

/** Pure: the message for a refused playback. Server detail is shown as text only. */
export function describeSnippetError(status: number, detail: unknown): string {
  const text = typeof detail === 'string' && detail.trim() ? detail.trim().slice(0, 200) : '';
  if (status === 401) return 'Your session has ended. Sign in again.';
  if (status === 404) {
    return 'No snippet is kept for this alert. Audio is kept only when retention was on as the call started, '
      + 'only for the tapped caller side, and only until its retention period ends (an expired snippet reads as '
      + 'not kept once it is deleted); switching retention off deletes snippets at the next cleanup.';
  }
  if (status === 410) return 'This alert\'s snippet has expired: its retention period has ended, so it is no longer played.';
  if (status === 422) return 'This alert ID cannot be played.';
  if (status === 500 && /authenticated or read/i.test(text)) {
    return 'The stored snippet could not be decrypted or checked (a changed file or another key); it is not played.';
  }
  if (status === 503) {
    if (/not configured/i.test(text)) return 'Snippet playback is not set up on this server (no retention folder and key).';
    if (/audit log/i.test(text)) return 'The playback could not be recorded in the audit log, so no audio was sent. Try again shortly.';
    // Before the busy check: a session check can fail with "(busy)" too.
    if (/account store unavailable/i.test(text)) return 'Your session could not be checked. Try again shortly.';
    if (/too many snippet playbacks/i.test(text)) return 'Too many snippets are being played at once. Try again shortly.';
    return 'The snippet store could not be read. Try again shortly.';
  }
  return `The service answered with HTTP ${status}${text ? ` (${text})` : ''}.`;
}

/** Pure: (sample rate, seconds) of a PCM16 mono RIFF/WAVE body as the server writes it,
 * or null if it is not one. Checked before the browser is given the audio. */
export function wavInfo(bytes: Uint8Array): { sampleRate: number; seconds: number } | null {
  if (bytes.length < 44 || bytes.length > MAX_SNIPPET_BYTES) return null;
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const tag = (at: number) => String.fromCharCode(...bytes.subarray(at, at + 4));
  if (tag(0) !== 'RIFF' || tag(8) !== 'WAVE' || tag(12) !== 'fmt ' || tag(36) !== 'data') return null;
  const format = view.getUint16(20, true);
  const channels = view.getUint16(22, true);
  const sampleRate = view.getUint32(24, true);
  const bits = view.getUint16(34, true);
  const size = view.getUint32(40, true);
  if (format !== 1 || channels !== 1 || bits !== 16 || sampleRate < 8000 || sampleRate > 48000
      || size % 2 !== 0 || 44 + size !== bytes.length) {
    return null;
  }
  return { sampleRate, seconds: size / 2 / sampleRate };
}

export interface Snippet {
  blob: Blob;
  sampleRate: number;
  seconds: number;
}

/** One playback request (audited on the server); never retried. */
export async function fetchSnippet(alertId: string, signal: AbortSignal): Promise<Snippet> {
  if (!validAlertId(alertId)) throw new ApiError(0, 'This alert ID cannot be played.'); // nothing is sent
  const limit = AbortSignal.timeout(SNIPPET_TIMEOUT_MS);
  const combined = AbortSignal.any([signal, limit]);
  let response: Response;
  let bytes: Uint8Array;
  try {
    response = await fetch(`v1/alerts/${encodeURIComponent(alertId)}/snippet`,
                           { signal: combined, credentials: 'same-origin', cache: 'no-store', headers: { Accept: 'audio/wav' } });
    reportSessionEnded(response.status);
    if (response.status !== 200) {
      let body: unknown;
      try {
        body = await response.json();
      } catch (error) {
        if (combined.aborted) throw error;
        body = undefined;
      }
      throw new ApiError(response.status, describeSnippetError(response.status, isObject(body) ? body.detail : undefined));
    }
    bytes = new Uint8Array(await response.arrayBuffer());
  } catch (error) {
    if (error instanceof ApiError || signal.aborted) throw error;
    if (limit.aborted) throw new ApiError(0, 'The service did not answer in time.');
    throw new ApiError(0, 'The service could not be reached.');
  }
  const info = (response.headers.get('content-type') ?? '').split(';')[0].trim() === 'audio/wav' ? wavInfo(bytes) : null;
  if (info === null) throw new ApiError(0, 'The service returned audio this page cannot play.');
  return { blob: new Blob([bytes], { type: 'audio/wav' }), ...info };
}
