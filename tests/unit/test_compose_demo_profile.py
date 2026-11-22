"""Claude-authored static tests (22 November 2026; not GPT-6 Astra) for the Compose `demo`
profile (backlog 11.2; PRD roadmap 5.9 demo, BO-4/BO-8; PRD constraint: no internet
dependency; deploy/compose/README.md "Demo profile").

Text checks run in CI (no YAML parser there); the structural checks parse compose.yaml
with PyYAML when it is installed (requirements-test.txt). Cases:
CD01 the forwarder is opt-in, published on 127.0.0.1 only, targets api:8000, hardened;
CD02 networks: `demo` internal, `demo-edge` the only non-internal one, joined only by
     the forwarder; api never on it; only api and the forwarder on `demo`;
CD03 the forwarder module uses the standard library only (it runs from the api image
     with no added package) and is admitted into the image by .dockerignore;
CD04 (PyYAML) the same rules on the parsed file, for every service.
"""
import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / 'deploy' / 'compose' / 'compose.yaml'
FORWARDER = (ROOT / 'services' / 'edge' / 'forward.py', ROOT / 'services' / 'edge' / '__main__.py')
STDLIB_OK = {'argparse', 'asyncio', 'dataclasses', 'json', 'signal', 'sys'}

try:
    import yaml
except ImportError:  # CI installs requirements-audio.txt only
    yaml = None


def published_entries(block):
    """Every list entry under `ports:`, quoted or not, short or long syntax (review L3)."""
    match = re.search(r'^    ports:\n((?:      [^\n]*\n)+)', block, re.M)
    if match is None:
        return []
    return [line.strip()[2:].strip() for line in match.group(1).splitlines() if line.strip().startswith('- ')] + \
        [line.strip() for line in match.group(1).splitlines() if not line.strip().startswith('- ')]


def service_block(text, name):
    match = re.search(rf'^  {re.escape(name)}:\n(.*?)(?=^  [a-z][a-z-]*:\n|^[a-z]+:)', text, re.M | re.S)
    return match.group(1) if match else None


class DemoProfileTextTests(unittest.TestCase):
    def setUp(self):
        self.text = COMPOSE.read_text(encoding='utf-8')
        self.block = service_block(self.text, 'demo-proxy')
        self.assertIsNotNone(self.block, 'demo-proxy service')

    def test_cd01_opt_in_loopback_only_fixed_target_and_hardened(self):
        block = self.block
        self.assertIn('    profiles: [demo]\n', block)
        self.assertEqual(published_entries(block), ['"127.0.0.1:${AVA_DEMO_PORT:-8080}:8080"'],
                         'one port, host side loopback only')
        self.assertEqual(len(re.findall(r'^\s*ports:', self.text, re.M)), 1, 'no other service publishes a port')
        self.assertNotRegex(self.text, r'(?m)^\s*(network_mode:\s*"?host|expose:)', 'no host networking')
        self.assertIn('    restart: on-failure\n', block, 'review L5: does not return after a reboot')
        self.assertIn('entrypoint: ["python", "-m", "services.edge"]', block)
        command = re.findall(r'^      - ([^\s#]+)', block[block.index('    command:\n'):block.index('    ports:\n')], re.M)
        args = dict(zip(command[::2], command[1::2]))
        self.assertEqual(args, {'--listen-host': '0.0.0.0', '--listen-port': '"8080"', '--target-host': 'api',
                                '--target-port': '"8000"', '--max-connections': '"32"'})
        self.assertIn('      api: {condition: service_healthy}', block)
        for rule in ('read_only: true', 'cap_drop: [ALL]', 'no-new-privileges:true', 'pids_limit: 64',
                     'mem_limit: 128m', 'cpus: 0.25', 'pull_policy: never', 'image: ava-api:dev'):
            self.assertIn(rule, block, rule)
        self.assertNotIn('environment:', block, 'the forwarder gets no secret')
        self.assertNotIn('volumes:', block, 'and no mount')

    def test_cd02_only_the_forwarder_has_a_route_out(self):
        networks = self.text[self.text.index('\nnetworks:\n'):self.text.index('\nvolumes:\n')]
        self.assertRegex(networks, r'\n  demo:\n(?:\s*#[^\n]*\n)*\s*internal: true\n')
        self.assertRegex(networks, r'\n  demo-edge:\n(?:\s*#[^\n]*\n)*\s*internal: false\n')
        self.assertEqual(networks.count('internal: false'), 1, 'demo-edge is the only network with a route out')
        self.assertIn('    networks: [demo-edge, demo]\n', self.block)
        services = self.text[self.text.index('\nservices:\n'):self.text.index('\nnetworks:\n')]
        self.assertEqual(len(re.findall(r'\bdemo-edge\b', services)), 2,
                         'demo-edge appears only in the forwarder (its networks and a comment)')
        api = service_block(self.text, 'api')
        self.assertIn('    networks: [backend, metrics, demo]\n', api)
        joined = re.findall(r'^    networks: \[([^\]]*)\]', services, re.M)
        self.assertEqual(sorted(n for n in joined if re.search(r'(?<![\w-])demo(?![\w-])', n)),
                         ['backend, metrics, demo', 'demo-edge, demo'], 'only api and the forwarder are on demo')

    def test_cd05_the_api_serves_only_allowlisted_host_names(self):
        """Review M4 (DNS rebinding): the api command, parsed by the real CLI parser,
        allows exactly the names its callers use; the Prometheus target is among them."""
        from services.api.__main__ import build_parser
        api = service_block(self.text, 'api')
        command = api[api.index('    command:\n'):api.index('    volumes:\n')]
        argv = [re.sub(r'\$\{[A-Z_]+:-([^}]*)\}', r'\1', re.sub(r'\s+#.*$', '', line.strip()[2:]).strip().strip('"'))
                for line in command.splitlines()[1:] if line.strip().startswith('- ')]
        args = build_parser().parse_args(argv)
        self.assertEqual(sorted(args.allowed_host), ['127.0.0.1', 'api', 'localhost'])
        prometheus = (ROOT / 'deploy' / 'compose' / 'monitoring' / 'prometheus.yml').read_text(encoding='utf-8')
        self.assertIn("targets: ['api:8000']", prometheus)
        self.assertIn("http://127.0.0.1:8000/readyz", api, 'the health check uses an allowed name')

    def test_cd06_the_checks_catch_an_unquoted_or_extra_port(self):
        block = self.block.replace('      - "127.0.0.1:${AVA_DEMO_PORT:-8080}:8080"\n',
                                   '      - "127.0.0.1:${AVA_DEMO_PORT:-8080}:8080"\n      - 9000:9000\n')
        self.assertEqual(published_entries(block), ['"127.0.0.1:${AVA_DEMO_PORT:-8080}:8080"', '9000:9000'])
        long_form = self.block.replace('      - "127.0.0.1:${AVA_DEMO_PORT:-8080}:8080"\n',
                                       '      - target: 8080\n        published: 8080\n')
        self.assertNotEqual(published_entries(long_form), ['"127.0.0.1:${AVA_DEMO_PORT:-8080}:8080"'])

    def test_cd03_the_forwarder_is_stdlib_only_and_in_the_image(self):
        for path in FORWARDER:
            tree = ast.parse(path.read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                    [node.module] if isinstance(node, ast.ImportFrom) and node.module else []
                for name in names:
                    top = name.split('.')[0]
                    self.assertTrue(top in STDLIB_OK or name.startswith('services.edge'), f'{path.name}: {name}')
        dockerignore = (ROOT / '.dockerignore').read_text(encoding='utf-8')
        self.assertRegex(dockerignore, r'(?m)^!services/\*\*/\*\.py$', 'services/**/*.py admitted into the image')
        dockerfile = (ROOT / 'deploy' / 'api' / 'Dockerfile').read_text(encoding='utf-8')
        self.assertIn('COPY services/ /app/services/', dockerfile)


@unittest.skipIf(yaml is None, 'needs PyYAML (requirements-test.txt)')
class DemoProfileParsedTests(unittest.TestCase):
    def test_cd04_parsed_rules_for_every_service(self):
        spec = yaml.safe_load(COMPOSE.read_text(encoding='utf-8'))
        networks, services = spec['networks'], spec['services']
        self.assertTrue(networks['demo']['internal'])
        self.assertFalse(networks['demo-edge']['internal'])
        outside = [name for name, net in networks.items() if not (net or {}).get('internal')]
        self.assertEqual(outside, ['demo-edge'])
        for name, service in services.items():
            joined = service.get('networks') or []
            joined = list(joined) if isinstance(joined, (list, dict)) else []
            with self.subTest(service=name):
                if name == 'demo-proxy':
                    self.assertEqual(joined, ['demo-edge', 'demo'])
                    self.assertEqual(service['profiles'], ['demo'])
                    self.assertEqual(service['ports'], ['127.0.0.1:${AVA_DEMO_PORT:-8080}:8080'])
                else:
                    self.assertNotIn('demo-edge', joined, f'{name} must have no route out')
                    self.assertNotIn('ports', service, f'{name} publishes nothing')
                    if name != 'api':
                        self.assertNotIn('demo', joined, f'{name} is not reachable from the forwarder')


if __name__ == '__main__':
    unittest.main()
