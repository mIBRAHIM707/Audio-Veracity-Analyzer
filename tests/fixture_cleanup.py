"""Bounded retry for fixture-directory cleanup on Windows (Claude-authored, 9 November 2026).

A Windows indexer or antivirus scan can briefly hold a just-written WAV, so
rmtree reports WinError 145 ("directory is not empty") after a test's assertions
have passed. This retries only fixture cleanup, a bounded number of times, and
re-raises the last error: a persistent failure still fails the test, and no error
is ignored (no ignore_cleanup_errors). Only sharing/not-empty errors are retried;
any other OSError is raised at once, and each retry is reported on stderr.
"""
import errno
import sys
import time

TRANSIENT = {5, 32, 145, errno.ENOTEMPTY}  # WinError access denied, sharing violation, not empty


def cleanup_with_retry(temporary, attempts=5, delay=0.2, sleep=time.sleep):
    for attempt in range(attempts):
        try:
            temporary.cleanup()
            return
        except OSError as error:
            code = getattr(error, 'winerror', None) or error.errno
            if code not in TRANSIENT or attempt == attempts - 1:
                raise
            print(f'fixture cleanup retry {attempt + 1}: OSError {code}', file=sys.stderr)
            sleep(delay * (attempt + 1))
