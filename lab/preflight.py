"""Docker/WSL preflight for the telephony lab (backlog 6.1; PRD FR-T1 lab; architecture
§2.1 Asterisk tap). Read-only: it runs `docker version`, `docker info`, `docker image
inspect` and `docker ps` (resolved `docker` executable, 20 s timeout each),
`ffmpeg -version`, reads free disk space and the lab test environment, and reports. It
never pulls, builds, starts or removes anything.

    uv run --no-project --python 3.11 python -m lab.preflight [--image TAG]
        [--min-free-gib 10] [--json]

The lab test environment is `.local/lab-test.env` under the process environment; it
supplies AVA_TEST_DOCKER_LAB, AVA_LAB_IMAGE and AVA_FFMPEG. That shell variables win
over the file is an assumption about `uv run --env-file` (dotenv convention), not
verified here. No value is printed except the image tag; DOCKER_HOST/DOCKER_CONTEXT in
the file would reach the lab tests but not these docker calls.

Each check is `pass`, `warn` (the lab can run, something needs attention), `fail` (the
lab tests cannot run) or `skip` (an earlier failure makes it meaningless); the same
eight names appear on every run. Exit status 0 without failures, 1 with any failure, 2
for usage errors. Only selected fields are reported (versions, OS type, CPU and memory,
names of leftover lab containers, free space); `docker info` proxies, registries and
labels are never printed.

The checks and their thresholds:
- `docker_cli`: the `docker` command is on PATH.
- `docker_engine`: `docker version` answers within the timeout with a server part and
  OSType `linux` (the lab image is a Linux image; Windows-containers mode fails).
- `docker_backend`: on Windows the engine should be Docker Desktop on WSL 2 (kernel
  name contains `WSL2`, else `warn`); fewer than 2 CPUs or 4 GiB for the engine is a
  `warn` (the lab image, SIPp and the ARI tests were run with 8 CPUs / 7.6 GiB).
- `lab_image`: `--image`, else AVA_LAB_IMAGE, else `ava-lab-asterisk:dev` exists
  locally (the tests use `--pull never`); the fix is the `docker build` command.
- `leftover_containers`: containers labelled `ava-lab-test=1` from an interrupted run
  (`warn`; this script removes nothing).
- `disk_free`: free space (GiB) on the repository drive and on the temporary folder
  that holds the harness's I/O folders, `warn` below `--min-free-gib` on either.
  Docker Desktop's own disk image is not measured.
- `lab_env`: `.local/lab-test.env` exists and the lab test environment has
  AVA_TEST_DOCKER_LAB=1 (else `warn`: the lab tests skip).
- `ffmpeg`: the ffmpeg the GSM lab cases use (AVA_FFMPEG, else the winget install of
  the pin) runs, reports the pinned version by the same exact-token rule as
  `data/prepare/ffmpeg_codecs.Ffmpeg` and lists the encoders and decoders that wrapper
  requires. Absent: `warn` (those cases skip); present but broken, another version or
  missing a codec: `warn` (those cases fail).
"""
import argparse
import glob
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

DEFAULT_IMAGE = 'ava-lab-asterisk:dev'
LAB_LABEL = 'ava-lab-test=1'
MIN_CPUS = 2
MIN_MEMORY_BYTES = 4 * 1024 ** 3
DEFAULT_MIN_FREE_GIB = 10.0
TIMEOUT_S = 20
STATUSES = ('pass', 'warn', 'fail', 'skip')
REPO = Path(__file__).resolve().parents[1]
FFMPEG_VERSION = '9.0.2'  # data/prepare/ffmpeg_codecs.py PINNED_VERSION, kept import-free here
# Every encoder/decoder `ffmpeg_codecs.Ffmpeg` requires before it accepts a binary (the
# GSM lab cases construct it); a unit test ties both constants to that module.
FFMPEG_CODECS = ('libgsm', 'libopencore_amrnb', 'libopus')


def run_command(command, timeout=TIMEOUT_S):
    """(returncode, stdout, stderr) of a command given as a list with a resolved
    executable; a missing program or a timeout is returncode None. Output is decoded
    as UTF-8 with replacement (review L1: the Windows code page could not decode some
    docker output)."""
    try:
        done = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', errors='replace',
                              timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        return None, '', type(error).__name__
    return done.returncode, done.stdout, done.stderr


def read_env_file(path):
    """KEY=VALUE lines of a dotenv-style file (blank lines and # comments ignored,
    surrounding quotes removed). Values are used, never printed."""
    values = {}
    for line in Path(path).read_text(encoding='utf-8', errors='replace').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        key, value = key.strip(), value.strip()
        if key.startswith('export '):
            key = key[len('export '):].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
            value = value[1:-1]
        elif ' #' in value:  # review N1: an unquoted inline comment is not part of the value
            value = value.split(' #', 1)[0].rstrip()
        if key:
            values[key] = value
    return values


def find_ffmpeg(env):
    """The ffmpeg the lab tests use (tests/unit/test_ffmpeg_codecs.real_ffmpeg_path):
    `AVA_FFMPEG` as given (even if missing), else the winget install of the pin."""
    path = env.get('AVA_FFMPEG')
    if path:
        return path
    pattern = os.path.join(env.get('LOCALAPPDATA', ''), 'Microsoft', 'WinGet', 'Packages', 'Gyan.FFmpeg*',
                           f'ffmpeg-{FFMPEG_VERSION}-*', 'bin', 'ffmpeg.exe')
    found = sorted(glob.glob(pattern)) if env.get('LOCALAPPDATA') else []
    return found[0] if found else None


def ffmpeg_version(path, run=run_command):
    """The version token of `ffmpeg -version` (e.g. '9.0.2-full_build') or None."""
    code, out, _ = run([path, '-hide_banner', '-version'])
    words = out.splitlines()[0].split() if code == 0 and out.strip() else []
    return words[2] if len(words) >= 3 and words[:2] == ['ffmpeg', 'version'] else None


def _check(name, status, detail, fix=None):
    if status not in STATUSES:
        raise ValueError(f'unknown status {status!r}')
    return {'name': name, 'status': status, 'detail': detail, **({'fix': fix} if fix else {})}


def _json(text):
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError('expected a JSON object')
    return value


def run_checks(*, image=None, min_free_gib=DEFAULT_MIN_FREE_GIB, docker=None, which=shutil.which,
               disk_usage=shutil.disk_usage, env=None, platform=sys.platform, repo=REPO, ffmpeg=find_ffmpeg,
               run=run_command, temp_dir=None):
    """All checks in order (always the same names), as a list of dicts. Every
    collaborator is injected so the rules can be tested without Docker.

    The environment is the one the lab tests run with: `.local/lab-test.env` values
    under the process environment (`uv run --env-file` does not override variables
    already set). It supplies AVA_TEST_DOCKER_LAB, AVA_LAB_IMAGE and AVA_FFMPEG."""
    env = dict(os.environ if env is None else env)
    lab_env = Path(repo, '.local', 'lab-test.env')
    try:
        file_values = read_env_file(lab_env) if lab_env.is_file() else None
    except OSError:
        file_values = None
    merged = {**(file_values or {}), **env}
    image = image or merged.get('AVA_LAB_IMAGE') or DEFAULT_IMAGE
    checks = []
    docker_path = which('docker')
    if docker_path is None:
        checks.append(_check('docker_cli', 'fail', 'docker is not on PATH',
                             'install Docker Desktop (WSL 2 backend) and reopen the shell'))
        engine_ok = False
    else:
        checks.append(_check('docker_cli', 'pass', 'docker found'))
        if docker is None:  # review L2: run the resolved executable, as harness.py does
            def docker(args):
                return run([docker_path, *args])
        engine_ok = _engine(checks, docker)
    if engine_ok:
        _backend(checks, docker, platform)
        _image(checks, docker, image)
        _leftovers(checks, docker)
    else:
        skipped = ['docker_backend', 'lab_image', 'leftover_containers']
        if docker_path is None:  # review L4: the same check names on every run
            skipped.insert(0, 'docker_engine')
        for name in skipped:
            checks.append(_check(name, 'skip', 'the Docker engine is not usable'))
    _disk(checks, disk_usage, {'repository': repo, 'temp (lab I/O)': temp_dir or tempfile.gettempdir()},
          min_free_gib)
    if file_values is None:
        checks.append(_check('lab_env', 'warn', '.local/lab-test.env is missing or unreadable, so the lab '
                             'tests skip', 'create it with the line AVA_TEST_DOCKER_LAB=1'))
    elif merged.get('AVA_TEST_DOCKER_LAB') != '1':
        checks.append(_check('lab_env', 'warn', 'AVA_TEST_DOCKER_LAB is not 1 in the lab test environment, so the '
                             'lab tests skip', 'put the line AVA_TEST_DOCKER_LAB=1 in .local/lab-test.env'))
    else:
        checks.append(_check('lab_env', 'pass', 'AVA_TEST_DOCKER_LAB=1 in the lab test environment'))
    _ffmpeg(checks, ffmpeg(merged), run)
    return checks


def _ffmpeg(checks, path, run):
    fix = f'install ffmpeg {FFMPEG_VERSION} (winget Gyan.FFmpeg) or set AVA_FFMPEG to it'
    if path is None:
        checks.append(_check('ffmpeg', 'warn', 'no pinned ffmpeg: the GSM lab cases skip', fix))
        return
    version = ffmpeg_version(path, run)
    if version is None:  # review M2: a stale AVA_FFMPEG makes those cases error, not skip
        checks.append(_check('ffmpeg', 'warn', 'the configured ffmpeg does not run: the GSM lab cases fail', fix))
    elif version.split('-')[0] != FFMPEG_VERSION:  # the exact-token rule of ffmpeg_codecs.Ffmpeg
        checks.append(_check('ffmpeg', 'warn', f'ffmpeg {version} is not the pinned {FFMPEG_VERSION}: the GSM lab '
                             'cases fail', fix))
    else:
        missing = []
        for kind in ('-encoders', '-decoders'):  # review N3: the wrapper's codec rule too
            code, out, _ = run([path, '-hide_banner', kind])
            names = {line.split()[1] for line in out.splitlines() if len(line.split()) > 1} if code == 0 else set()
            missing += [f'{kind[1:-1]} {name}' for name in FFMPEG_CODECS if name not in names]
        if missing:
            checks.append(_check('ffmpeg', 'warn', f'ffmpeg {version} lacks {", ".join(missing)}: the GSM lab '
                                 'cases fail', fix))
        else:
            checks.append(_check('ffmpeg', 'pass', f'ffmpeg {version} (pinned {FFMPEG_VERSION}) with '
                                 + ', '.join(FFMPEG_CODECS)))


def _engine(checks, docker):
    code, out, err = docker(['version', '--format', '{{json .}}'])
    if code is None:
        checks.append(_check('docker_engine', 'fail', f'docker version did not answer ({err})',
                             'start Docker Desktop and wait until the engine is running'))
        return False
    try:
        version = _json(out)
    except ValueError:
        version = None
    server = version.get('Server') if version else None
    if code != 0 or not isinstance(server, dict):
        checks.append(_check('docker_engine', 'fail', 'the Docker engine is not running or not reachable',
                             'start Docker Desktop and wait until the engine is running'))
        return False
    os_type = server.get('Os')
    if os_type != 'linux':
        checks.append(_check('docker_engine', 'fail', f'engine runs {os_type!r} containers; the lab needs linux',
                             'switch Docker Desktop to Linux containers'))
        return False
    checks.append(_check('docker_engine', 'pass', f"server {server.get('Version', '?')} ({os_type})"))
    return True


def _backend(checks, docker, platform):
    code, out, _ = docker(['info', '--format', '{{json .}}'])
    try:
        info = _json(out) if code == 0 else None
    except ValueError:
        info = None
    if info is None:
        checks.append(_check('docker_backend', 'warn', 'docker info gave no usable answer'))
        return
    kernel, system = str(info.get('KernelVersion', '')), str(info.get('OperatingSystem', ''))
    cpus, memory = info.get('NCPU'), info.get('MemTotal')
    problems = []
    if platform == 'win32' and 'wsl2' not in kernel.lower():
        problems.append('the engine is not on WSL 2')
    if not isinstance(cpus, int) or isinstance(cpus, bool) or cpus < MIN_CPUS:
        problems.append(f'engine CPUs {cpus!r} < {MIN_CPUS}')
    if not isinstance(memory, int) or isinstance(memory, bool) or memory < MIN_MEMORY_BYTES:
        problems.append(f'engine memory {memory!r} bytes < 4 GiB')
    gib = memory / 1024 ** 3 if isinstance(memory, int) and not isinstance(memory, bool) else None
    detail = f"{system or '?'}, kernel {kernel or '?'}, {cpus} CPUs, " + (f'{gib:.1f} GiB' if gib else '? GiB')
    if problems:
        checks.append(_check('docker_backend', 'warn', f"{detail}: {'; '.join(problems)}",
                             'use Docker Desktop with the WSL 2 backend and give it more CPU/memory'))
    else:
        checks.append(_check('docker_backend', 'pass', detail))


def _image(checks, docker, image):
    code, out, _ = docker(['image', 'inspect', image, '--format', '{{.Id}}'])
    if code == 0 and out.strip().startswith('sha256:'):
        checks.append(_check('lab_image', 'pass', f'{image} {out.strip()[:19]}'))
    else:
        checks.append(_check('lab_image', 'fail', f'{image} is not built locally (the tests never pull)',
                             f'docker build -t {image} lab/asterisk'))


def _leftovers(checks, docker):
    code, out, _ = docker(['ps', '-a', '--filter', f'label={LAB_LABEL}', '--format', '{{.Names}}'])
    if code != 0:
        checks.append(_check('leftover_containers', 'warn', 'docker ps failed'))
        return
    names = [line.strip() for line in out.splitlines() if line.strip()]
    if names:
        shown = ', '.join(names[:10]) + (f' (first 10 of {len(names)})' if len(names) > 10 else '')
        checks.append(_check('leftover_containers', 'warn', f'{len(names)} lab test container(s) left: {shown}',
                             f'remove every container labelled {LAB_LABEL}: docker rm -f with the IDs from '
                             f'docker ps -aq --filter label={LAB_LABEL}'))
    else:
        checks.append(_check('leftover_containers', 'pass', 'none'))


def _disk(checks, disk_usage, paths, min_free_gib):
    """Free space on each place the lab writes: the repository and the temporary folder
    holding the harness's I/O folders (review L3; Docker Desktop's own disk image is
    not measured). GiB throughout."""
    parts, low, errors = [], [], []
    for label, path in paths.items():
        try:
            free = disk_usage(path).free / 1024 ** 3
        except OSError as error:
            errors.append(f'{label}: {type(error).__name__}')
            continue
        parts.append(f'{label} {free:.1f} GiB free')
        if free < min_free_gib:
            low.append(label)
    detail = '; '.join(parts + errors) or 'nothing measured'
    if errors or low:
        checks.append(_check('disk_free', 'warn', detail + (f' (< {min_free_gib:g} GiB: {", ".join(low)})'
                                                            if low else ''), 'free disk space'))
    else:
        checks.append(_check('disk_free', 'pass', detail))


def summary(checks):
    counts = {status: sum(c['status'] == status for c in checks) for status in STATUSES}
    return {'ok': counts['fail'] == 0, 'counts': counts, 'checks': checks}


def main(argv=None, *, out=sys.stdout, **collaborators):
    parser = argparse.ArgumentParser(description='Read-only Docker/WSL preflight for the AVA telephony lab')
    parser.add_argument('--image', default=None,
                        help=f'lab image (default: AVA_LAB_IMAGE from the lab test environment, else {DEFAULT_IMAGE})')
    parser.add_argument('--min-free-gib', type=float, default=DEFAULT_MIN_FREE_GIB)
    parser.add_argument('--json', action='store_true', help='print one JSON object instead of lines')
    args = parser.parse_args(argv)
    if not args.min_free_gib >= 0 or args.min_free_gib == float('inf'):
        parser.error('--min-free-gib must be a finite number >= 0')
    result = summary(run_checks(image=args.image, min_free_gib=args.min_free_gib, **collaborators))
    if args.json:
        out.write(json.dumps(result, indent=2) + '\n')
    else:
        for check in result['checks']:
            out.write(f"{check['status'].upper():5} {check['name']}: {check['detail']}\n")
            if 'fix' in check and check['status'] in ('fail', 'warn'):
                out.write(f"      fix: {check['fix']}\n")
        out.write('preflight ' + ('passed' if result['ok'] else 'FAILED') + f" {result['counts']}\n")
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
