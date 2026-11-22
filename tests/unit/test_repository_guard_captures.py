"""Claude-authored tests (15 November 2026): the repository guard blocks packet
captures and raw telephony audio (lab review M2, backlog 6.1). Authored by Claude
(Claude Code), not by GPT-6 Astra; the earlier guard cases in
test_foundation_tools.py keep their provenance.
"""
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]


def load_guard():
    spec = importlib.util.spec_from_file_location('repository_guard', ROOT / 'scripts' / 'repository_guard.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CaptureGuardTests(unittest.TestCase):
    def test_captures_and_raw_telephony_audio_are_blocked_at_any_size(self):
        guard = load_guard()
        for name in ('lab/io/inject.pcap', 'call.PCAPNG', 'x.cap', 'caller.ul', 'agent.al', 'a.ulaw', 'b.alaw',
                     'c.sln', 'c.sln16', 'd.g711', 'e.g722', 'f.opus', 'g.raw'):
            with self.subTest(name=name):
                self.assertEqual(guard.reason(name, 10), 'audio, model or key artifact')

    def test_code_and_config_with_similar_names_pass(self):
        guard = load_guard()
        for name in ('lab/asterisk/rtp_pcap.py', 'lab/asterisk/config/pjsip.conf', 'docs/capture.md',
                     'lab/asterisk/sipp/uac_inject.xml'):
            with self.subTest(name=name):
                self.assertIsNone(guard.reason(name, 10))


if __name__ == '__main__':
    unittest.main()
