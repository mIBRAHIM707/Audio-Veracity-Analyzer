"""Read-back of retained alert snippets for playback (PRD FR-U5 "playback of retained
snippets if retention is on; an expired or missing snippet shows a clear message";
FR-O5; architecture §2.4, §6 "Audio minimization").

`SnippetReader(root, sealer, ttl_s=, clock=)` opens the sealed snippet the live service
stored for one alert (`snippets.SnippetStore.put`), authenticates it with the same
AES-256-GCM key (`snippets.open_snippet`) and returns it as a PCM16 mono WAV in memory.
Nothing decrypted is written to disk. Every failure is a `SnippetUnavailable` with one
fixed reason, never the file name, path or key:

- `missing`: no snippet for this alert ID (retention was off when the call started, the
  alert was on a direction that is not tapped, the write failed, or it was purged);
- `expired`: the snippet is older than the TTL but cleanup has not deleted it yet (the
  live service cleans every 10 minutes, so it can still be on disk; it is not served);
- `unreadable`: the file failed authentication or is malformed (another key, a changed
  file, a file copied under another alert's name, a creation time in the future);
- `error`: the file could not be read (an OS error).

The TTL here should equal the live service's `--retention-ttl-days`; a longer one never
serves a snippet the live service has already deleted, and a shorter one hides it early.
The creation time compared with the TTL is the authenticated one from inside the file.
"""
import io
import math
from pathlib import Path
import struct
import wave

import numpy as np

from services.retention.snippets import (AesGcmSealer, AES_GCM, CLOCK_SKEW_S, DEFAULT_TTL_S, REPO_ROOT,
                                         open_snippet)

REASONS = ('missing', 'expired', 'unreadable', 'error')
MAX_ALERT_ID_CHARS = 128  # the alerts table's rule (services/store/history.valid_call_id)


class SnippetUnavailable(Exception):
    def __init__(self, message, *, reason):
        super().__init__(message)
        if reason not in REASONS:
            raise ValueError(f'reason must be one of {REASONS}')
        self.reason = reason


def check_alert_id(alert_id):
    if not isinstance(alert_id, str) or not 1 <= len(alert_id) <= MAX_ALERT_ID_CHARS or '\x00' in alert_id:
        raise ValueError('alert ID must be 1-128 characters without NUL')
    return alert_id


def to_wav(pcm, sample_rate):
    """PCM16 little-endian mono RIFF/WAVE bytes."""
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(sample_rate)
        out.writeframes(np.asarray(pcm, dtype='<i2').tobytes())
    return buffer.getvalue()


class SnippetReader:
    """Read-only access to one retention folder. Holds the key in memory only."""

    def __init__(self, root, sealer, *, ttl_s=DEFAULT_TTL_S, clock, _insecure_test_sealer=False):
        root = Path(root).resolve()
        if root == REPO_ROOT or REPO_ROOT in root.parents:
            raise ValueError('the retention folder must be outside the repository')
        if type(sealer) is not AesGcmSealer and not _insecure_test_sealer:  # as SnippetStore: no subclasses
            raise ValueError(f'snippet playback needs the {AES_GCM} key (AesGcmSealer)')
        if isinstance(ttl_s, bool) or not isinstance(ttl_s, (int, float)) or not math.isfinite(ttl_s) or ttl_s <= 0:
            raise ValueError('ttl_s must be a positive finite number')
        self.root, self._sealer, self.ttl_s, self._clock = root, sealer, float(ttl_s), clock

    def read(self, alert_id):
        """{'wav': bytes, 'seconds', 'sample_rate', 'direction', 'call_id', 'created'} or
        SnippetUnavailable. ValueError for a malformed alert ID (before any file access)."""
        check_alert_id(alert_id)
        try:
            snippet = open_snippet(self.root, alert_id, self._sealer)
        except FileNotFoundError:
            raise SnippetUnavailable('no snippet is kept for this alert', reason='missing') from None
        except IsADirectoryError:
            raise SnippetUnavailable('the snippet is not a file', reason='unreadable') from None
        except OSError:
            raise SnippetUnavailable('the snippet could not be read', reason='error') from None
        except (ValueError, struct.error):
            raise SnippetUnavailable('the snippet failed authentication or is malformed',
                                     reason='unreadable') from None
        now = float(self._clock())
        created = snippet['created']
        if not math.isfinite(created) or created > now + CLOCK_SKEW_S:
            raise SnippetUnavailable('the snippet has an invalid creation time', reason='unreadable')
        if now - created >= self.ttl_s:  # the same comparison as SnippetStore.cleanup
            raise SnippetUnavailable('the snippet is past its retention period', reason='expired')
        rate, pcm = snippet['sample_rate'], snippet['pcm']
        if type(rate) is not int or not 8000 <= rate <= 48000:
            raise SnippetUnavailable('the snippet has an invalid sample rate', reason='unreadable')
        return {'wav': to_wav(pcm, rate), 'seconds': len(pcm) / rate, 'sample_rate': rate,
                'direction': snippet['direction'], 'call_id': snippet['call_id'], 'created': created}
