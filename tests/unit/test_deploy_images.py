"""Claude-authored tests (17 November 2026): the pinning rules of the deployment images
(backlog 10.2; PRD FR-O7 "images pinned by digest", partial: versions without hashes).

Authored by Claude (Claude Code), not by GPT-6 Astra. Pure file checks, no Docker:
- every FROM in deploy/*/Dockerfile names a sha256 digest;
- each image's requirement file, read the way the image reads it (the Dockerfile copies
  the root `requirements-*.txt` and the image file into one folder, so `-r` names are
  repository-root files), pins every package exactly, never twice with different
  versions, and allows no index other than the PyTorch CPU one;
- pip installs that set with --no-deps --only-binary=:all: followed by `pip check`, so a
  build cannot pull an unpinned package or compile from source.
The builds themselves are evidence in tests/lab/test_compose_stack_lab.py.
21 Nov 2026: the reader moved to scripts/image_sbom.py (`read_pins`: the same pin rules,
plus a size limit and refusal of unreadable or non-UTF-8 files), so
the SBOM pin check and these tests read the image files the same way.
"""
from pathlib import Path
import re
import tempfile
import unittest

from scripts.image_sbom import ALLOWED_OPTIONS, read_pins

ROOT = Path(__file__).resolve().parents[2]
IMAGES = {  # Dockerfile -> the requirement file it installs
    'deploy/live/Dockerfile': 'deploy/live/requirements-live.txt',
    'deploy/api/Dockerfile': 'deploy/api/requirements-api-image.txt',
}


class ImagePinTests(unittest.TestCase):
    def test_every_base_image_is_pinned_by_digest(self):
        for dockerfile in IMAGES:
            froms = [line for line in (ROOT / dockerfile).read_text(encoding='utf-8').splitlines()
                     if line.upper().startswith('FROM ')]
            self.assertTrue(froms, dockerfile)
            for line in froms:
                self.assertRegex(line, r'^FROM \S+@sha256:[0-9a-f]{64}( AS \w+)?$', f'{dockerfile}: {line}')

    def test_each_image_installs_an_exact_conflict_free_set(self):
        for dockerfile, requirements in IMAGES.items():
            with self.subTest(image=dockerfile):
                pins, options = read_pins(ROOT / requirements, ROOT)
                self.assertGreater(len(pins), 5)
                self.assertLessEqual(options, ALLOWED_OPTIONS)
                text = (ROOT / dockerfile).read_text(encoding='utf-8')
                install = re.search(r'pip install ([^\n]*)', text)
                self.assertIsNotNone(install, dockerfile)
                for flag in ('--no-deps', '--only-binary=:all:', '/tmp/requirements/' + Path(requirements).name):
                    self.assertIn(flag, install.group(1), dockerfile)
                self.assertIn('&& pip check', text, dockerfile)
                # The Dockerfile copies the root requirement files and the image file side by side.
                self.assertIn(f'COPY requirements-*.txt {requirements} /tmp/requirements/', text, dockerfile)

    def test_the_api_image_pins_torch_from_the_cpu_index_only(self):
        pins, options = read_pins(ROOT / IMAGES['deploy/api/Dockerfile'], ROOT)
        self.assertTrue(pins['torch'].endswith('+cpu'), pins['torch'])
        self.assertTrue(pins['torchaudio'].endswith('+cpu'), pins['torchaudio'])
        self.assertEqual(options, ALLOWED_OPTIONS)
        # What the API process imports at start-up and at model load (services/api,
        # services/inference/baseline.py, models/smoke/antideepfake.py).
        # 20 Nov: psycopg (with its bundled libpq) for the history routes and db-setup.
        for package in ('fastapi', 'uvicorn', 'websockets', 'python-multipart', 'redis', 'numpy', 'soxr',
                        'safetensors', 'psutil', 'psycopg', 'psycopg-binary'):
            self.assertIn(package, pins)

    def test_the_reader_refuses_what_the_image_rules_forbid(self):
        cases = {
            'loose': ('numpy>=2\n', 'not an exact pin'),
            'bare': ('numpy\n', 'not an exact pin'),
            'conflict': ('numpy==2.3.5\nNumPy==2.3.4\n', 'pinned to'),
            'index': ('--index-url https://example.invalid/simple\nnumpy==2.3.5\n', 'option not allowed'),
            'missing': ('-r requirements-nowhere.txt\n', 'included file not found'),
        }
        with tempfile.TemporaryDirectory() as folder:
            for name, (text, message) in cases.items():
                with self.subTest(case=name):
                    path = Path(folder) / f'requirements-{name}.txt'
                    path.write_text(text, encoding='utf-8')
                    with self.assertRaisesRegex(ValueError, message):
                        read_pins(path, folder)
            # A conflict between an included file and the including one is found too.
            (Path(folder) / 'requirements-base.txt').write_text('soxr==1.1.0\n', encoding='utf-8')
            top = Path(folder) / 'requirements-top.txt'
            top.write_text('-r requirements-base.txt\nsoxr==1.0.0\n', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'soxr pinned to'):
                read_pins(top, folder)
            cycle = Path(folder) / 'requirements-cycle.txt'
            cycle.write_text('-r requirements-cycle.txt\n', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'cycle'):
                read_pins(cycle, folder)
            ok = Path(folder) / 'requirements-ok.txt'
            ok.write_text('# comment\n--extra-index-url https://download.pytorch.org/whl/cpu\n'
                          'torch==2.8.0+cpu  # inline\nJinja2==3.1.6\n', encoding='utf-8')
            self.assertEqual(read_pins(ok, folder), ({'torch': '2.8.0+cpu', 'jinja2': '3.1.6'}, ALLOWED_OPTIONS))


if __name__ == '__main__':
    unittest.main()
