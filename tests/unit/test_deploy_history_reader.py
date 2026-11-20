"""Claude-authored static checks (20 November 2026) of the Compose wiring of the API's
read-only history login (backlog 5.1 "Compose/k3s wiring with a read-only role"; PRD
FR-A3, FR-A6, §6.3; architecture §2.5, §2.7). The k3s side is in test_k8s_manifests.py;
the role's privileges on a real PostgreSQL are in test_store_roles.py, and the running
stack in tests/lab/test_compose_stack_lab.py.

Authored by Claude (Claude Code), not by GPT-6 Astra. Needs PyYAML
(requirements-test.txt); skips without it, like the k8s checks.
"""
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


@unittest.skipIf(yaml is None, 'PyYAML missing: install requirements-test.txt')
class ComposeHistoryReaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.services = yaml.safe_load(COMPOSE.read_text(encoding='utf-8'))['services']

    def test_db_setup_runs_the_reader_role_command_the_parser_accepts(self):
        from services.store.__main__ import build_parser
        setup = self.services['db-setup']
        self.assertEqual(setup['entrypoint'], ['python', '-m', 'services.store'])
        args = build_parser().parse_args(setup['command'])
        self.assertEqual((args.command, args.database_url_env, args.role, args.password_env),
                         ('reader-role', 'AVA_DATABASE_URL', 'ava_reader', 'AVA_DB_READER_PASSWORD'))
        self.assertEqual(setup['environment']['AVA_DATABASE_URL'], 'postgresql://ava@postgres:5432/ava')
        self.assertEqual(setup['restart'], 'no', 'a one-shot job, not a restarting service')
        self.assertEqual(setup['depends_on'], {'postgres': {'condition': 'service_healthy'}})
        self.assertEqual(setup['networks'], ['backend'])
        self.assertIs(setup['read_only'], True)
        self.assertEqual(setup['cap_drop'], ['ALL'])

    def test_the_api_reads_as_the_reader_and_never_gets_the_owner_password(self):
        api = self.services['api']
        environment = api['environment']
        self.assertEqual(environment['AVA_DATABASE_URL'], 'postgresql://ava_reader@postgres:5432/ava')
        self.assertRegex(environment['PGPASSWORD'], r'^\$\{AVA_DB_READER_PASSWORD:\?')
        index = api['command'].index('--database-url-env')
        self.assertEqual(api['command'][index + 1], 'AVA_DATABASE_URL')
        self.assertEqual(api['depends_on']['db-setup'], {'condition': 'service_completed_successfully'})
        holders = {name for name, service in self.services.items()
                   if 'AVA_DB_PASSWORD' in str(service.get('environment', {}))}
        self.assertEqual(holders, {'postgres', 'live', 'db-setup'}, 'the owner password stays with the owner side')
        self.assertNotIn('db-setup', str(self.services['live'].get('depends_on', {})),
                         'live never waits on the API\'s reader role')

    def test_no_password_is_inside_a_url_and_the_example_names_the_key(self):
        for name, service in self.services.items():
            for key, value in (service.get('environment') or {}).items():
                if str(value).startswith('postgresql://'):
                    self.assertNotRegex(value, r'//[^/@]*:[^/@]*@', f'{name} {key}')
        self.assertRegex(ENV_EXAMPLE.read_text(encoding='utf-8'), re.compile(r'^AVA_DB_READER_PASSWORD=', re.M))


if __name__ == '__main__':
    unittest.main()
