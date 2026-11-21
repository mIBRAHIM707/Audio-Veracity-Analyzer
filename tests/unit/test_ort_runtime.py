"""ONNX Runtime is always imported with telemetry off (PRD: no telemetry;
services/inference/ort_runtime.py).

Claude-authored test design, 17 November 2026. A stand-in `onnxruntime` module checks
the helper sets ORT_DISABLE_TELEMETRY=1 before the import (even over another value) and
calls disable_telemetry_events(); a source scan keeps every production import going
through the helper. The real effect was observed in the Linux image (no 1DS
initialisation message with the variable set). Runs without onnxruntime installed.
"""
import os
from pathlib import Path
import re
import sys
import types
import unittest
from unittest import mock

from services.inference import ort_runtime

ROOT = Path(__file__).resolve().parents[2]
DIRECT_IMPORT = re.compile(r'^(\s*)(import onnxruntime\b|from onnxruntime\b)', re.MULTILINE)
ALLOWED = {Path('services/inference/ort_runtime.py')}
HELPER_CALL = 'ort_runtime.onnxruntime()'


class OrtRuntimeTests(unittest.TestCase):
    def fake_module(self, seen):
        module = types.ModuleType('onnxruntime')
        module.__version__ = 'fake'
        module.disable_telemetry_events = lambda: seen.append(('disabled', os.environ.get('ORT_DISABLE_TELEMETRY')))
        return module

    def test_variable_is_forced_before_import_and_events_disabled(self):
        seen = []
        module = self.fake_module(seen)
        real_import = __import__

        def importer(name, *args, **kwargs):
            if name == 'onnxruntime':
                seen.append(('import', os.environ.get('ORT_DISABLE_TELEMETRY')))
                return module
            return real_import(name, *args, **kwargs)
        with mock.patch.dict(os.environ, {'ORT_DISABLE_TELEMETRY': '0'}), \
                mock.patch.dict(sys.modules, {'onnxruntime': module}), \
                mock.patch('builtins.__import__', importer):
            self.assertIs(ort_runtime.onnxruntime(), module)
            self.assertEqual(os.environ['ORT_DISABLE_TELEMETRY'], '1')
        self.assertEqual(seen, [('import', '1'), ('disabled', '1')])

    def test_a_runtime_without_the_switch_still_imports(self):
        module = types.ModuleType('onnxruntime')
        with mock.patch.dict(os.environ, {}), mock.patch.dict(sys.modules, {'onnxruntime': module}):
            self.assertIs(ort_runtime.onnxruntime(), module)

    def test_code_and_tests_import_onnxruntime_only_through_the_helper(self):
        # Review L1: tests are scanned too (a model test run would otherwise start the
        # telemetry), and a submodule import is allowed only inside a function after the
        # helper ran there: never at module level, where it would initialise ONNX Runtime.
        offenders = []
        for folder in ('services', 'models', 'data', 'lab', 'scripts', 'tests'):
            for path in sorted((ROOT / folder).rglob('*.py')):
                relative = path.relative_to(ROOT)
                if relative in ALLOWED:
                    continue
                text = path.read_text(encoding='utf-8', errors='replace')
                for match in DIRECT_IMPORT.finditer(text):
                    line = text[match.start():text.find('\n', match.start())].strip()
                    indented = bool(match.group(1))
                    before = text[:match.start()]
                    function_start = before.rfind('\n    def ') if indented else -1
                    function_start = max(function_start, before.rfind('\ndef '))
                    helper_first = HELPER_CALL in before[function_start:] if function_start >= 0 else False
                    if not (line.startswith('from onnxruntime.') and indented and helper_first):
                        offenders.append(f'{relative}: {line}')
        self.assertEqual(offenders, [], 'import onnxruntime through services.inference.ort_runtime')


if __name__ == '__main__':
    unittest.main()
