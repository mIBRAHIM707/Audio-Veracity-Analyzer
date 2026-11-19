// Claude-authored unit cases (19 November 2026, attempt 10) for backlog 7.3 / PRD FR-U5
// (playback of retained snippets; an expired or missing snippet shows a clear message):
// the pure rules in dashboard/src/snippet.ts. Authored by Claude (Claude Code), not
// GPT-6 Astra. No browser or server: Playwright's runner only transpiles TypeScript here.
// Runs in the `foundation` project (CI, Linux).
import { expect, test } from '@playwright/test';
import { describeSnippetError, MAX_SNIPPET_BYTES, wavInfo } from '../../dashboard/src/snippet';

/** A RIFF/WAVE header as Python's `wave` module writes it, then `samples` zero samples. */
function wav(samples: number, { rate = 8000, channels = 1, bits = 16, format = 1, extra = 0, dataTag = 'data' } = {}) {
  const size = samples * channels * (bits / 8);
  const bytes = new Uint8Array(44 + size + extra);
  const view = new DataView(bytes.buffer);
  const tag = (at: number, text: string) => [...text].forEach((c, i) => { bytes[at + i] = c.charCodeAt(0); });
  tag(0, 'RIFF'); view.setUint32(4, 36 + size, true); tag(8, 'WAVE'); tag(12, 'fmt ');
  view.setUint32(16, 16, true); view.setUint16(20, format, true); view.setUint16(22, channels, true);
  view.setUint32(24, rate, true); view.setUint32(28, rate * channels * bits / 8, true);
  view.setUint16(32, channels * bits / 8, true); view.setUint16(34, bits, true);
  tag(36, dataTag); view.setUint32(40, size, true);
  return bytes;
}

test('SS01 the server\'s PCM16 mono WAV is accepted with its rate and length', () => {
  expect(wavInfo(wav(16000))).toEqual({ sampleRate: 8000, seconds: 2 });
  expect(wavInfo(wav(8000, { rate: 16000 }))).toEqual({ sampleRate: 16000, seconds: 0.5 });
  expect(wavInfo(wav(0))).toEqual({ sampleRate: 8000, seconds: 0 });
  expect(wavInfo(wav(1, { rate: 48000 }))?.sampleRate).toBe(48000);
  // A view into a larger buffer (as a fetch body may be) reads from its own offset.
  const padded = new Uint8Array(10 + 44 + 2);
  padded.set(wav(1), 10);
  expect(wavInfo(padded.subarray(10))).toEqual({ sampleRate: 8000, seconds: 1 / 8000 });
});

test('SS02 anything else is refused before the browser gets it', () => {
  for (const bad of [
    new Uint8Array(0), new Uint8Array(43), wav(10, { channels: 2 }), wav(10, { bits: 8 }), wav(10, { format: 3 }),
    wav(10, { rate: 7999 }), wav(10, { rate: 48001 }), wav(10, { extra: 1 }), wav(10, { dataTag: 'LIST' }),
  ]) {
    expect(wavInfo(bad)).toBeNull();
  }
  const lying = wav(10);
  new DataView(lying.buffer).setUint32(40, 30, true); // the data size says more than the body has
  expect(wavInfo(lying)).toBeNull();
  const notRiff = wav(10);
  notRiff[0] = 'X'.charCodeAt(0);
  expect(wavInfo(notRiff)).toBeNull();
  expect(wavInfo(new Uint8Array(MAX_SNIPPET_BYTES + 1))).toBeNull();
});

test('SS03 missing and expired snippets each have their own clear message', () => {
  const missing = describeSnippetError(404, 'No snippet is kept for this alert');
  expect(missing).toMatch(/^No snippet is kept for this alert\./);
  expect(missing).toContain('retention was on as the call started');
  expect(describeSnippetError(410, 'The snippet for this alert has expired'))
    .toBe('This alert\'s snippet has expired: its retention period has ended, so it is no longer played.');
  expect(describeSnippetError(500, 'The stored snippet could not be authenticated or read'))
    .toBe('The stored snippet could not be decrypted or checked (a changed file or another key); it is not played.');
  expect(describeSnippetError(401, 'Login required')).toBe('Your session has ended. Sign in again.');
  expect(describeSnippetError(422, 'x')).toBe('This alert ID cannot be played.');
});

test('SS04 503 reasons: not set up, not audited, busy, session check, store error; others by status', () => {
  expect(describeSnippetError(503, 'Snippet playback is not configured on this server'))
    .toBe('Snippet playback is not set up on this server (no retention folder and key).');
  expect(describeSnippetError(503, 'Playback not recorded in the audit log (unavailable)'))
    .toBe('The playback could not be recorded in the audit log, so no audio was sent. Try again shortly.');
  expect(describeSnippetError(503, 'Too many snippet playbacks at once (busy); retry later'))
    .toBe('Too many snippets are being played at once. Try again shortly.');
  expect(describeSnippetError(503, 'Account store unavailable (unavailable)'))
    .toBe('Your session could not be checked. Try again shortly.');
  expect(describeSnippetError(503, 'Account store unavailable (busy)'))
    .toBe('Your session could not be checked. Try again shortly.');
  expect(describeSnippetError(503, 'Playback not recorded in the audit log (busy)'))
    .toBe('The playback could not be recorded in the audit log, so no audio was sent. Try again shortly.');
  expect(describeSnippetError(503, 'The snippet store could not be read (error)'))
    .toBe('The snippet store could not be read. Try again shortly.');
  expect(describeSnippetError(502, undefined)).toBe('The service answered with HTTP 502.');
  expect(describeSnippetError(500, 'Internal Server Error')).toBe('The service answered with HTTP 500 (Internal Server Error).');
  expect(describeSnippetError(500, '<b>' + 'x'.repeat(500))).toHaveLength('The service answered with HTTP 500 ().'.length + 200);
});
