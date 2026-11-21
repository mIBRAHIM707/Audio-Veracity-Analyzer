"""Claude-authored lab test (21 November 2026): `scripts/image_sbom.py` inside the real
deployment images (backlog 10.2; PRD FR-O7 "SBOM generated in CI").

Authored by Claude (Claude Code), not by GPT-6 Astra. Opt-in (AVA_TEST_DOCKER_LAB=1);
needs the locally built `ava-live:dev` and `ava-api:dev` (build them from this tree first:
the pin check compares the image with the checked-out requirement files) and the base
image `python:3.11-slim` at the Dockerfiles' digest. Each run is `--network none`,
read-only, with every capability dropped, as the image's default user (uid 10001 in the
AVA images; root in the base image, which only lists its pip packages), with the
repository mounted read-only where the generator runs. For each image it checks:
- the generator exits 0 with `--check-pins`: the installed Python set equals the pins;
- the Python components equal pip's own listing in the same image, and the Debian
  components equal `dpkg-query`'s installed list (independent oracles);
- the unpinned base tools (pip, setuptools, wheel) have the same versions as in the base
  image at the pinned digest, so the exemption hides nothing the build added;
- the image ID and the base image digest are recorded; two runs give identical bytes;
- the API image's SBOM lists the dashboard's npm packages (react required, vite excluded).
Negative: the live image checked against the API image's requirement file exits 1 and
names the missing torch pin, and still writes the SBOM.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import unittest

from scripts import image_sbom as sb

LAB = unittest.skipIf(os.environ.get('AVA_TEST_DOCKER_LAB') != '1', 'set AVA_TEST_DOCKER_LAB=1')
ROOT = Path(__file__).resolve().parents[2]
IMAGES = {
    'ava-live': ('ava-live:dev', 'deploy/live/Dockerfile', 'deploy/live/requirements-live.txt', None),
    'ava-api': ('ava-api:dev', 'deploy/api/Dockerfile', 'deploy/api/requirements-api-image.txt',
                'dashboard/package-lock.json'),
}
RUN = ['docker', 'run', '--rm', '--network', 'none', '--read-only', '--cap-drop', 'ALL',
       '--security-opt', 'no-new-privileges', '--pull', 'never']
TIMEOUT_S = 180


def docker(*args, check=True):
    result = subprocess.run(['docker', *args], capture_output=True, text=True, encoding='utf-8',
                            timeout=TIMEOUT_S)
    if check and result.returncode != 0:
        raise AssertionError(f'docker {args[0]} failed ({result.returncode}): {result.stderr[-2000:]}')
    return result


def image_id(tag):
    return docker('image', 'inspect', tag, '--format', '{{.Id}}').stdout.strip()


def in_image(tag, *command, mount=False):
    args = [*RUN, *(['-v', f'{ROOT}:/src:ro'] if mount else []), '--entrypoint', command[0], tag, *command[1:]]
    return subprocess.run(args, capture_output=True, text=True, encoding='utf-8', timeout=TIMEOUT_S)


def source_revision():
    """HEAD of this checkout, with +dirty when the tree has changes (as the README says)."""
    def git(*args):
        return subprocess.run(['git', *args], cwd=ROOT, capture_output=True, text=True, check=True,
                              timeout=30).stdout
    return git('rev-parse', 'HEAD').strip() + ('+dirty' if git('status', '--porcelain').strip() else '')


def generate(name, requirements=None):
    tag, dockerfile, own_requirements, npm_lock = IMAGES[name]
    command = ['python', '-I', '/src/scripts/image_sbom.py', '--image-name', name,
               '--image-id', image_id(tag), '--dockerfile', f'/src/{dockerfile}',
               '--requirements', f'/src/{requirements or own_requirements}', '--root', '/src', '--check-pins',
               '--source-revision', source_revision()]
    if npm_lock:
        command += ['--npm-lock', f'/src/{npm_lock}']
    return in_image(tag, *command, mount=True)


def pip_listing(tag):
    result = in_image(tag, 'python', '-I', '-m', 'pip', 'list', '--format', 'json', '--disable-pip-version-check')
    if result.returncode != 0:
        raise AssertionError(result.stderr[-2000:])
    return {sb.normalise(p['name']): p['version'] for p in json.loads(result.stdout)}


def base_image_ref(dockerfile):
    text = (ROOT / dockerfile).read_text(encoding='utf-8')
    return re.findall(r'^FROM (python:3\.11-slim@sha256:[0-9a-f]{64})', text, re.MULTILINE)[-1]


@LAB
class ImageSbomLabTests(unittest.TestCase):
    def check_image(self, name):
        tag, dockerfile, requirements, npm_lock = IMAGES[name]
        result = generate(name)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('pins match', result.stderr)
        bom = json.loads(result.stdout)
        self.assertEqual(bom['metadata']['component']['hashes'][0]['content'], image_id(tag).split(':', 1)[1])
        self.assertEqual(bom['metadata']['properties'], [{'name': 'ava:source-revision', 'value': source_revision()}])
        base = base_image_ref(dockerfile)
        containers = [c for c in bom['components'] if c['type'] == 'container']
        self.assertIn(base.split('@sha256:')[1], [c['hashes'][0]['content'] for c in containers])
        for component in containers:
            self.assertIn({'name': 'ava:source', 'value': 'dockerfile'}, component['properties'])

        # Python components = pip's own listing in the same image.
        python = {sb.normalise(c['name']): c['version'] for c in bom['components']
                  if c.get('purl', '').startswith('pkg:pypi/')}
        self.assertEqual(python, pip_listing(tag))
        pins, _ = sb.read_pins(ROOT / requirements, ROOT)
        self.assertEqual(set(python) - set(pins), set(sb.BASE_IMAGE_TOOLS))

        # Unpinned base tools are exactly the base image's.
        base_pip = pip_listing(base)
        for tool in sb.BASE_IMAGE_TOOLS:
            self.assertEqual(python[tool], base_pip[tool], tool)

        # Debian components = dpkg-query's installed packages.
        listed = in_image(tag, 'dpkg-query', '-W', '-f', '${db:Status-Status} ${Package} ${Version}\\n')
        self.assertEqual(listed.returncode, 0, listed.stderr)
        installed = {tuple(line.split(' ')[1:3]) for line in listed.stdout.splitlines()
                     if line.startswith('installed ')}
        debs = {(c['name'], c['version']) for c in bom['components'] if c.get('purl', '').startswith('pkg:deb/')}
        self.assertEqual(debs, installed)
        self.assertGreater(len(debs), 50)

        runtime = [c for c in bom['components'] if c.get('purl', '').startswith('pkg:generic/cpython@3.11.')]
        self.assertEqual(len(runtime), 1)

        # Deterministic: a second run gives the same bytes.
        self.assertEqual(generate(name).stdout, result.stdout)
        return bom

    def test_the_live_image(self):
        bom = self.check_image('ava-live')
        self.assertFalse([c for c in bom['components'] if c.get('purl', '').startswith('pkg:npm/')])
        names = {c['name'] for c in bom['components']}
        self.assertTrue({'onnxruntime', 'psycopg', 'cryptography'} <= names)
        self.assertNotIn('torch', names)  # serving needs no torch

    def test_the_api_image(self):
        bom = self.check_image('ava-api')
        npm = {c['name']: c for c in bom['components'] if c.get('purl', '').startswith('pkg:npm/')}
        self.assertEqual(npm['react']['scope'], 'required')
        self.assertEqual(npm['vite']['scope'], 'excluded')
        for component in npm.values():
            self.assertEqual(component['properties'], [{'name': 'ava:source', 'value': 'lockfile'}])
        torch = next(c for c in bom['components'] if c['name'] == 'torch')
        self.assertTrue(torch['purl'].endswith('%2Bcpu'), torch['purl'])

    def test_a_mismatched_requirement_file_fails_the_check_but_keeps_the_sbom(self):
        result = generate('ava-live', requirements=IMAGES['ava-api'][2])
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertRegex(result.stderr, r'pin check: pinned but not installed: torch==2\.8\.0\+cpu')
        self.assertIn('pins DIFFER', result.stderr)
        self.assertEqual(json.loads(result.stdout)['bomFormat'], 'CycloneDX')


if __name__ == '__main__':
    unittest.main()
