"""Claude-authored static checks (22 November 2026; not GPT-6 Astra) of the FR-O5 alert
snippet wiring in the Compose stack, the k3s manifests and the live image (backlog 11.1;
PRD FR-O5 "default no raw audio stored ... AES-256-GCM at rest, TTL cleanup (default 7
days) ... switching off purges existing snippets").

What a mistake here would cost: retention silently on by default, a key mounted
writable into live, a key job with network access, a key path that differs between the
job and live (every alert's write fails), a snippet folder lost at every restart, or a
pod fsGroup that makes the key group-readable (live then refuses to start). The live
command line is parsed with the real parser, so a flag the parser does not accept fails
here. The running Compose stack is tests/lab/test_compose_stack_lab.py
(`test_retention_on_stores_one_sealed_snippet_per_alert_and_off_purges_them`).

Needs PyYAML (requirements-test.txt); skips without it, like the other deploy checks.
"""
import contextlib
import io
import os
from pathlib import Path
import re
import unittest

try:
    import yaml
except ImportError:  # pragma: no cover - environment without requirements-test.txt
    yaml = None

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / 'deploy' / 'compose' / 'compose.yaml'
ENV_EXAMPLE = ROOT / 'deploy' / 'compose' / 'stack.env.example'
K8S = ROOT / 'deploy' / 'k8s' / 'base'
DOCKERFILE = ROOT / 'deploy' / 'live' / 'Dockerfile'
KEY_DIR, SNIPPET_DIR = '/var/lib/ava/retention-key', '/var/lib/ava/snippets'
KEY_FILE = f'{KEY_DIR}/snippet.key'


def interpolate(value, env=None):
    """Compose `${NAME:-default}` / `${NAME:?message}` with the given variables."""
    env = env or {}

    def replace(match):
        name, op, rest = match.group(1), match.group(2), match.group(3)
        if env.get(name):
            return env[name]
        if op == ':-':
            return rest
        return f'<{name} required>'
    return re.sub(r'\$\{([A-Za-z_][A-Za-z0-9_]*)(:-|:\?)([^}]*)\}', replace, str(value))


@unittest.skipIf(yaml is None, 'PyYAML missing: install requirements-test.txt')
class ComposeRetentionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = yaml.safe_load(COMPOSE.read_text(encoding='utf-8'))
        cls.services, cls.volumes = spec['services'], spec['volumes']

    def assert_job_parses(self, command):
        """The job's command, interpolated off and on, runs: off skips, on reaches the
        path checks (here the container folder does not exist, so it is refused)."""
        import json
        from services.retention import keygen
        cases = [('off', 'skipped')]
        if not Path(KEY_DIR).exists():  # never create a key on a host that has the folder
            cases.append(('on', None))
        for value, expected in cases:
            out = io.StringIO()
            keygen.main([interpolate(arg, {'AVA_RETENTION': value}) for arg in command], stdout=out)
            record = json.loads(out.getvalue())
            if expected:
                self.assertEqual(record, {'event': 'retention_key', 'result': expected})
            else:  # the setting reached the path checks: no such folder on this host
                reason = 'must be absolute' if os.name == 'nt' else 'does not exist'
                self.assertEqual(record['event'], 'refused', record)
                self.assertIn(reason, record['reason'])

    def live_args(self, env=None):
        from services.live.__main__ import build_parser
        command = [interpolate(arg, env) for arg in self.services['live']['command']]
        return build_parser().parse_args(command)

    def test_retention_is_off_by_default_and_the_parser_accepts_both_settings(self):
        args = self.live_args()
        self.assertEqual((args.retention, args.retention_ttl_days), ('off', 7.0), 'FR-O5: default off, 7 days')
        self.assertEqual((args.retention_dir, args.retention_key_file), (SNIPPET_DIR, KEY_FILE))
        on = self.live_args({'AVA_RETENTION': 'on', 'AVA_RETENTION_TTL_DAYS': '2.5'})
        self.assertEqual((on.retention, on.retention_ttl_days, on.retention_key_file), ('on', 2.5, KEY_FILE))
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.live_args({'AVA_RETENTION': 'yes'})  # a misspelt setting is refused, not read as off

    def test_the_default_purges_without_opening_the_key(self):
        from services.live.__main__ import _retention
        if Path(SNIPPET_DIR).resolve().exists():
            self.skipTest('this host has the container snippet folder')
        store, policy = _retention(self.live_args())
        self.assertTrue(store.cleanup_only, 'off with the fixed folder: a purge-only store (FR-O5)')
        self.assertFalse(policy.enabled)

    def test_volumes_key_read_only_in_live_and_writable_only_in_the_job(self):
        live = self.services['live']['volumes']
        self.assertIn(f'snippets:{SNIPPET_DIR}', live)
        self.assertIn(f'retentionkey:{KEY_DIR}:ro', live)
        job = self.services['retention-key']
        self.assertEqual(job['volumes'], [f'retentionkey:{KEY_DIR}'])
        self.assertIn('snippets', self.volumes)
        self.assertIn('retentionkey', self.volumes)
        for name, service in self.services.items():
            for volume in service.get('volumes', []):
                if volume.startswith(('snippets:', 'retentionkey:')):
                    self.assertIn(name, ('live', 'retention-key'), f'{name} must not mount FR-O5 data')

    def test_the_key_job_is_offline_hardened_and_writes_the_path_live_reads(self):
        job = self.services['retention-key']
        self.assertEqual(job['image'], self.services['live']['image'])
        self.assertEqual(job['entrypoint'], ['python', '-m', 'services.retention.keygen'])
        # Review M1: the job gets live's setting, so with off it does nothing.
        self.assertEqual(job['command'], ['--retention', '${AVA_RETENTION:-off}', '--key-file', KEY_FILE])
        self.assertEqual(self.services['live']['command'][self.services['live']['command'].index('--retention') + 1],
                         '${AVA_RETENTION:-off}', 'the job and live read the same setting')
        self.assert_job_parses(job['command'])
        self.assertEqual(job['network_mode'], 'none', 'the key job needs no network')
        self.assertNotIn('networks', job)
        self.assertNotIn('ports', job)
        self.assertNotIn('environment', job)
        self.assertEqual((job['read_only'], job['cap_drop'], job['security_opt'], job['restart']),
                         (True, ['ALL'], ['no-new-privileges:true'], 'no'))
        self.assertEqual(self.services['live']['depends_on']['retention-key'],
                         {'condition': 'service_completed_successfully'})
        self.assertNotIn('profiles', job, 'runs in every stack, like live')

    def test_the_env_example_documents_the_settings_commented_out(self):
        text = ENV_EXAMPLE.read_text(encoding='utf-8')
        self.assertRegex(text, r'(?m)^# AVA_RETENTION=off$')
        self.assertRegex(text, r'(?m)^# AVA_RETENTION_TTL_DAYS=7$')
        self.assertNotRegex(text, r'(?m)^AVA_RETENTION=', 'the example never turns retention on')


@unittest.skipIf(yaml is None, 'PyYAML missing: install requirements-test.txt')
class K8sRetentionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.by_name = {}
        for path in K8S.glob('*.yaml'):
            for doc in yaml.safe_load_all(path.read_text(encoding='utf-8')):
                if doc and 'metadata' in doc:  # not the Kustomization itself
                    cls.by_name[(doc['kind'], doc['metadata']['name'])] = doc
        cls.pod = cls.by_name[('Deployment', 'live')]['spec']['template']['spec']
        cls.config = cls.by_name[('ConfigMap', 'ava-config')]['data']

    def test_config_defaults_and_the_live_command_line(self):
        from services.live.__main__ import build_parser
        self.assertEqual((self.config['AVA_RETENTION'], self.config['AVA_RETENTION_TTL_DAYS']), ('off', '7'))
        live = self.pod['containers'][0]
        # Review R4: the same ConfigMap key as the init container, not a fixed value.
        self.assertEqual(live['args'][live['args'].index('--retention') + 1], '$(AVA_RETENTION)')
        resolved =[re.sub(r'\$\(([A-Z_]+)\)', lambda m: self.config.get(m.group(1), m.group(0)), arg)
                    for arg in live['args']]
        resolved = [arg.replace('$(POD_IP)', '10.0.0.5') for arg in resolved]
        args = build_parser().parse_args(resolved)
        self.assertEqual((args.retention, args.retention_ttl_days, args.retention_dir, args.retention_key_file),
                         ('off', 7.0, SNIPPET_DIR, KEY_FILE))

    def test_claims_mounts_and_no_fsgroup(self):
        for claim in ('ava-snippets', 'ava-retention-key'):
            pvc = self.by_name[('PersistentVolumeClaim', claim)]['spec']
            self.assertEqual(pvc['accessModes'], ['ReadWriteOnce'], claim)
            self.assertNotIn('storageClassName', pvc, f'{claim}: the node default class')
        volumes = {v['name']: v for v in self.pod['volumes']}
        self.assertEqual(volumes['snippets']['persistentVolumeClaim'], {'claimName': 'ava-snippets'})
        self.assertEqual(volumes['retention-key']['persistentVolumeClaim'], {'claimName': 'ava-retention-key'})
        live_mounts = {m['mountPath']: m for m in self.pod['containers'][0]['volumeMounts']}
        self.assertEqual(live_mounts[SNIPPET_DIR]['name'], 'snippets')
        self.assertTrue(live_mounts[KEY_DIR].get('readOnly'), 'live reads the key, never writes it')
        for mount in live_mounts.values():
            if mount['name'] in ('snippets', 'retention-key'):
                self.assertNotIn('subPath', mount, 'a subPath folder would be created as root')
        self.assertNotIn('fsGroup', self.pod['securityContext'],
                         'fsGroup adds group read to the key, which live refuses')
        api = self.by_name[('Deployment', 'api')]['spec']['template']['spec']
        self.assertFalse({'snippets', 'retention-key'} & {v['name'] for v in api['volumes']},
                         'the API does not mount FR-O5 data (playback is not wired in k3s)')

    def test_the_key_init_container_runs_after_the_storage_wait(self):
        inits = self.pod['initContainers']
        self.assertEqual([c['name'] for c in inits], ['wait-for-storage', 'retention-key'])
        job = inits[1]
        self.assertEqual(job['command'], ['python', '-m', 'services.retention.keygen'])
        # Review M1: live's own setting, so with "off" the job does nothing.
        self.assertEqual(job['args'], ['--retention', '$(AVA_RETENTION)', '--key-file', KEY_FILE])
        self.assertEqual(job['env'], [{'name': 'AVA_RETENTION', 'valueFrom': {
            'configMapKeyRef': {'name': 'ava-config', 'key': 'AVA_RETENTION'}}}],
            'only the setting: no secret or other variable reaches the key job')
        self.assertNotIn('envFrom', job)
        self.assertEqual(job['volumeMounts'], [{'name': 'retention-key', 'mountPath': KEY_DIR}])
        self.assertEqual(job['securityContext']['readOnlyRootFilesystem'], True)


class LiveImageTests(unittest.TestCase):
    def test_the_image_owns_both_mount_points_owner_only(self):
        text = DOCKERFILE.read_text(encoding='utf-8')
        self.assertRegex(text, r'install -d -o ava -g ava -m 0700 ' + re.escape(SNIPPET_DIR) + r' '
                         + re.escape(KEY_DIR))
        self.assertLess(text.index('useradd'), text.index('install -d -o ava'), 'the user exists first')


if __name__ == '__main__':
    unittest.main()
