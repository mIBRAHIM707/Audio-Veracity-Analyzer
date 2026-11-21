"""Claude-authored tests (21 November 2026): the image SBOM generator
`scripts/image_sbom.py` (backlog 10.2; PRD FR-O7 "SBOM generated in CI", architecture
supply chain row).

Authored by Claude (Claude Code), not by GPT-6 Astra. No Docker: Python distributions are
real `.dist-info` folders read through `importlib.metadata`, dpkg and npm inputs are text
in the formats those tools write, and the real Dockerfiles, requirement files and the
dashboard lockfile are read where the rule concerns them. The run inside the real images
is tests/lab/test_image_sbom_lab.py. When `cyclonedx-python-lib` is installed, the output
is also validated against the bundled CycloneDX 1.5 JSON schema (an independent oracle);
the case skips without it.
"""
import base64
import hashlib
from importlib import metadata
import io
import json
from pathlib import Path
import re
import tempfile
import unittest

from scripts import image_sbom as sb

ROOT = Path(__file__).resolve().parents[2]
SERIAL = re.compile(r'^urn:uuid:[0-9a-f]{8}-[0-9a-f]{4}-5[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$')
IMAGE_ID = 'sha256:' + 'ab' * 32
DIGEST = 'c' * 64


def make_dist(folder, name, version, *, extra='', record='a.py,sha256=x,1\n', dist_dir=None):
    """A real installed-distribution folder; returns nothing (read via distributions())."""
    path = Path(folder) / (dist_dir or f'{name.replace("-", "_")}-{version}.dist-info')
    path.mkdir()
    lines = ['Metadata-Version: 2.1']
    if name is not None:
        lines.append(f'Name: {name}')
    if version is not None:
        lines.append(f'Version: {version}')
    (path / 'METADATA').write_text('\n'.join(lines) + '\n' + extra, encoding='utf-8')
    if record is not None:
        (path / 'RECORD').write_text(record, encoding='utf-8')


def dists(folder):
    return list(metadata.distributions(path=[str(folder)]))


def props(component):
    return {p['name']: p['value'] for p in component.get('properties', [])}


DPKG = '''Package: libc6
Status: install ok installed
Priority: optional
Architecture: amd64
Version: 2.36-9+deb12u10
Description: GNU C Library: Shared libraries
 Contains the standard libraries that are used by nearly all programs on
 the system.
Conffiles:
 /etc/ld.so.conf.d/x86_64-linux-gnu.conf d4e7a7b88a71b5ffd9e2644e71a0cfab

Package: libgcc-s1
Status: install ok installed
Architecture: amd64
Version: 1:12.2.0-14+deb12u1

Package: libstdc++6
Status: hold ok installed
Architecture: amd64
Version: 12.2.0-14

Package: removed-thing
Status: deinstall ok config-files
Architecture: amd64
Version: 1.0
'''
OS_RELEASE = 'PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"\nID=debian\nVERSION_ID="12"\n'


class RequirementPinTests(unittest.TestCase):
    def test_the_real_image_files_read_as_exact_pin_sets(self):
        live, live_options = sb.read_pins(ROOT / 'deploy/live/requirements-live.txt', ROOT)
        api, api_options = sb.read_pins(ROOT / 'deploy/api/requirements-api-image.txt', ROOT)
        self.assertEqual(live_options, set())
        self.assertEqual(api_options, set(sb.ALLOWED_OPTIONS))
        self.assertEqual(live['onnxruntime'], '1.32.0')
        self.assertTrue(api['torch'].endswith('+cpu'))
        self.assertNotIn('pip', live)  # the base tools come from the base image

    def test_a_refusal_is_a_value_error(self):
        # tests/unit/test_deploy_images.py relies on this (it catches ValueError).
        self.assertTrue(issubclass(sb.SbomError, ValueError))


class PythonComponentTests(unittest.TestCase):
    def test_names_versions_and_purls_follow_the_purl_rules(self):
        with tempfile.TemporaryDirectory() as folder:
            make_dist(folder, 'Typing_Extensions', '4.16.0')
            make_dist(folder, 'torch', '2.8.0+cpu')
            make_dist(folder, 'zope.interface', '7.2')
            got = sb.python_components(dists(folder), {'zope-interface': '7.2'})
        by_name = {c['name']: c for c in got}
        self.assertEqual([c['name'] for c in got], ['torch', 'Typing_Extensions', 'zope.interface'])  # by key
        self.assertEqual(by_name['Typing_Extensions']['purl'], 'pkg:pypi/typing-extensions@4.16.0')
        # purl keeps dots (only '_' becomes '-'); the pin match still uses PEP 503 names.
        self.assertEqual(by_name['zope.interface']['purl'], 'pkg:pypi/zope.interface@7.2')
        self.assertEqual(props(by_name['zope.interface'])['ava:pinned'], 'true')
        self.assertEqual(by_name['torch']['purl'], 'pkg:pypi/torch@2.8.0%2Bcpu')
        self.assertEqual(by_name['torch']['version'], '2.8.0+cpu')
        for component in got:
            self.assertEqual(component['bom-ref'], component['purl'])
            self.assertEqual(component['type'], 'library')

    def test_the_licence_comes_from_the_expression_then_classifiers_then_a_short_field(self):
        cases = {
            'expr': ('License-Expression: Apache-2.0 OR BSD-3-Clause\nLicense: ignored\n'
                     'Classifier: License :: OSI Approved :: MIT License\n',
                     [{'expression': 'Apache-2.0 OR BSD-3-Clause'}]),
            'classifiers': ('License: ignored when classifiers exist\n'
                            'Classifier: License :: OSI Approved :: BSD License\n'
                            'Classifier: Programming Language :: Python\n'
                            'Classifier: License :: OSI Approved :: BSD License\n',
                            [{'license': {'name': 'BSD License'}}]),
            'field': ('License: MIT\n', [{'license': {'name': 'MIT'}}]),
            'bare classifier': ('Classifier: License :: OSI Approved\nLicense: ISC\n',
                                [{'license': {'name': 'ISC'}}]),
            'bare classifier only': ('Classifier: License :: OSI Approved\n', None),
            'longtext': ('License: ' + 'Permission is hereby granted ' * 10 + '\n', None),
            'multiline': ('License: Copyright (c) someone\n        All rights reserved.\n', None),
            'none': ('', None),
        }
        for name, (extra, expected) in cases.items():
            with self.subTest(case=name), tempfile.TemporaryDirectory() as folder:
                make_dist(folder, f'pkg-{name}', '1.0', extra=extra)
                component = sb.python_components(dists(folder))[0]
                self.assertEqual(component.get('licenses'), expected)

    def test_pinned_origin_and_record_fingerprint(self):
        record = 'redis/__init__.py,sha256=abc,10\nredis-8.1.0.dist-info/RECORD,,\n'
        with tempfile.TemporaryDirectory() as folder:
            make_dist(folder, 'redis', '8.1.0', record=record)
            make_dist(folder, 'numpy', '2.3.4')
            make_dist(folder, 'pip', '24.0')
            make_dist(folder, 'wheel', '0.46.3', record=None)
            make_dist(folder, 'setuptools', '79.0.1')
            got = {c['name']: props(c) for c in sb.python_components(
                dists(folder), {'redis': '8.1.0', 'numpy': '2.3.5', 'setuptools': '79.0.1'})}
        self.assertEqual(got['redis']['ava:pinned'], 'true')
        self.assertEqual(got['redis']['ava:record-sha256'], hashlib.sha256(record.encode()).hexdigest())
        self.assertEqual(got['numpy']['ava:pinned'], 'false')  # pinned at another version
        self.assertEqual(got['pip'], {'ava:pinned': 'false', 'ava:origin': 'base-image',
                                      'ava:record-sha256': got['pip']['ava:record-sha256']})
        self.assertNotIn('ava:record-sha256', got['wheel'])  # no RECORD, no fingerprint
        self.assertNotIn('ava:origin', got['setuptools'])  # pinned, so the image's own
        self.assertNotIn('ava:origin', got['redis'])

    def test_refusals(self):
        with tempfile.TemporaryDirectory() as folder:
            make_dist(folder, 'soxr', '1.1.0')
            make_dist(folder, 'SoXR', '1.0.0', dist_dir='other.dist-info')
            with self.assertRaisesRegex(sb.SbomError, 'installed twice: soxr'):
                sb.python_components(dists(folder))
        with tempfile.TemporaryDirectory() as folder:
            make_dist(folder, 'noversion', None, dist_dir='noversion.dist-info')
            with self.assertRaisesRegex(sb.SbomError, 'no name or version'):
                sb.python_components(dists(folder))
        # Broken installs are refused input, not a traceback (which would exit 1).
        with tempfile.TemporaryDirectory() as folder:
            make_dist(folder, 'nometa', '1.0')
            (Path(folder) / 'nometa-1.0.dist-info' / 'METADATA').unlink()
            # Python 3.11 reads empty metadata here; some versions raise TypeError instead.
            with self.assertRaisesRegex(sb.SbomError, 'unreadable Python distribution|no name or version'):
                sb.python_components(dists(folder))
        with tempfile.TemporaryDirectory() as folder:
            make_dist(folder, 'badrecord', '1.0', record=None)
            (Path(folder) / 'badrecord-1.0.dist-info' / 'RECORD').write_bytes(b'caf\xe9.py,,\n')
            with self.assertRaisesRegex(sb.SbomError, 'unreadable Python distribution: UnicodeDecodeError'):
                sb.python_components(dists(folder))

    def test_the_runtime_component(self):
        component = sb.python_runtime('CPython', '3.11.14')
        self.assertEqual(component['purl'], 'pkg:generic/cpython@3.11.14')
        self.assertEqual(component['type'], 'framework')
        self.assertEqual(sb.python_runtime('CPython', '3.13.0rc2')['version'], '3.13.0rc2')
        self.assertEqual(sb.python_runtime('CPython', '3.11.14+')['purl'], 'pkg:generic/cpython@3.11.14%2B')
        for implementation, version in (('CPython', '3.11'), ('C Python', '3.11.1'), ('CPython', '3.11.1/x')):
            with self.subTest(version=version), self.assertRaises(sb.SbomError):
                sb.python_runtime(implementation, version)


class PinCheckTests(unittest.TestCase):
    def test_equal_sets_with_base_tools_match(self):
        self.assertEqual(sb.check_pins({'numpy': '2.3.5', 'pip': '24.0', 'wheel': '0.46.3'}, {'numpy': '2.3.5'}), [])

    def test_every_kind_of_difference_is_reported(self):
        problems = sb.check_pins(
            {'numpy': '2.3.4', 'extra-pkg': '1.0', 'pip': '23.0', 'setuptools': '1'},
            {'numpy': '2.3.5', 'soxr': '1.1.0', 'pip': '24.0'})
        self.assertEqual(problems, [  # pinned names in order, then the unpinned extras
            'version differs: numpy pinned 2.3.5, installed 2.3.4',
            'version differs: pip pinned 24.0, installed 23.0',  # a pinned base tool is checked
            'pinned but not installed: soxr==1.1.0',
            'installed but not pinned: extra-pkg==1.0',  # setuptools is an unpinned base tool
        ])

    def test_the_exemption_is_only_the_named_tools(self):
        self.assertEqual(sb.check_pins({'pip': '24.0'}, {}, exempt=()), ['installed but not pinned: pip==24.0'])
        self.assertEqual(sb.check_pins({'pip-tools': '7.0'}, {}), ['installed but not pinned: pip-tools==7.0'])


class BaseImageTests(unittest.TestCase):
    def test_the_real_dockerfiles(self):
        live = sb.base_images((ROOT / 'deploy/live/Dockerfile').read_text(encoding='utf-8'))
        api = sb.base_images((ROOT / 'deploy/api/Dockerfile').read_text(encoding='utf-8'))
        self.assertEqual([(c['name'], c['version']) for c in live], [('python', '3.11-slim')])
        self.assertEqual(sorted(c['name'] for c in api), ['node', 'python'])
        for component in live + api:
            self.assertRegex(component['hashes'][0]['content'], r'^[0-9a-f]{64}$')
            self.assertEqual(component['type'], 'container')
            self.assertIn(component['hashes'][0]['content'], component['bom-ref'])
            self.assertEqual(props(component), {'ava:origin': 'base-image', 'ava:source': 'dockerfile'})

    def test_stages_registries_and_refusals(self):
        text = (f'FROM registry.local:5000/team/base@sha256:{DIGEST} AS build\n'
                'RUN true\n'
                'FROM build\n'
                f'from python:3.11-slim@sha256:{"d" * 64}\n'
                f'FROM python:3.11-slim@sha256:{"d" * 64} AS again\n')
        got = sb.base_images(text)
        self.assertEqual([(c['name'], c.get('version')) for c in got],
                         [('python', '3.11-slim'), ('registry.local:5000/team/base', None)])
        for bad, message in ((f'FROM python:3.11-slim\n', 'without a sha256 digest'),
                             (f'FROM python@sha256:{"c" * 63}\n', 'without a sha256 digest'),
                             ('FROM a b c d\n', 'unreadable FROM'),
                             ('RUN true\n', 'no FROM image')):
            with self.subTest(text=bad), self.assertRaisesRegex(sb.SbomError, message):
                sb.base_images(bad)


class DebianComponentTests(unittest.TestCase):
    def test_installed_packages_with_encoded_versions(self):
        got = sb.deb_components(DPKG, OS_RELEASE)
        self.assertEqual([c['purl'] for c in got], [
            'pkg:deb/debian/libc6@2.36-9%2Bdeb12u10?arch=amd64&distro=debian-12',
            'pkg:deb/debian/libgcc-s1@1%3A12.2.0-14%2Bdeb12u1?arch=amd64&distro=debian-12',
            # A held package is installed; '+' in the name is encoded like the version's.
            'pkg:deb/debian/libstdc%2B%2B6@12.2.0-14?arch=amd64&distro=debian-12',
        ])
        self.assertEqual(got[1]['version'], '1:12.2.0-14+deb12u1')
        self.assertEqual(got[2]['name'], 'libstdc++6')

    def test_windows_line_endings_read_the_same(self):
        self.assertEqual(sb.deb_components(DPKG.replace('\n', '\r\n'), OS_RELEASE), sb.deb_components(DPKG, OS_RELEASE))

    def test_refusals(self):
        cases = {
            'no os id': (DPKG, 'NAME=x\n', 'os-release'),
            'nothing installed': ('Package: a\nStatus: deinstall ok config-files\nArchitecture: all\nVersion: 1\n\n'
                                  'Package: b\nStatus: install reinstreq half-installed\nArchitecture: all\n'
                                  'Version: 1\n\nPackage: c\nStatus: purge ok not-installed\nArchitecture: all\n'
                                  'Version: 1\n',
                                  OS_RELEASE, 'no installed package'),
            'twice': (DPKG + '\n' + DPKG.split('\n\n')[0] + '\n', OS_RELEASE, 'listed twice'),
            'bad name': ('Package: Bad Name\nStatus: install ok installed\nArchitecture: amd64\nVersion: 1\n',
                         OS_RELEASE, 'unreadable dpkg entry'),
            'no version': ('Package: a1\nStatus: install ok installed\nArchitecture: amd64\n',
                           OS_RELEASE, 'unreadable dpkg entry'),
        }
        for name, (status, release, message) in cases.items():
            with self.subTest(case=name), self.assertRaisesRegex(sb.SbomError, message):
                sb.deb_components(status, release)


class NpmComponentTests(unittest.TestCase):
    def test_the_real_dashboard_lockfile(self):
        lock = json.loads((ROOT / 'dashboard/package-lock.json').read_text(encoding='utf-8'))
        got = {c['purl']: c for c in sb.npm_components(lock)}
        paths = [p for p in lock['packages'] if p]
        self.assertEqual(len(got), len({(p.rsplit('node_modules/', 1)[1], lock['packages'][p]['version'])
                                        for p in paths}))
        react = got['pkg:npm/react@18.3.1']
        self.assertEqual(react['scope'], 'required')
        expected = base64.b64decode(lock['packages']['node_modules/react']['integrity'][7:]).hex()
        self.assertEqual(react['hashes'], [{'alg': 'SHA-512', 'content': expected}])
        self.assertEqual(len(expected), 128)
        self.assertEqual(react['licenses'], [{'expression': 'MIT'}])
        self.assertEqual(got['pkg:npm/vite@5.4.11']['scope'], 'excluded')
        types = got['pkg:npm/%40types/react@18.3.12']
        self.assertEqual((types['group'], types['name'], types['scope']), ('@types', 'react', 'excluded'))
        # Everything the bundle ships at run time is required; react-dom is one of them.
        self.assertEqual(got[next(p for p in got if p.startswith('pkg:npm/react-dom@'))]['scope'], 'required')

    def test_integrity_known_answer_and_refusals(self):
        raw = hashlib.sha512(b'ava').digest()
        integrity = 'sha512-' + base64.b64encode(raw).decode()
        lock = {'lockfileVersion': 3, 'packages': {
            '': {'name': 'root'},
            'node_modules/a': {'version': '1.0.0', 'integrity': integrity, 'optional': True},
            'node_modules/x/node_modules/a': {'version': '1.0.0', 'integrity': integrity},
            'node_modules/linked': {'link': True, 'resolved': '../linked'},
            # devOptional: in the production tree as an optional dependency (npm docs).
            'node_modules/b': {'version': '2.0.0', 'devOptional': True},
            # dev + optional: an optional dependency of a dev dependency, build-only.
            'node_modules/c': {'version': '3.0.0', 'dev': True, 'optional': True},
            # An alias folder: the purl names the real package.
            'node_modules/foo': {'name': 'bar', 'version': '4.0.0'},
        }}
        got = sb.npm_components(lock)
        self.assertEqual([(c['purl'], c['scope']) for c in got], [
            ('pkg:npm/a@1.0.0', 'required'), ('pkg:npm/b@2.0.0', 'optional'), ('pkg:npm/bar@4.0.0', 'required'),
            ('pkg:npm/c@3.0.0', 'excluded')])
        self.assertEqual(got[0]['hashes'], [{'alg': 'SHA-512', 'content': raw.hex()}])
        self.assertNotIn('hashes', got[1])
        for component in got:
            self.assertEqual(props(component), {'ava:source': 'lockfile'})
        bad = {
            'v1': ({'lockfileVersion': 1, 'dependencies': {}}, 'lockfileVersion'),
            'no map': ({'lockfileVersion': 3}, 'no packages map'),
            'short hash': ({'lockfileVersion': 3, 'packages': {'node_modules/a': {
                'version': '1', 'integrity': 'sha512-' + base64.b64encode(b'x' * 10).decode()}}}, '10 bytes'),
            'not base64': ({'lockfileVersion': 3, 'packages': {'node_modules/a': {
                'version': '1', 'integrity': 'sha512-***'}}}, 'not base64'),
            'md5': ({'lockfileVersion': 3, 'packages': {'node_modules/a': {
                'version': '1', 'integrity': 'md5-AAAA'}}}, 'unsupported integrity'),
            'outside': ({'lockfileVersion': 3, 'packages': {'vendor/a': {'version': '1'}}}, 'outside node_modules'),
            'no version': ({'lockfileVersion': 3, 'packages': {'node_modules/a': {}}}, 'unreadable npm'),
            'bad alias name': ({'lockfileVersion': 3, 'packages': {'node_modules/a': {
                'name': '../x', 'version': '1'}}}, 'unreadable npm'),
            'two hashes': ({'lockfileVersion': 3, 'packages': {
                'node_modules/a': {'version': '1', 'integrity': integrity},
                'node_modules/b/node_modules/a': {'version': '1', 'integrity': 'sha512-' + base64.b64encode(
                    hashlib.sha512(b'other').digest()).decode()}}}, 'two different integrity'),
        }
        for name, (lock, message) in bad.items():
            with self.subTest(case=name), self.assertRaisesRegex(sb.SbomError, message):
                sb.npm_components(lock)


class DocumentTests(unittest.TestCase):
    def components(self):
        return [sb.python_runtime('CPython', '3.11.14')] + sb.deb_components(DPKG, OS_RELEASE)

    def test_structure_and_determinism(self):
        first = sb.build_bom('ava-live', IMAGE_ID, self.components())
        again = sb.build_bom('ava-live', IMAGE_ID, self.components())
        self.assertEqual(first, again)
        self.assertEqual((first['bomFormat'], first['specVersion'], first['version']), ('CycloneDX', '1.5', 1))
        self.assertRegex(first['serialNumber'], SERIAL)
        self.assertNotIn('timestamp', first['metadata'])
        self.assertEqual(first['metadata']['component']['hashes'], [{'alg': 'SHA-256', 'content': 'ab' * 32}])
        self.assertEqual(first['metadata']['component']['bom-ref'], f'image:ava-live@{IMAGE_ID}')
        changed = sb.build_bom('ava-live', IMAGE_ID, self.components()[:-1])
        self.assertNotEqual(changed['serialNumber'], first['serialNumber'])
        other_image = sb.build_bom('ava-live', 'sha256:' + 'cd' * 32, self.components())
        self.assertNotEqual(other_image['serialNumber'], first['serialNumber'])
        stamped = sb.build_bom('ava-live', IMAGE_ID, self.components(), '2026-11-21T08:00:00+05:00')
        self.assertEqual(stamped['metadata']['timestamp'], '2026-11-21T08:00:00+05:00')
        self.assertNotEqual(stamped['serialNumber'], first['serialNumber'])
        self.assertNotIn('properties', first['metadata'])
        revised = sb.build_bom('ava-live', IMAGE_ID, self.components(), source_revision='71cfd7a+dirty')
        self.assertEqual(revised['metadata']['properties'], [{'name': 'ava:source-revision', 'value': '71cfd7a+dirty'}])
        self.assertNotEqual(revised['serialNumber'], first['serialNumber'])

    def test_refusals(self):
        with self.assertRaisesRegex(sb.SbomError, 'duplicate component'):
            sb.build_bom('ava-live', IMAGE_ID, self.components() * 2)
        for name, image_id in (('Ava-Live', IMAGE_ID), ('ava live', IMAGE_ID), ('ava-live', 'ab' * 32),
                               ('ava-live', 'sha256:' + 'AB' * 32)):
            with self.subTest(name=name, image_id=image_id), self.assertRaises(sb.SbomError):
                sb.build_bom(name, image_id, [])
        for revision in ('71cfd7', 'HEAD', '71CFD7A', '71cfd7a-dirty', 'a' * 41):
            with self.subTest(revision=revision), self.assertRaisesRegex(sb.SbomError, 'source revision'):
                sb.build_bom('ava-live', IMAGE_ID, [], source_revision=revision)

    def test_the_output_validates_against_the_cyclonedx_schema(self):
        try:
            from cyclonedx.schema import SchemaVersion
            from cyclonedx.validation.json import JsonStrictValidator
        except ImportError:
            self.skipTest('cyclonedx-python-lib[json-validation] is not installed')
        lock = json.loads((ROOT / 'dashboard/package-lock.json').read_text(encoding='utf-8'))
        with tempfile.TemporaryDirectory() as folder:
            make_dist(folder, 'torch', '2.8.0+cpu', extra='License-Expression: BSD-3-Clause\n')
            make_dist(folder, 'pip', '24.0', extra='Classifier: License :: OSI Approved :: MIT License\n')
            python = sb.python_components(dists(folder), {'torch': '2.8.0+cpu'})
        components = (sb.base_images((ROOT / 'deploy/api/Dockerfile').read_text(encoding='utf-8')) + python
                      + self.components() + sb.npm_components(lock))
        bom = sb.build_bom('ava-api', IMAGE_ID, components, '2026-11-21T03:00:00Z', '71cfd7a+dirty')
        validator = JsonStrictValidator(SchemaVersion.V1_5)
        error = validator.validate_str(json.dumps(bom))
        self.assertIsNone(error, str(error))
        # Control: the validator does reject a broken document.
        broken = dict(bom, components=[dict(components[0], type='not-a-type')] + components[1:])
        self.assertIsNotNone(validator.validate_str(json.dumps(broken)))


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        base = Path(self.folder.name)
        self.site = base / 'site'
        self.site.mkdir()
        make_dist(self.site, 'numpy', '2.3.5')
        make_dist(self.site, 'pip', '24.0')
        (base / 'requirements-a.txt').write_text('numpy==2.3.5\n', encoding='utf-8')
        (base / 'requirements-image.txt').write_text('-r requirements-a.txt\n', encoding='utf-8')
        (base / 'Dockerfile').write_text(f'FROM python:3.11-slim@sha256:{DIGEST}\n', encoding='utf-8')
        (base / 'status').write_text(DPKG, encoding='utf-8')
        (base / 'os-release').write_text(OS_RELEASE, encoding='utf-8')
        self.base = base

    def run_main(self, *extra, site=None):
        b = self.base
        argv = ['--image-name', 'ava-test', '--image-id', IMAGE_ID, '--dockerfile', str(b / 'Dockerfile'),
                '--requirements', str(b / 'requirements-image.txt'), '--root', str(b),
                '--dpkg-status', str(b / 'status'), '--os-release', str(b / 'os-release'), *extra]
        out, err = io.StringIO(), io.StringIO()
        code = sb.main(argv, distributions=dists(site or self.site), stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue()

    def test_a_matching_image_writes_the_sbom_and_exits_0(self):
        code, out, err = self.run_main('--check-pins', '--npm-lock', str(ROOT / 'dashboard/package-lock.json'))
        self.assertEqual(code, 0, err)
        bom = json.loads(out)
        kinds = {c.get('purl', 'container').split('/')[0] for c in bom['components']}
        self.assertEqual(kinds, {'container', 'pkg:pypi', 'pkg:generic', 'pkg:deb', 'pkg:npm'})
        self.assertIn('pins match', err)
        self.assertTrue(out.endswith('\n'))

    def test_a_pin_mismatch_still_writes_the_sbom_and_exits_1(self):
        make_dist(self.site, 'requests', '2.0.0')
        code, out, err = self.run_main('--check-pins')
        self.assertEqual(code, 1)
        self.assertIn('pkg:pypi/requests@2.0.0', out)
        self.assertIn('installed but not pinned: requests==2.0.0', err)
        self.assertIn('pins DIFFER', err)
        # Without --check-pins the same image exits 0 and says pins were not checked.
        code, _, err = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn('pins not checked', err)

    def test_refused_input_writes_nothing_and_exits_2(self):
        (self.base / 'requirements-a.txt').write_text('numpy>=2\n', encoding='utf-8')
        code, out, err = self.run_main()
        self.assertEqual((code, out), (2, ''))
        self.assertIn('not an exact pin', err)
        (self.base / 'requirements-a.txt').write_bytes('numpy==2.3.5  # caf\xe9\n'.encode('latin-1'))
        code, out, err = self.run_main('--check-pins')
        self.assertEqual((code, out), (2, ''))  # never 1, which means "pins differ"
        self.assertIn('requirement file requirements-a.txt is not UTF-8', err)
        (self.base / 'requirements-a.txt').write_text('numpy==2.3.5\n', encoding='utf-8')
        rfc = 'RFC 3339'
        for extra, message in ((('--timestamp', '2026-11-21T08:00:00'), rfc),
                               (('--timestamp', 'yesterday'), rfc),
                               (('--timestamp', '2026-11-21 08:00:00+05:00'), rfc),
                               (('--timestamp', '20261121T080000+0500'), rfc),
                               (('--timestamp', '2026-02-30T08:00:00Z'), rfc),
                               (('--source-revision', 'main'), 'source revision'),
                               (('--requirements', str(self.base / 'nowhere.txt')),
                                'cannot read requirement file nowhere.txt'),
                               (('--npm-lock', str(self.base / 'missing.json')), 'cannot read npm lockfile'),
                               (('--npm-lock', str(self.base / 'status')), 'not JSON')):
            with self.subTest(extra=extra):
                code, out, err = self.run_main(*extra)
                self.assertEqual((code, out), (2, ''))
                self.assertIn(message, err)

    def test_timestamp_and_revision_are_recorded(self):
        code, out, err = self.run_main('--timestamp', '2026-11-21T03:00:00.5Z', '--source-revision', 'a' * 40)
        self.assertEqual(code, 0, err)
        meta = json.loads(out)['metadata']
        self.assertEqual(meta['timestamp'], '2026-11-21T03:00:00.5Z')
        self.assertEqual(meta['properties'], [{'name': 'ava:source-revision', 'value': 'a' * 40}])

    def test_skipping_debian_packages(self):
        code, out, _ = self.run_main('--dpkg-status', '')
        self.assertEqual(code, 0)
        self.assertNotIn('pkg:deb/', out)

    def test_an_output_file_is_created_and_never_overwritten(self):
        target = self.base / 'sbom.json'
        code, out, _ = self.run_main('--output', str(target))
        self.assertEqual((code, out), (0, ''))
        first = target.read_bytes()
        self.assertEqual(json.loads(first)['metadata']['component']['name'], 'ava-test')
        self.assertNotIn(b'\r\n', first)
        code, _, err = self.run_main('--output', str(target), '--timestamp', '2026-11-21T03:00:00Z')
        self.assertEqual(code, 2)
        self.assertIn('never overwritten', err)
        self.assertEqual(target.read_bytes(), first)

    def test_the_same_inputs_give_identical_bytes(self):
        self.assertEqual(self.run_main()[1], self.run_main()[1])


if __name__ == '__main__':
    unittest.main()
