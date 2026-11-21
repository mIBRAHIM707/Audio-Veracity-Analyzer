"""CycloneDX 1.5 software bill of materials for an AVA deployment image (PRD FR-O7
"images pinned by digest; hashed pip requirements; SBOM generated in CI"; backlog 10.2).

Standard library only, so it runs inside the image it describes without installing
anything there. Run it in the image with the repository mounted read-only, as the image
user, with no network, for example:

    docker run --rm --network none -v <repo>:/src:ro --entrypoint python ava-live:dev \
        -I /src/scripts/image_sbom.py --image-name ava-live --image-id <docker image ID> \
        --dockerfile /src/deploy/live/Dockerfile \
        --requirements /src/deploy/live/requirements-live.txt --root /src --check-pins

`-I` keeps the mounted folder off `sys.path`, so only the image's own packages are read.

What it records:
- the image itself (`metadata.component`, the Docker image ID passed in) and each `FROM`
  base image with its sha256 digest (a `FROM` without a digest is refused). The digests,
  the pins and the npm list are read from the mounted checkout (`ava:source` property),
  not from the image, so build and generate from the same checkout and pass
  `--source-revision`;
- every installed Python distribution (`importlib.metadata`), with its licence as the
  metadata states it, whether the image's requirement file pins it, and the SHA-256 of
  its installed `RECORD` file (a fingerprint of the installed files, not a wheel hash);
- every Debian package dpkg lists as installed, with architecture and distribution
  (licences are not recorded: dpkg's status file has none);
- optionally the npm packages of a lockfile (the dashboard bundle), with the lockfile's
  integrity hash; build-only (dev) packages are listed with scope `excluded`.

`--check-pins` compares the installed Python set with the requirement file, read the way
the image reads it (see `read_pins`): every pinned package must be installed at exactly
its version, and nothing else may be installed except the base image's packaging tools
(`BASE_IMAGE_TOOLS`). Problems go to stderr and the exit status is 1; the SBOM is still
written, so the evidence of the mismatch is kept. The exemption is by name: that those
tools still have the base image's versions is checked by the lab test
(tests/lab/test_image_sbom_lab.py), not here.

Output is deterministic for the same inputs: sorted components, a serial number derived
from the content, and a timestamp only when `--timestamp` is given.

Exit status: 0 written (and pins match when checked), 1 pins do not match, 2 refused
input or usage error (nothing written).

Not done here: wheel hashes (needs a hashed lock, see deploy/compose/README.md), a
dependency graph, licences of Debian packages, files outside package managers.
"""
from __future__ import annotations

import argparse
import base64
import binascii
from datetime import datetime
import hashlib
from importlib import metadata
import json
from pathlib import Path
import platform
import re
import sys
from typing import Iterable, Mapping
from urllib.parse import quote
import uuid

SPEC_VERSION = '1.5'
TOOL_NAME = 'ava-image-sbom'
TOOL_VERSION = '1'
# Installed by the python:3.11-slim base image, not by the requirement file.
BASE_IMAGE_TOOLS = frozenset({'pip', 'setuptools', 'wheel'})
ALLOWED_OPTIONS = frozenset({'--extra-index-url https://download.pytorch.org/whl/cpu'})
PIN = re.compile(r'^([A-Za-z0-9][A-Za-z0-9._-]*)==([0-9][0-9A-Za-z.+!-]*)$')
FROM_LINE = re.compile(r'^FROM\s+(\S+)(?:\s+AS\s+(\S+))?\s*$', re.IGNORECASE)
DIGEST_REF = re.compile(r'^([^@\s]+)@sha256:([0-9a-f]{64})$')
IMAGE_ID = re.compile(r'^sha256:[0-9a-f]{64}$')
IMAGE_NAME = re.compile(r'^[a-z0-9][a-z0-9._/-]{0,127}$')
SOURCE_REVISION = re.compile(r'^[0-9a-f]{7,40}(\+dirty)?$')
RFC3339 = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?(Z|[+-]\d{2}:\d{2})$')
LICENCE_CLASSIFIER = 'License :: '
MAX_LICENCE_FIELD = 100  # a longer License field is usually the whole licence text
MAX_INPUT_BYTES = 16_000_000
INTEGRITY_ALGS = {'sha512': ('SHA-512', 64), 'sha384': ('SHA-384', 48), 'sha256': ('SHA-256', 32),
                  'sha1': ('SHA-1', 20)}
NPM_PREFIX = 'node_modules/'


class SbomError(ValueError):
    """Refused input; nothing is written."""


def normalise(name: str) -> str:
    """PEP 503 name normalisation."""
    return re.sub(r'[-_.]+', '-', name).lower()


def _purl_part(text: str) -> str:
    """Percent-encode a purl name or version ('+' -> %2B, ':' -> %3A, '@' -> %40)."""
    return quote(text, safe='.-_~')


def read_pins(path, root, chain=()):
    """Return ({package: version}, options) for `path`, following `-r` names as files in
    `root` (the Dockerfiles copy the root `requirements-*.txt` and the image file into one
    folder). Raises SbomError on an unpinned line, an option other than the PyTorch CPU
    index, a conflicting pin, a missing include or a cycle (a file included twice by
    different files is fine, as pip allows)."""
    path = Path(path)
    if path.resolve() in chain:
        raise SbomError(f'include cycle at {path.name}')
    chain = (*chain, path.resolve())
    pins, options = {}, set()
    for number, raw in enumerate(_read_text(path, f'requirement file {path.name}').splitlines(), 1):
        line = raw.split('#', 1)[0].strip()
        if not line:
            continue
        if line.startswith('-r '):
            included = Path(root) / line[3:].strip()
            if not included.is_file():
                raise SbomError(f'{path.name}:{number}: included file not found: {line[3:].strip()}')
            more, more_options = read_pins(included, root, chain)
            options |= more_options
            for name, version in more.items():
                if pins.setdefault(name, version) != version:
                    raise SbomError(f'{name} pinned to {pins[name]} and {version}')
            continue
        if line.startswith('-'):
            if line not in ALLOWED_OPTIONS:
                raise SbomError(f'{path.name}:{number}: option not allowed: {line}')
            options.add(line)
            continue
        match = PIN.match(line)
        if match is None:
            raise SbomError(f'{path.name}:{number}: not an exact pin: {line}')
        name, version = normalise(match.group(1)), match.group(2)
        if pins.setdefault(name, version) != version:
            raise SbomError(f'{name} pinned to {pins[name]} and {version}')
    return pins, options


def base_images(dockerfile_text: str) -> list[dict]:
    """Container components for each external `FROM` image; every one must name a sha256
    digest. A `FROM <earlier stage>` is not an image and is skipped."""
    stages, images = set(), {}
    for raw in dockerfile_text.splitlines():
        line = raw.strip()
        if not line.upper().startswith('FROM '):
            continue
        match = FROM_LINE.match(line)
        if match is None:
            raise SbomError(f'unreadable FROM line: {line}')
        ref, stage = match.group(1), match.group(2)
        if ref.lower() not in stages:
            digest = DIGEST_REF.match(ref)
            if digest is None:
                raise SbomError(f'FROM without a sha256 digest: {ref}')
            name, value = digest.group(1), digest.group(2)
            # A tag is the part after the last ':' of the last path segment (a registry
            # host may carry a port: host:5000/repo:tag).
            last = name.rsplit('/', 1)[-1]
            if ':' in last:
                repository, tag = name.rsplit(':', 1)
            else:
                repository, tag = name, ''
            bom_ref = f'image:{name}@sha256:{value}'
            # Read from the Dockerfile in the checkout, not from the image: see
            # `--source-revision` for which checkout that was.
            images[bom_ref] = {
                'type': 'container', 'bom-ref': bom_ref, 'name': repository,
                **({'version': tag} if tag else {}),
                'hashes': [{'alg': 'SHA-256', 'content': value}],
                'properties': [{'name': 'ava:origin', 'value': 'base-image'},
                               {'name': 'ava:source', 'value': 'dockerfile'}],
            }
        if stage:
            stages.add(stage.lower())
    if not images:
        raise SbomError('no FROM image found')
    return [images[key] for key in sorted(images)]


def _python_licences(meta) -> list[dict]:
    expression = (meta.get('License-Expression') or '').strip()
    if expression:
        return [{'expression': expression}]
    names = []
    for classifier in meta.get_all('Classifier') or []:
        parts = [part.strip() for part in classifier.split('::')]
        # `License :: OSI Approved` alone names no licence; it needs a third segment.
        if classifier.startswith(LICENCE_CLASSIFIER) and len(parts) >= 3 and parts[-1]:
            names.append(parts[-1])
    field = (meta.get('License') or '').strip()
    if not names and field and '\n' not in field and len(field) <= MAX_LICENCE_FIELD:
        names.append(field)
    return [{'license': {'name': name}} for name in dict.fromkeys(names) if name]


def python_components(distributions: Iterable, pins: Mapping[str, str] | None = None) -> list[dict]:
    """Library components for installed distributions. Refuses a distribution without a
    name or version, and the same name installed twice."""
    pins = pins or {}
    found = {}
    for dist in distributions:
        try:
            found_one = _python_component(dist, pins)
        except (OSError, UnicodeDecodeError, TypeError, AttributeError) as error:
            # A broken install (no METADATA, a non-UTF-8 RECORD) is refused input (exit 2),
            # never a traceback with exit 1, which means "pins differ".
            raise SbomError(f'unreadable Python distribution: {type(error).__name__}') from error
        key, component = found_one
        if key in found:
            raise SbomError(f'Python distribution installed twice: {key}')
        found[key] = component
    return [found[key] for key in sorted(found)]


def _python_component(dist, pins: Mapping[str, str]) -> tuple[str, dict]:
    meta = dist.metadata
    name, version = (meta.get('Name') or '').strip(), (meta.get('Version') or '').strip()
    if not name or not version:
        raise SbomError('a Python distribution has no name or version')
    key = normalise(name)
    # purl pypi rule: lower-case, '_' -> '-' (dots stay: pkg:pypi/zope.interface).
    purl = f'pkg:pypi/{_purl_part(name.lower().replace("_", "-"))}@{_purl_part(version)}'
    properties = [{'name': 'ava:pinned', 'value': 'true' if pins.get(key) == version else 'false'}]
    if key in BASE_IMAGE_TOOLS and key not in pins:
        properties.append({'name': 'ava:origin', 'value': 'base-image'})
    record = dist.read_text('RECORD')
    if record is not None:
        properties.append({'name': 'ava:record-sha256',
                           'value': hashlib.sha256(record.encode('utf-8')).hexdigest()})
    component = {'type': 'library', 'bom-ref': purl, 'name': name, 'version': version, 'purl': purl}
    licences = _python_licences(meta)
    if licences:
        component['licenses'] = licences
    component['properties'] = sorted(properties, key=lambda p: p['name'])
    return key, component


def python_runtime(implementation: str, version: str) -> dict:
    """The interpreter itself (the official python images install it outside dpkg)."""
    if not re.fullmatch(r'[A-Za-z]+', implementation) or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+[0-9a-z+]*', version):
        raise SbomError('unreadable Python implementation or version')
    name = implementation.lower()
    purl = f'pkg:generic/{name}@{_purl_part(version)}'
    return {'type': 'framework', 'bom-ref': purl, 'name': name, 'version': version, 'purl': purl,
            'properties': [{'name': 'ava:origin', 'value': 'base-image'}]}


def installed_versions(components: Iterable[dict]) -> dict[str, str]:
    return {normalise(c['name']): c['version'] for c in components if c['purl'].startswith('pkg:pypi/')}


def check_pins(installed: Mapping[str, str], pins: Mapping[str, str],
               exempt: Iterable[str] = BASE_IMAGE_TOOLS) -> list[str]:
    """Problems between the installed Python set and the pins; empty when they match."""
    exempt = frozenset(normalise(name) for name in exempt)
    problems = []
    for name in sorted(pins):
        if name not in installed:
            problems.append(f'pinned but not installed: {name}=={pins[name]}')
        elif installed[name] != pins[name]:
            problems.append(f'version differs: {name} pinned {pins[name]}, installed {installed[name]}')
    for name in sorted(set(installed) - set(pins) - exempt):
        problems.append(f'installed but not pinned: {name}=={installed[name]}')
    return problems


def _os_release(text: str) -> dict[str, str]:
    values = {}
    for line in text.splitlines():
        key, sep, value = line.partition('=')
        if sep and key.strip():
            values[key.strip()] = value.strip().strip('"\'')
    return values


def deb_components(status_text: str, os_release_text: str) -> list[dict]:
    """Components for the packages dpkg's status file marks `install ok installed`."""
    release = _os_release(os_release_text)
    distro_id, version_id = release.get('ID', ''), release.get('VERSION_ID', '')
    if not re.fullmatch(r'[a-z0-9]+', distro_id) or not re.fullmatch(r'[0-9.]+', version_id):
        raise SbomError('os-release has no usable ID and VERSION_ID')
    distro = f'{distro_id}-{version_id}'
    found = {}
    for stanza in re.split(r'\n\s*\n', status_text.replace('\r\n', '\n')):
        fields, last = {}, None
        for line in stanza.split('\n'):
            if line[:1] in (' ', '\t') and last:
                continue  # continuation of a multi-line field (Description, Conffiles)
            key, sep, value = line.partition(':')
            if sep:
                last = key.strip()
                fields[last] = value.strip()
        # Status is "<want> <flag> <state>"; a held package ("hold ok installed") is installed too.
        status = fields.get('Status', '').split()
        if len(status) != 3 or status[1] != 'ok' or status[2] != 'installed':
            continue
        name, version, arch = fields.get('Package', ''), fields.get('Version', ''), fields.get('Architecture', '')
        if not re.fullmatch(r'[a-z0-9][a-z0-9.+-]+', name) or not version or not re.fullmatch(r'[a-z0-9-]+', arch):
            raise SbomError(f'unreadable dpkg entry: {name or "(no name)"}')
        purl = (f'pkg:deb/{distro_id}/{_purl_part(name)}@{_purl_part(version)}'
                f'?arch={arch}&distro={distro}')
        if purl in found:
            raise SbomError(f'dpkg package listed twice: {name}')
        found[purl] = {'type': 'library', 'bom-ref': purl, 'name': name, 'version': version, 'purl': purl}
    if not found:
        raise SbomError('dpkg status lists no installed package')
    return [found[key] for key in sorted(found)]


def _integrity_hash(integrity: str) -> dict:
    alg, sep, encoded = integrity.partition('-')
    if not sep or alg not in INTEGRITY_ALGS:
        raise SbomError(f'unsupported integrity value: {integrity[:16]}')
    name, size = INTEGRITY_ALGS[alg]
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as error:
        raise SbomError('integrity value is not base64') from error
    if len(raw) != size:
        raise SbomError(f'integrity value has {len(raw)} bytes, {alg} needs {size}')
    return {'alg': name, 'content': raw.hex()}


def npm_components(lock: Mapping) -> list[dict]:
    """Components for an npm lockfile (version 2 or 3 `packages` map). The root project
    and linked folders are skipped. Scopes follow npm's flags: `dev` (strictly the dev
    tree, also with `optional`) is `excluded`; `optional`, and `devOptional` (a dev
    dependency that is also an optional dependency of a non-dev package, so in the
    production tree), are `optional`; everything else is `required`. An alias folder
    (`"foo": "npm:bar@1"`) is named by the entry's `name`, the real package."""
    if not isinstance(lock, Mapping) or lock.get('lockfileVersion') not in (2, 3):
        raise SbomError('npm lockfile must be lockfileVersion 2 or 3')
    packages = lock.get('packages')
    if not isinstance(packages, Mapping):
        raise SbomError('npm lockfile has no packages map')
    found = {}
    for path, entry in packages.items():
        if path == '' or not isinstance(entry, Mapping) or entry.get('link'):
            continue
        if NPM_PREFIX not in path:
            raise SbomError(f'npm package path outside node_modules: {path}')
        name = entry.get('name', path.rsplit(NPM_PREFIX, 1)[1])
        version = entry.get('version')
        if not isinstance(name, str) or not re.fullmatch(r'(@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*', name) \
                or not isinstance(version, str) or not version:
            raise SbomError(f'unreadable npm package: {path}')
        scope_name, _, bare = name.rpartition('/')
        purl = f'pkg:npm/{_purl_part(scope_name) + "/" if scope_name else ""}{bare}@{_purl_part(version)}'
        if entry.get('dev'):
            scope = 'excluded'
        elif entry.get('optional') or entry.get('devOptional'):
            scope = 'optional'
        else:
            scope = 'required'
        component = {'type': 'library', 'bom-ref': purl, 'name': bare, 'version': version, 'purl': purl,
                     'scope': scope, 'properties': [{'name': 'ava:source', 'value': 'lockfile'}]}
        if scope_name:
            component['group'] = scope_name
        integrity = entry.get('integrity')
        if isinstance(integrity, str) and integrity:
            component['hashes'] = [_integrity_hash(integrity.split()[0])]
        licence = entry.get('license')
        if isinstance(licence, str) and licence.strip():
            component['licenses'] = [{'expression': licence.strip()}]
        previous = found.get(purl)
        if previous is not None:
            # The same package at two paths: keep the widest scope, require equal hashes.
            if previous.get('hashes') != component.get('hashes'):
                raise SbomError(f'npm package {name}@{version} has two different integrity values')
            order = ('required', 'optional', 'excluded')
            if order.index(scope) < order.index(previous['scope']):
                previous['scope'] = scope
            continue
        found[purl] = component
    return [found[key] for key in sorted(found)]


def build_bom(image_name: str, image_id: str, components: Iterable[dict], timestamp: str | None = None,
              source_revision: str | None = None) -> dict:
    """The CycloneDX document. Refuses duplicate bom-refs. `source_revision` names the
    checkout the Dockerfile, requirement file and lockfile were read from."""
    if not IMAGE_NAME.match(image_name):
        raise SbomError('image name must be a lower-case repository name')
    if not IMAGE_ID.match(image_id):
        raise SbomError('image ID must be sha256:<64 hex>')
    if source_revision is not None and not SOURCE_REVISION.match(source_revision):
        raise SbomError('source revision must be a git commit (7-40 lower-case hex), optionally +dirty')
    components = list(components)
    refs = [c['bom-ref'] for c in components]
    duplicates = sorted({ref for ref in refs if refs.count(ref) > 1})
    if duplicates:
        raise SbomError(f'duplicate component: {duplicates[0]}')
    image_ref = f'image:{image_name}@{image_id}'
    metadata_block = {
        'tools': {'components': [{'type': 'application', 'name': TOOL_NAME, 'version': TOOL_VERSION}]},
        'component': {'type': 'container', 'bom-ref': image_ref, 'name': image_name,
                      'hashes': [{'alg': 'SHA-256', 'content': image_id.split(':', 1)[1]}]},
    }
    if source_revision is not None:
        metadata_block['properties'] = [{'name': 'ava:source-revision', 'value': source_revision}]
    if timestamp is not None:
        metadata_block = {'timestamp': timestamp, **metadata_block}
    body = {'metadata': metadata_block, 'components': components}
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
    serial = uuid.uuid5(uuid.NAMESPACE_URL, f'urn:ava:sbom:{digest}')
    return {'bomFormat': 'CycloneDX', 'specVersion': SPEC_VERSION, 'serialNumber': f'urn:uuid:{serial}',
            'version': 1, **body}


def _read_text(path: str, what: str) -> str:
    try:
        file = Path(path)
        if file.stat().st_size > MAX_INPUT_BYTES:
            raise SbomError(f'{what} is larger than {MAX_INPUT_BYTES} bytes')
        return file.read_text(encoding='utf-8')
    except OSError as error:
        raise SbomError(f'cannot read {what}: {error.strerror or type(error).__name__}') from error
    except UnicodeDecodeError as error:
        raise SbomError(f'{what} is not UTF-8') from error


def _timestamp(value: str) -> str:
    """An RFC 3339 date-time (the schema's format): `T` separator, offset or Z."""
    message = 'timestamp must be RFC 3339, e.g. 2026-11-21T08:00:00+05:00'
    if not RFC3339.match(value):
        raise SbomError(message)
    try:
        datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as error:
        raise SbomError(message) from error
    return value


def generate(args, distributions=None) -> tuple[dict, list[str]]:
    """The SBOM and the pin problems (empty unless --check-pins found some)."""
    pins, _ = read_pins(args.requirements, args.root)
    components = base_images(_read_text(args.dockerfile, 'Dockerfile'))
    python = python_components(metadata.distributions() if distributions is None else distributions, pins)
    components += python
    components.append(python_runtime(platform.python_implementation(), platform.python_version()))
    if args.dpkg_status:
        components += deb_components(_read_text(args.dpkg_status, 'dpkg status'),
                                     _read_text(args.os_release, 'os-release'))
    if args.npm_lock:
        try:
            lock = json.loads(_read_text(args.npm_lock, 'npm lockfile'))
        except json.JSONDecodeError as error:
            raise SbomError('npm lockfile is not JSON') from error
        components += npm_components(lock)
    problems = check_pins(installed_versions(python), pins) if args.check_pins else []
    timestamp = _timestamp(args.timestamp) if args.timestamp else None
    return build_bom(args.image_name, args.image_id, components, timestamp, args.source_revision), problems


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n', 1)[0])
    parser.add_argument('--image-name', required=True, help='repository name of the image, e.g. ava-live')
    parser.add_argument('--image-id', required=True, help='Docker image ID, sha256:<64 hex>')
    parser.add_argument('--dockerfile', required=True)
    parser.add_argument('--requirements', required=True, help="the image's requirement file")
    parser.add_argument('--root', required=True, help='folder that `-r` names are read from')
    parser.add_argument('--dpkg-status', default='/var/lib/dpkg/status',
                        help="dpkg's status file; empty string to skip Debian packages")
    parser.add_argument('--os-release', default='/etc/os-release')
    parser.add_argument('--npm-lock', help='npm lockfile whose packages the image bundles')
    parser.add_argument('--timestamp', help='RFC 3339 time with offset; omitted by default')
    parser.add_argument('--source-revision',
                        help='git commit of the checkout the Dockerfile, pins and lockfile come from '
                             '(append +dirty for uncommitted changes); recorded in metadata')
    parser.add_argument('--check-pins', action='store_true',
                        help='exit 1 unless the installed Python set equals the pins')
    parser.add_argument('--output', default='-', help='file to create (never overwritten), or - for stdout')
    return parser.parse_args(argv)


def main(argv=None, distributions=None, stdout=None, stderr=None) -> int:
    stdout, stderr = stdout or sys.stdout, stderr or sys.stderr
    args = parse_args(argv)
    try:
        bom, problems = generate(args, distributions)
        text = json.dumps(bom, indent=2, sort_keys=False) + '\n'
        if args.output == '-':
            stdout.write(text)
        else:
            try:
                with open(args.output, 'x', encoding='utf-8', newline='\n') as handle:
                    handle.write(text)
            except FileExistsError as error:
                raise SbomError('output file exists; it is never overwritten') from error
            except OSError as error:
                raise SbomError(f'cannot write output: {error.strerror or type(error).__name__}') from error
    except SbomError as error:
        print(f'image_sbom: refused: {error}', file=stderr)
        return 2
    for problem in problems:
        print(f'image_sbom: pin check: {problem}', file=stderr)
    counts = {}
    for component in bom['components']:
        kind = component.get('purl', 'pkg:container/').split(':', 1)[1].split('/', 1)[0]
        counts[kind] = counts.get(kind, 0) + 1
    print(f'image_sbom: {len(bom["components"])} components {json.dumps(counts, sort_keys=True)}; '
          f'pins {"not checked" if not args.check_pins else ("match" if not problems else "DIFFER")}',
          file=stderr)
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
