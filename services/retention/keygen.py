"""Create the FR-O5 snippet key once, for the Compose and k3s stacks (backlog 11.1;
PRD FR-O5 "AES-256-GCM at rest"; architecture §2.4 "Audio minimization").

    python -m services.retention.keygen --retention on --key-file /var/lib/ava/retention-key/snippet.key

A one-shot job run before the live service at every start (Compose service
`retention-key`, k3s init container `retention-key`), given the same `--retention`
setting as live. With `off` (the default) it does nothing at all and exits 0
(`skipped`): live does not open the key then, so a missing, unwritable or damaged key
volume must not stop the scorer for a feature that is switched off (review M1).

With `on`: if the key file is missing it writes 32 random bytes from the operating
system (`os.urandom`) to a new file readable by its owner only (0400 on POSIX), under a
temporary name that is fsynced and then hard-linked to the final name, so a reader
never sees a partial key and two jobs at once cannot both create one. If the file
exists it is checked against the rules the live service applies when loading it
(`AesGcmSealer.from_key_file`: a regular file, not a symlink, exactly 32 bytes, no
group/other permission bits on POSIX; checked on the opened descriptor, opened
non-blocking so a FIFO cannot hang the job) and kept as it is. Its own temporary files
older than 60 s (a job killed mid-write) are removed first.

It never overwrites, rotates or deletes a key: an existing file that breaks a rule is
refused (exit 2) and left for the operator, because replacing it would make every
stored snippet unreadable. The containing folder must already exist (the mounted
volume); a missing folder is a mount mistake and is refused, not created.

One JSON line goes to stdout: `{"event": "retention_key", "result":
"created"|"kept"|"skipped"}` or `{"event": "refused", "reason": ...}`. The key bytes and
the path are never printed. Standard library only (no numpy, no cryptography).
"""
import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time

KEY_BYTES = 32
TEMP_PREFIX = '.snippet-key-'
OWN_TEMP = re.compile(r'\.snippet-key-[A-Za-z0-9_]+\.tmp')
TEMP_GRACE_S = 60.0
REPO_ROOT = Path(__file__).resolve().parents[2]
EXIT_OK, EXIT_REFUSED = 0, 2


class KeyFileError(Exception):
    """The key file cannot be created or the existing one breaks a rule. The message
    names the rule, never the path or the key."""


def _check_existing(path):
    """Raise KeyFileError unless `path` is a usable key (the live service's rules);
    FileNotFoundError if there is none."""
    if stat.S_ISLNK(os.lstat(path).st_mode):
        raise KeyFileError('the existing key must be a regular file, not a symlink')
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0) \
        | getattr(os, 'O_NONBLOCK', 0)
    try:
        descriptor = os.open(path, flags)
    except IsADirectoryError:
        raise KeyFileError('the existing key must be a regular file') from None
    except PermissionError:
        if os.path.isdir(path):  # Windows refuses to open a folder this way
            raise KeyFileError('the existing key must be a regular file') from None
        raise
    try:
        info = os.fstat(descriptor)  # the file actually opened, not the name
        if not stat.S_ISREG(info.st_mode):  # before fdopen, which refuses a folder on Linux
            raise KeyFileError('the existing key must be a regular file')
    except BaseException:
        os.close(descriptor)
        raise
    with os.fdopen(descriptor, 'rb') as handle:
        if os.name == 'posix' and info.st_mode & 0o077:
            raise KeyFileError('the existing key file must not be readable by group or others')
        if len(handle.read(KEY_BYTES + 1)) != KEY_BYTES:
            raise KeyFileError(f'the existing key file must hold exactly {KEY_BYTES} raw bytes')


def _fsync_folder(folder):
    """Best effort: the link is made either way, and some mounts refuse a folder fsync
    (review L2), so a failure here must not turn a created key into a refusal."""
    if os.name != 'posix':  # Windows cannot open a folder for fsync
        return
    try:
        descriptor = os.open(folder, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        pass


def _unlink(path, attempts=20):
    """Remove a temporary name. On Windows another job may hold the same file open
    through the key's name (no delete sharing), so retry briefly (review L4)."""
    for attempt in range(attempts):
        try:
            os.unlink(path)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if os.name != 'nt' or attempt == attempts - 1:  # only Windows sharing clears
                return  # left for the next run's stale-temp cleanup
            time.sleep(0.05)
        except OSError:
            return


def _remove_stale_temps(folder, now):
    for entry in os.scandir(folder):
        try:
            if OWN_TEMP.fullmatch(entry.name) and entry.is_file(follow_symlinks=False) \
                    and now - entry.stat(follow_symlinks=False).st_mtime >= TEMP_GRACE_S:
                _unlink(entry.path)
        except OSError:
            pass


def ensure_key(key_file, *, urandom=os.urandom, clock=time.time):
    """Return 'created' or 'kept'; raise KeyFileError for a refusal. `urandom` and
    `clock` are replaceable in tests only."""
    path = Path(key_file)
    if not path.is_absolute():
        raise KeyFileError('the key file path must be absolute')
    folder = path.parent
    try:
        resolved = folder.resolve(strict=True)
    except OSError:
        raise KeyFileError('the key folder does not exist (is the volume mounted?)') from None
    if not resolved.is_dir():
        raise KeyFileError('the key folder is not a folder')
    if resolved == REPO_ROOT or REPO_ROOT in resolved.parents:
        raise KeyFileError('the key file must be outside the repository')
    target = resolved / path.name
    try:
        _remove_stale_temps(resolved, clock())
    except OSError:
        pass  # an unlistable folder fails below with a clear reason
    try:
        _check_existing(target)
        return 'kept'
    except FileNotFoundError:
        pass
    except OSError as error:
        raise KeyFileError(f'the existing key cannot be read: {type(error).__name__}') from None
    key = urandom(KEY_BYTES)
    if not isinstance(key, bytes) or len(key) != KEY_BYTES:
        raise KeyFileError('the random source did not return 32 bytes')
    try:
        descriptor, temporary = tempfile.mkstemp(dir=resolved, prefix=TEMP_PREFIX, suffix='.tmp')
    except OSError as error:
        raise KeyFileError(f'the key folder is not writable: {type(error).__name__}') from None
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            if os.name == 'posix':
                os.fchmod(handle.fileno(), 0o400)  # owner read only; this descriptor may still write
            handle.write(key)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, target)  # exclusive: fails if the name exists
        except FileExistsError:  # another job created it first: check that one instead
            try:
                _check_existing(target)
            except OSError as error:
                raise KeyFileError(f'the existing key cannot be read: {type(error).__name__}') from None
            return 'kept'
        _fsync_folder(resolved)
        return 'created'
    except KeyFileError:
        raise
    except OSError as error:
        raise KeyFileError(f'the key could not be written: {type(error).__name__}') from None
    finally:
        _unlink(temporary)


def main(argv=None, *, stdout=None, urandom=os.urandom):
    stdout = sys.stdout if stdout is None else stdout
    parser = argparse.ArgumentParser(prog='python -m services.retention.keygen',
                                     description='create the FR-O5 snippet key if retention is on and it is missing')
    parser.add_argument('--retention', choices=('off', 'on'), default='off',
                        help="live's retention setting; with off nothing is done (default off)")
    parser.add_argument('--key-file', required=True, help='absolute path of the key file in a mounted folder')
    args = parser.parse_args(argv)
    if args.retention == 'off':
        record, code = {'event': 'retention_key', 'result': 'skipped'}, EXIT_OK
    else:
        try:
            result = ensure_key(args.key_file, urandom=urandom)
        except KeyFileError as error:
            record, code = {'event': 'refused', 'reason': str(error)}, EXIT_REFUSED
        else:
            record, code = {'event': 'retention_key', 'result': result}, EXIT_OK
    stdout.write(json.dumps(record, sort_keys=True) + '\n')
    stdout.flush()
    return code


if __name__ == '__main__':
    sys.exit(main())
