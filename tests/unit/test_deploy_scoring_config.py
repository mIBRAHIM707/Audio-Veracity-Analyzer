"""Claude-authored tests (20 November 2026): the scoring-config file plus reload wired
into the deployments (backlog 7.3; PRD FR-A9 "config file plus reload ... validated per
FR-I4", FR-I4 reloads reach calls started afterwards, FR-O2 ConfigMap; architecture §7.3
defaults).

Authored by Claude (Claude Code), not by GPT-6 Astra. The live process's reload itself is
tested in test_live_config_reload.py; these check that both deployments hand it a file
it accepts and can see change:
- the shipped files are the §7.3 defaults under the real start-up rules, so turning the
  wiring on changes no setting;
- the Compose command parses with the real live argument parser and names a file inside
  a mounted FOLDER (a single-file bind mount keeps the old inode, so a file replaced by
  rename would never be seen);
- (PyYAML) the k3s pod mounts the generated `ava-scoring` ConfigMap without subPath (a
  subPath mount is never updated) and the generator has no hash suffix (a suffixed name
  changes on each edit and rolls the pod, ending its calls).
Behaviour on a real stack: tests/lab/test_compose_stack_lab.py and test_k3s_stack_lab.py.
"""
import contextlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import unittest

from services.live.__main__ import build_parser
from services.scorer.config import ScoringConfig, parse_config, read_json_document

try:
    import yaml
except ImportError:  # pragma: no cover - CI installs only requirements-audio.txt
    yaml = None

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_DIR = ROOT / 'deploy' / 'compose'
COMPOSE = COMPOSE_DIR / 'compose.yaml'
K8S_BASE = ROOT / 'deploy' / 'k8s' / 'base'
SHIPPED = (COMPOSE_DIR / 'config' / 'scoring.json', K8S_BASE / 'scoring.json')
MODEL_WINDOW_S = 4.0  # the window every exported card carries (models/export/onnx_export.py)
INTERPOLATION = re.compile(r'\$\{([A-Z0-9_]+)(?::([-?])([^}]*))?\}')


def compose_value(text):
    """Compose interpolation with an empty environment: `:-` gives its default, `:?`
    (a required setting) a placeholder."""
    def substitute(match):
        _, kind, rest = match.groups()
        return rest if kind == '-' else 'required'
    return INTERPOLATION.sub(substitute, text)


def live_block():
    compose = COMPOSE.read_text(encoding='utf-8')
    match = re.search(r'^  live:\n(.*?)(?=^  [a-z]+:\n|^[a-z]+:)', compose, re.M | re.S)
    if match is None:
        raise AssertionError('no live service in compose.yaml')
    return match.group(1)


def block_items(block, key):
    """The `- item` lines of a list key at four-space indentation (text parse: CI has no
    YAML parser), unquoted, comments dropped."""
    match = re.search(rf'^    {key}:\n((?:      .*\n|\s*#.*\n)+)', block, re.M)
    if match is None:
        raise AssertionError(f'live has no {key}')
    items = []
    for line in match.group(1).splitlines():
        item = re.match(r'^      - (.*)$', line)
        if item:
            value = re.sub(r'\s+#.*$', '', item.group(1)).strip()
            items.append(value[1:-1] if value[:1] == value[-1:] == '"' else value)
    return items


class ShippedDefaultsTests(unittest.TestCase):
    def test_both_files_are_the_section_7_3_defaults_under_the_start_up_rules(self):
        for path in SHIPPED:
            document = read_json_document(str(path))  # the hardened reader live uses
            self.assertEqual(parse_config(document, model_window_s=MODEL_WINDOW_S), ScoringConfig(), path)
        compose_doc, k8s_doc = (json.loads(path.read_text(encoding='utf-8')) for path in SHIPPED)
        self.assertEqual(compose_doc, k8s_doc, 'one default for both deployments')

    def test_every_setting_is_written_out_so_an_operator_edits_rather_than_guesses(self):
        document = json.loads(SHIPPED[0].read_text(encoding='utf-8'))
        self.assertEqual(set(document), {'audio', 'scoring'})
        self.assertEqual(set(document['audio']), {'window_s', 'hop_s', 'max_concealed_fraction'})
        self.assertEqual(set(document['scoring']), {'ema_alpha', 'min_windows', 'thresholds', 'score_directions',
                                                    'degraded_max_stride_factor', 'degraded_recovery_s'})
        self.assertEqual(set(document['scoring']['thresholds']),
                         {'suspicious_enter', 'alert_enter', 'alert_exit', 'suspicious_exit'})

    def test_the_rules_still_bite_on_a_shipped_file(self):
        """Control: the equality above is not vacuous; a broken edit is refused."""
        document = json.loads(SHIPPED[0].read_text(encoding='utf-8'))
        document['scoring']['thresholds']['alert_exit'] = 0.9
        with self.assertRaises(ValueError):
            parse_config(document, model_window_s=MODEL_WINDOW_S)


class ComposeWiringTests(unittest.TestCase):
    def setUp(self):
        self.block = live_block()

    def parsed(self):
        return build_parser().parse_args([compose_value(item) for item in block_items(self.block, 'command')])

    def test_the_command_turns_the_reload_on_with_the_real_parser(self):
        args = self.parsed()
        self.assertEqual(args.scoring_config, '/config/scoring/scoring.json')
        self.assertEqual(args.scoring_config_watch_s, 10.0)

    def test_the_file_is_inside_a_mounted_folder_read_only(self):
        args = self.parsed()
        mounts = [compose_value(item) for item in block_items(self.block, 'volumes')]
        folder = str(PurePosixPath(args.scoring_config).parent)
        matching = [m for m in mounts if m.split(':')[1:2] == [folder]]
        self.assertEqual(len(matching), 1, mounts)
        host, target, mode = matching[0].rsplit(':', 2)
        self.assertEqual((target, mode), ('/config/scoring', 'ro'))
        self.assertTrue((COMPOSE_DIR / host / PurePosixPath(args.scoring_config).name).resolve()
                        .samefile(SHIPPED[0]), 'the default folder (relative to compose.yaml) holds the shipped file')
        for mount in mounts:
            self.assertFalse(mount.split(':')[1].endswith('.json'), f'a single-file mount misses renames: {mount}')

    def test_the_interval_and_file_name_can_be_overridden_and_bad_values_refused(self):
        command = ' '.join(block_items(self.block, 'command'))
        self.assertIn('${AVA_SCORING_CONFIG_WATCH_S:-10}', command)
        self.assertIn('/config/scoring/${AVA_SCORING_CONFIG_FILE:-scoring.json}', command)
        self.assertIn('${AVA_SCORING_CONFIG_DIR:-./config}:/config/scoring:ro', self.block)
        items = [compose_value(item) for item in block_items(self.block, 'command')]
        index = items.index('--scoring-config-watch-s')
        for bad in ('0', '61', 'ten'):
            items[index + 1] = bad
            with self.assertRaises(SystemExit, msg=bad), contextlib.redirect_stderr(io.StringIO()):
                build_parser().parse_args(items)

    def test_the_env_example_documents_the_overrides(self):
        example = (COMPOSE_DIR / 'stack.env.example').read_text(encoding='utf-8')
        for name in ('AVA_SCORING_CONFIG_DIR', 'AVA_SCORING_CONFIG_FILE', 'AVA_SCORING_CONFIG_WATCH_S'):
            self.assertIn(name, example)


@unittest.skipIf(yaml is None, 'PyYAML missing: install requirements-test.txt')
class K8sWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kustomization = yaml.safe_load((K8S_BASE / 'kustomization.yaml').read_text(encoding='utf-8'))
        [cls.live] = [d for d in yaml.safe_load_all((K8S_BASE / 'live.yaml').read_text(encoding='utf-8'))
                      if d and d['kind'] == 'Deployment']
        [cls.config] = [d for d in yaml.safe_load_all((K8S_BASE / 'config.yaml').read_text(encoding='utf-8'))
                        if d and d['kind'] == 'ConfigMap']

    def test_the_generator_has_a_fixed_name_and_the_shipped_file(self):
        [generator] = [g for g in self.kustomization['configMapGenerator'] if g['name'] == 'ava-scoring']
        self.assertEqual(generator['files'], ['scoring.json'])
        self.assertIs(generator['options']['disableNameSuffixHash'], True,
                      'a hash suffix would roll the pod (and end its calls) on every edit')
        self.assertTrue((K8S_BASE / 'scoring.json').is_file())

    def test_the_pod_mounts_the_config_map_folder_without_sub_path(self):
        spec = self.live['spec']['template']['spec']
        [container] = spec['containers']
        volumes = {v['name']: v for v in spec['volumes']}
        [mount] = [m for m in container['volumeMounts'] if volumes[m['name']].get('configMap', {}).get('name')
                   == 'ava-scoring']
        self.assertNotIn('subPath', mount, 'a subPath ConfigMap mount is never updated')
        self.assertIs(mount.get('readOnly'), True)
        args = container['args']
        path = PurePosixPath(args[args.index('--scoring-config') + 1])
        self.assertEqual(str(path.parent), mount['mountPath'])
        self.assertEqual(path.name, 'scoring.json', 'the ConfigMap key generated from the file name')

    def test_the_interval_resolves_to_a_value_the_parser_accepts(self):
        args = self.live['spec']['template']['spec']['containers'][0]['args']
        ref = args[args.index('--scoring-config-watch-s') + 1]
        self.assertEqual(ref, '$(AVA_SCORING_CONFIG_WATCH_S)')
        value = self.config['data']['AVA_SCORING_CONFIG_WATCH_S']
        parsed = build_parser().parse_args(['--card', 'c', '--onnx', 'o', '--vad-model', 'v', '--ari-url', 'u',
                                            '--ari-user', 'a', '--receiver-host', 'h', '--scoring-config', 's',
                                            '--scoring-config-watch-s', value])
        self.assertEqual(parsed.scoring_config_watch_s, 10.0)


if __name__ == '__main__':
    unittest.main()
