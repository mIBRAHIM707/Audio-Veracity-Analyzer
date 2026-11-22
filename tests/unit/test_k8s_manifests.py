"""Claude-authored static checks (19 November 2026) of the Kubernetes manifests in
deploy/k8s (backlog 10.2; PRD FR-O2 probes, limits, persistent volumes, ConfigMap and
Secrets; FR-O3 default-deny egress; FR-O4 models from a read-only local volume; FR-O7
digest-pinned public images; architecture §7.2).

Authored by Claude (Claude Code), not by GPT-6 Astra. These run in CI without a
cluster; the behaviour on a real k3s node (probes, policy enforcement, a real call) is
tests/lab/test_k3s_stack_lab.py. A rule here that a manifest breaks fails the build,
for example a container without limits, a hostPath in a pod, an egress rule with no
destination (which Kubernetes reads as "anywhere") or an unresolved `$(VAR)`.

PyYAML comes from requirements-test.txt (kept out of requirements-audio.txt, which the
service images include). Without it these tests SKIP with that reason; the CI workflow
installs only requirements-audio.txt until a human adds requirements-test.txt there.
"""
import ipaddress
from pathlib import Path
import re
import unittest

try:
    import yaml
except ImportError:  # pragma: no cover - environment without requirements-test.txt
    yaml = None

NEEDS_YAML = unittest.skipIf(yaml is None, 'PyYAML missing: install requirements-test.txt')

ROOT = Path(__file__).resolve().parents[2]
K8S = ROOT / 'deploy' / 'k8s'
BASE, LAB = K8S / 'base', K8S / 'lab'
COMPOSE = ROOT / 'deploy' / 'compose' / 'compose.yaml'
DIGEST = re.compile(r'^[a-z0-9./_-]+(:[A-Za-z0-9._-]+)?@sha256:[0-9a-f]{64}$')
LOCAL_IMAGE = re.compile(r'^ava-[a-z-]+:dev$')
WORKLOADS = ('Deployment', 'StatefulSet')
POD_VOLUME_TYPES = {'persistentVolumeClaim', 'emptyDir', 'configMap', 'secret', 'projected', 'downwardAPI'}


def load_dir(folder):
    kustomization = yaml.safe_load((folder / 'kustomization.yaml').read_text(encoding='utf-8'))
    objects = []
    for name in kustomization['resources']:
        path = folder / name
        if path.is_file():
            objects += [doc for doc in yaml.safe_load_all(path.read_text(encoding='utf-8')) if doc]
    return kustomization, objects


def pod_specs(objects):
    for obj in objects:
        if obj['kind'] in WORKLOADS:
            yield obj, obj['spec']['template']


def containers(objects):
    for obj, template in pod_specs(objects):
        for container in template['spec'].get('initContainers', []) + template['spec']['containers']:
            yield obj, template, container


@NEEDS_YAML
class BaseManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kustomization, cls.objects = load_dir(BASE)
        cls.by_kind = {}
        for obj in cls.objects:
            cls.by_kind.setdefault(obj['kind'], {})[obj['metadata']['name']] = obj

    def test_every_manifest_file_is_listed_and_parses(self):
        listed = set(self.kustomization['resources'])
        on_disk = {p.name for p in BASE.glob('*.yaml')} - {'kustomization.yaml'}
        self.assertEqual(listed, on_disk, 'a manifest not in kustomization.yaml is never applied')
        self.assertEqual(self.kustomization['namespace'], 'ava')
        for obj in self.objects:
            self.assertIn('apiVersion', obj)
            self.assertNotIn('namespace', obj['metadata'], f'{obj["kind"]}/{obj["metadata"]["name"]}: '
                                                           'the kustomization sets the namespace')
        self.assertEqual(set(self.by_kind['Deployment']), {'live', 'api'})
        self.assertEqual(set(self.by_kind['StatefulSet']), {'postgres', 'redis'})

    def test_the_namespace_enforces_pod_security_restricted(self):
        labels = self.by_kind['Namespace']['ava']['metadata']['labels']
        self.assertEqual(labels['pod-security.kubernetes.io/enforce'], 'restricted')

    def test_images_are_digest_pinned_or_local_never_pulled(self):
        compose = COMPOSE.read_text(encoding='utf-8')
        for obj, _, container in containers(self.objects):
            image, policy = container['image'], container.get('imagePullPolicy')
            where = f'{obj["metadata"]["name"]}/{container["name"]}'
            if LOCAL_IMAGE.match(image):
                self.assertEqual(policy, 'Never', f'{where}: a local build must never be pulled')
            else:
                self.assertRegex(image, DIGEST, f'{where}: public images are pinned by digest (FR-O7)')
                self.assertIn(image, compose, f'{where}: the same pinned image as the Compose stack')

    def test_every_container_has_probes_limits_and_a_locked_down_context(self):
        for obj, template, container in containers(self.objects):
            where = f'{obj["metadata"]["name"]}/{container["name"]}'
            spec = template['spec']
            if container in spec['containers']:  # init containers run to completion, unprobed
                for probe in ('readinessProbe', 'livenessProbe'):
                    self.assertIn(probe, container, f'{where}: {probe} (FR-O2)')
            for bound in ('requests', 'limits'):
                self.assertEqual(set(container['resources'][bound]), {'cpu', 'memory'}, f'{where}: {bound}')
            context = container['securityContext']
            self.assertIs(context['allowPrivilegeEscalation'], False, where)
            self.assertIs(context['readOnlyRootFilesystem'], True, where)
            self.assertEqual(context['capabilities'], {'drop': ['ALL']}, where)
            self.assertNotIn('privileged', context, where)
            pod = spec['securityContext']
            self.assertIs(pod['runAsNonRoot'], True, where)
            self.assertIsInstance(pod['runAsUser'], int)
            self.assertGreater(pod['runAsUser'], 0, where)
            self.assertEqual(pod['seccompProfile'], {'type': 'RuntimeDefault'}, where)
            self.assertIs(spec['automountServiceAccountToken'], False, f'{where}: no Kubernetes API token')
            self.assertNotIn('hostNetwork', spec)
            self.assertNotIn('hostPID', spec)

    def test_probe_ports_name_declared_container_ports(self):
        for obj, _, container in containers(self.objects):
            names = {p.get('name') for p in container.get('ports', [])}
            for probe in ('startupProbe', 'readinessProbe', 'livenessProbe'):
                http = container.get(probe, {}).get('httpGet')
                if http:
                    self.assertIn(http['port'], names, f'{obj["metadata"]["name"]}: {probe} port')

    def test_pods_use_no_host_path_and_models_are_mounted_read_only(self):
        for obj, template in pod_specs(self.objects):
            for volume in template['spec'].get('volumes', []):
                kinds = set(volume) - {'name'}
                self.assertTrue(kinds <= POD_VOLUME_TYPES, f'{obj["metadata"]["name"]}: volume {volume}')
                for kind in kinds:
                    if kind == 'emptyDir':
                        self.assertIn('sizeLimit', volume['emptyDir'], f'{obj["metadata"]["name"]}: bounded')
            models = [v for v in template['spec'].get('volumes', []) if v['name'] == 'models']
            for volume in models:
                self.assertEqual(volume['persistentVolumeClaim'], {'claimName': 'ava-models', 'readOnly': True})
                for container in template['spec']['containers']:
                    for mount in container['volumeMounts']:
                        if mount['name'] == 'models':
                            self.assertIs(mount.get('readOnly'), True, f'{obj["metadata"]["name"]}: {mount}')
        volume = self.by_kind['PersistentVolume']['ava-models']['spec']
        claim = self.by_kind['PersistentVolumeClaim']['ava-models']['spec']
        self.assertEqual(volume['claimRef'], {'namespace': 'ava', 'name': 'ava-models'})
        self.assertEqual((claim['volumeName'], claim['storageClassName']), ('ava-models', volume['storageClassName']))
        self.assertEqual(volume['accessModes'], ['ReadOnlyMany'])
        self.assertTrue(volume['hostPath']['path'].startswith('/var/lib/ava/'))

    def test_stateful_services_keep_their_data_on_claims(self):
        for name, mount in (('postgres', '/var/lib/postgresql/data'), ('redis', '/data')):
            sts = self.by_kind['StatefulSet'][name]['spec']
            templates = {t['metadata']['name'] for t in sts['volumeClaimTemplates']}
            self.assertEqual(templates, {'data'})
            mounts = {m['mountPath']: m['name'] for m in sts['template']['spec']['containers'][0]['volumeMounts']}
            self.assertEqual(mounts[mount], 'data', name)
        self.assertEqual(self.by_kind['Deployment']['live']['spec']['strategy'], {'type': 'Recreate'},
                         'two tap processes must never own one PBX application at once')

    def test_python_services_are_not_pid_1_and_wait_for_storage(self):
        """Review L1: Python as PID 1 ignores SIGTERM until its handler is installed (model
        load), so a stop would wait out the grace period; the pod's pause process takes
        PID 1 instead (Compose uses `init: true`). Review L3: the services wait for their
        storage in an init container instead of crash-looping until it is up."""
        for name, needs in (('live', {'postgres:5432', 'redis:6379'}), ('api', {'redis:6379', 'postgres:5432'})):
            spec = self.by_kind['Deployment'][name]['spec']['template']['spec']
            self.assertIs(spec.get('shareProcessNamespace'), True, name)
            wait = spec['initContainers'][0]  # first: later init containers need the storage
            self.assertEqual(wait['name'], 'wait-for-storage', name)
            for init in spec['initContainers']:
                self.assertEqual(init['image'], spec['containers'][0]['image'], f'{name}: no extra image')
            self.assertEqual(set(wait['args']), needs, name)
        self.assertEqual([c['name'] for c in self.by_kind['Deployment']['live']['spec']['template']['spec']
                          ['initContainers']], ['wait-for-storage', 'retention-key'],
                         'live never waits on the API\'s reader role (22 Nov: plus the FR-O5 key job)')

    def test_every_variable_reference_resolves(self):
        """Kubernetes leaves an unknown `$(NAME)` in args as literal text: a typo would
        reach the program, not fail the deployment."""
        config = set(self.by_kind['ConfigMap']['ava-config']['data'])
        for obj, _, container in containers(self.objects):
            names = {e['name'] for e in container.get('env', [])}
            for source in container.get('envFrom', []):
                self.assertEqual(source['configMapRef']['name'], 'ava-config')
                names |= config
            for arg in container.get('args', []) + container.get('command', []):
                for ref in re.findall(r'\$\(([A-Za-z_][A-Za-z0-9_]*)\)', str(arg)):
                    self.assertIn(ref, names, f'{obj["metadata"]["name"]}: $({ref})')
            for index, arg in enumerate(container.get('args', [])):
                if arg in ('--database-url-env', '--redis-url-env', '--ari-password-env'):
                    self.assertIn(container['args'][index + 1], names, f'{obj["metadata"]["name"]} {arg}')

    def test_secrets_are_referenced_never_committed(self):
        self.assertNotIn('Secret', self.by_kind, 'no Secret object (with values) in Git')
        refs = set()
        for _, _, container in containers(self.objects):
            for env in container.get('env', []):
                ref = env.get('valueFrom', {}).get('secretKeyRef')
                if ref:
                    refs.add((ref['name'], ref['key']))
                self.assertFalse(env['name'].endswith('PASSWORD') and 'value' in env, env['name'])
        self.assertEqual(refs, {('ava-secrets', 'ari-password'), ('ava-secrets', 'db-password'),
                                ('ava-secrets', 'db-reader-password')})
        for key in ('AVA_DATABASE_URL', 'AVA_HISTORY_DATABASE_URL'):
            self.assertNotRegex(self.by_kind['ConfigMap']['ava-config']['data'][key], r'//[^/@]*:[^/@]*@',
                                'no password inside the database URL')

    def test_the_api_reads_history_as_the_read_only_login_only(self):
        """20 Nov (backlog 5.1; Claude-authored): the owner password reaches only live and
        the API's reader-role init container; the API container gets the reader's and
        connects with the reader URL; the init container's command line is the one the
        real parser accepts, for the role the URL names."""
        from services.store.__main__ import build_parser
        config = self.by_kind['ConfigMap']['ava-config']['data']
        owner_user = re.match(r'^postgresql://([a-z_]+)@', config['AVA_DATABASE_URL']).group(1)
        reader_user = re.match(r'^postgresql://([a-z_]+)@', config['AVA_HISTORY_DATABASE_URL']).group(1)
        self.assertEqual((owner_user, reader_user), ('ava', 'ava_reader'))
        holders = {}
        for obj, _, container in containers(self.objects):
            for env in container.get('env', []):
                ref = env.get('valueFrom', {}).get('secretKeyRef')
                if ref and ref['key'].startswith('db-'):
                    holders.setdefault(ref['key'], set()).add((obj['metadata']['name'], container['name'], env['name']))
        self.assertEqual(holders['db-password'], {('postgres', 'postgres', 'POSTGRES_PASSWORD'),
                                                  ('live', 'live', 'PGPASSWORD'),
                                                  ('api', 'db-reader-role', 'PGPASSWORD')})
        self.assertEqual(holders['db-reader-password'], {('api', 'db-reader-role', 'AVA_DB_READER_PASSWORD'),
                                                         ('api', 'api', 'PGPASSWORD')})
        spec = self.by_kind['Deployment']['api']['spec']['template']['spec']
        api = spec['containers'][0]
        index = api['args'].index('--database-url-env')
        self.assertEqual(api['args'][index + 1], 'AVA_HISTORY_DATABASE_URL')
        [setup] = [c for c in spec['initContainers'] if c['name'] == 'db-reader-role']
        self.assertEqual(setup['command'], ['python', '-m', 'services.store'])
        parsed = build_parser().parse_args(setup['args'])
        self.assertEqual((parsed.command, parsed.database_url_env, parsed.role, parsed.password_env),
                         ('reader-role', 'AVA_DATABASE_URL', reader_user, 'AVA_DB_READER_PASSWORD'))

    def test_selectors_match_their_pods(self):
        pods = {obj['metadata']['name']: template['metadata']['labels'] for obj, template in pod_specs(self.objects)}
        for obj, template in pod_specs(self.objects):
            selector = obj['spec']['selector']['matchLabels']
            self.assertLessEqual(selector.items(), template['metadata']['labels'].items(), obj['metadata']['name'])
        for name, service in self.by_kind['Service'].items():
            matched = [p for p, labels in pods.items() if service['spec']['selector'].items() <= labels.items()]
            self.assertEqual(len(matched), 1, f'Service {name} selects {matched}')
        for name, sts in self.by_kind['StatefulSet'].items():
            self.assertIn(sts['spec']['serviceName'], self.by_kind['Service'])

    def test_network_policy_denies_by_default_and_allows_only_named_peers(self):
        policies = self.by_kind['NetworkPolicy']
        deny = policies['default-deny']['spec']
        self.assertEqual(deny, {'podSelector': {}, 'policyTypes': ['Ingress', 'Egress']}, 'deny all, both ways')
        pods = [template['metadata']['labels'] for _, template in pod_specs(self.objects)]
        for name, policy in policies.items():
            spec = policy['spec']
            selector = spec['podSelector'].get('matchLabels', {})
            self.assertTrue(any(selector.items() <= labels.items() for labels in pods), f'{name} selects no pod')
            for direction, peers_key in (('egress', 'to'), ('ingress', 'from')):
                for rule in spec.get(direction, []):
                    # A rule without peers (or with an empty list) admits every address.
                    self.assertTrue(rule.get(peers_key), f'{name}: {direction} rule without {peers_key}')
                    self.assertTrue(rule.get('ports'), f'{name}: {direction} rule without ports')
                    for peer in rule[peers_key]:
                        if 'ipBlock' in peer:
                            network = ipaddress.ip_network(peer['ipBlock']['cidr'])
                            self.assertGreaterEqual(network.prefixlen, 24, f'{name}: {network} is too wide')
                        else:
                            self.assertTrue(peer.get('podSelector') or peer.get('namespaceSelector'), f'{name}: {peer}')
                        for label_selector in (peer.get('podSelector'), peer.get('namespaceSelector')):
                            if label_selector is not None:
                                self.assertTrue(label_selector.get('matchLabels'), f'{name}: an empty selector')
        # Peers outside the namespace: egress to kube-dns and to the PBX (live only);
        # ingress from the PBX into live only (review N5).
        external = {'egress': {}, 'ingress': {}}
        for name, policy in policies.items():
            for direction, peers_key in (('egress', 'to'), ('ingress', 'from')):
                for rule in policy['spec'].get(direction, []):
                    for peer in rule[peers_key]:
                        if 'ipBlock' in peer or 'namespaceSelector' in peer:
                            external[direction][name] = (peer, rule['ports'])
        self.assertEqual(set(external['egress']), {'allow-dns', 'live-pbx'})
        self.assertEqual(set(external['ingress']), {'live-pbx'})
        self.assertEqual(external['egress']['allow-dns'][0]['podSelector'], {'matchLabels': {'k8s-app': 'kube-dns'}})
        pbx = {'ipBlock': {'cidr': '192.0.2.10/32'}}
        self.assertEqual(external['egress']['live-pbx'], (pbx, [{'protocol': 'TCP', 'port': 8088}]),
                         'the base PBX placeholder is a documentation address; ARI only')
        # Review L4: tapped RTP only on Linux's default ephemeral range, where the receivers bind.
        self.assertEqual(external['ingress']['live-pbx'], (pbx, [{'protocol': 'UDP', 'port': 32768, 'endPort': 60999}]))
        self.assertIn('192.0.2.10', self.by_kind['ConfigMap']['ava-config']['data']['AVA_ARI_URL'])

    def test_postgres_admits_only_live_and_the_api(self):
        """20 Nov: the API reads history (as the read-only login); nothing else connects."""
        ingress = self.by_kind['NetworkPolicy']['postgres']['spec']['ingress']
        self.assertEqual(ingress, [{'from': [{'podSelector': {'matchLabels': {'app.kubernetes.io/name': 'live'}}},
                                             {'podSelector': {'matchLabels': {'app.kubernetes.io/name': 'api'}}}],
                                    'ports': [{'protocol': 'TCP', 'port': 5432}]}])
        egress = self.by_kind['NetworkPolicy']['api']['spec']['egress']
        self.assertEqual(sorted((rule['to'][0]['podSelector']['matchLabels']['app.kubernetes.io/name'],
                                 rule['ports'][0]['port']) for rule in egress), [('postgres', 5432), ('redis', 6379)])


class TestRequirementsStayOutOfImagesTests(unittest.TestCase):
    """Review M1 (runs without PyYAML): the test-only pins never reach a service image."""

    def included(self, path, seen=None):
        seen = set() if seen is None else seen
        for line in path.read_text(encoding='utf-8').splitlines():
            match = re.match(r'^-r\s+(\S+)', line.strip())
            if match:
                name = Path(match.group(1)).name
                if name not in seen:
                    seen.add(name)
                    self.included(ROOT / name, seen)
        return seen

    def test_image_requirement_files_never_include_the_test_pins(self):
        for image_file in (ROOT / 'deploy' / 'live' / 'requirements-live.txt',
                           ROOT / 'deploy' / 'api' / 'requirements-api-image.txt'):
            files = self.included(image_file) | {image_file.name}
            self.assertNotIn('requirements-test.txt', files, image_file)
            for name in files:
                path = ROOT / name if (ROOT / name).is_file() else image_file
                self.assertNotRegex(path.read_text(encoding='utf-8').lower(), r'(?m)^pyyaml', f'{name} in {image_file}')
        self.assertIn('pyyaml==6.0.3', (ROOT / 'requirements-test.txt').read_text(encoding='utf-8'))


@NEEDS_YAML
class LabOverlayTests(unittest.TestCase):
    def test_the_overlay_points_live_at_the_lab_pbx(self):
        kustomization, objects = load_dir(LAB)
        self.assertIn('../base', kustomization['resources'])
        self.assertNotIn('namespace', kustomization, 'the PBX keeps its own namespace')
        _, base = load_dir(BASE)
        base_by_name = {(o['kind'], o['metadata']['name']): o for o in base}
        for patch in kustomization['patches']:
            target = base_by_name[(patch['target']['kind'], patch['target']['name'])]
            for op in yaml.safe_load(patch['patch']):
                node = target
                for part in op['path'].strip('/').split('/'):
                    node = node[int(part)] if isinstance(node, list) else node[part]  # the path exists in base
        names = {(o['kind'], o['metadata']['name']): o for o in objects}
        self.assertEqual(names[('Namespace', 'ava-lab-pbx')]['metadata']['name'], 'ava-lab-pbx')
        deployment = names[('Deployment', 'asterisk')]
        self.assertEqual(deployment['metadata']['namespace'], 'ava-lab-pbx')
        container = deployment['spec']['template']['spec']['containers'][0]
        self.assertEqual((container['image'], container['imagePullPolicy']), ('ava-lab-asterisk:dev', 'Never'))
        self.assertNotIn('Secret', {kind for kind, _ in names}, 'the lab ARI password is generated, not committed')
        policy_patch = next(p for p in kustomization['patches'] if p['target']['name'] == 'live-pbx')
        ops = yaml.safe_load(policy_patch['patch'])
        self.assertEqual({op['path'] for op in ops}, {'/spec/egress/0/to', '/spec/ingress/0/from'})
        pod_labels = deployment['spec']['template']['metadata']['labels']
        for op in ops:
            [peer] = op['value']
            self.assertEqual(peer['namespaceSelector']['matchLabels'], {'kubernetes.io/metadata.name': 'ava-lab-pbx'})
            self.assertLessEqual(peer['podSelector']['matchLabels'].items(), pod_labels.items(), 'selects the PBX pod')


if __name__ == '__main__':
    unittest.main()
