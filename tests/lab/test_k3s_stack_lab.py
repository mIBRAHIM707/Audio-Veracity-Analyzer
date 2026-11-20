"""Claude-authored lab test (19 November 2026): the Kubernetes manifests on a real k3s
node (backlog 10.2; PRD FR-O2 manifests with probes, limits and persistent volumes,
FR-O3 NetworkPolicy denies egress, FR-O4 models from a local read-only volume, BO-7;
architecture §7.2, ADR-8).

Authored by Claude (Claude Code), not by GPT-6 Astra. Opt-in (AVA_TEST_K3S_LAB=1);
needs Docker, the locally built images `ava-live:dev`, `ava-api:dev` and
`ava-lab-asterisk:dev`, the public QA clips, the Silero model, an exported detector
(AVA_LIVE_ONNX; the 14 Nov static-INT8 export with an UNTRAINED head: scores carry no
meaning) and the pinned AntiDeepfake checkpoint (`model_root` in
`.local/real-model-qa.json`).

It starts a disposable single-node k3s server as a privileged Docker container (the
lab's node, pinned by digest; k3s ships a NetworkPolicy controller), imports the three
local images, mounts the model files read-only at the node's /var/lib/ava/models, and
applies the operator's flow from deploy/k8s/README.md: a site overlay (here in a
temporary folder) on top of `deploy/k8s/lab` that names the detector file and
generates the Secrets from files. Then:
- every pod becomes ready through its own probes, under Pod Security "restricted";
  a privileged pod is refused in the namespace;
- a real SIPp call into the lab PBX (namespace `ava-lab-pbx`) is tapped across the
  namespaces: stored in PostgreSQL with `bye` and its windows, and ended in the API's
  registry (through Redis);
- FR-O3: no AVA pod (live, api, postgres, redis) can open a connection to the
  internet while a pod without the policy can (control); live and api cannot query a
  public DNS resolver directly (cluster DNS still forwards names: README "Egress");
  the PBX namespace cannot reach postgres, redis or the api although it resolves
  them; inside the namespace only the allowed flows work (api -> redis yes, api ->
  postgres no, live -> api no); each "blocked" has a positive control (review L2);
- (20 Nov, PRD FR-A9) a patch of the `ava-scoring` ConfigMap reaches the running live
  pod through the kubelet's update of the mounted folder and is applied (one
  `config_reloaded`, `ava_live_config_version` + 1), with no restart;
- no container restarted (init containers wait for the storage); AVA pods run as
  their non-root users; no password appears in any pod log.
The node container and its volumes are removed afterwards. The node itself pulls the
k3s system images and the digest-pinned PostgreSQL and Redis images from Docker Hub
when it starts (lab set-up; the pods' own egress is what FR-O3 constrains).
"""
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import tempfile
import textwrap
import time
import unittest

from lab.asterisk import rtp_pcap
from models.export import model_card as mc
from tests.lab.test_compose_stack_lab import EGRESS_PROBE, HTTP_GET, MODEL_ROOT
from tests.lab.test_live_service_lab import ONNX, QA_CONFIG, VAD_MODEL

LAB = unittest.skipIf(os.environ.get('AVA_TEST_K3S_LAB') != '1', 'set AVA_TEST_K3S_LAB=1')
ROOT = Path(__file__).resolve().parents[2]
K8S = ROOT / 'deploy' / 'k8s'
K3S_IMAGE = ('rancher/k3s:v1.34.2-k3s1@sha256:'
             '331ec2db7e78a5ccc5da6a744003079045e0b0ecc35b7d1733b7185c5f48829e')
LOCAL_IMAGES = ('ava-live:dev', 'ava-api:dev', 'ava-lab-asterisk:dev')

# Run in a pod (python -c SCRIPT HOST PORT): print "open", "unresolved" (the name did
# not resolve: says nothing about the policy) or "blocked <error>".
PY_CONNECT = textwrap.dedent(r'''
    import socket, sys
    try:
        address = socket.getaddrinfo(sys.argv[1], int(sys.argv[2]), socket.AF_INET, socket.SOCK_STREAM)[0][4]
    except OSError:
        print('unresolved'); sys.exit()
    try:
        socket.create_connection(address, timeout=4).close()
        print('open')
    except OSError as e:
        print('blocked', type(e).__name__)
''')
# Review M2: a DNS query sent straight to a public resolver (python -c SCRIPT IP), not
# through cluster DNS: "answered" or "blocked <error>".
PY_DNS = textwrap.dedent(r'''
    import os, socket, struct, sys
    query = struct.pack('>HHHHHH', int.from_bytes(os.urandom(2), 'big'), 0x0100, 1, 0, 0, 0) + \
        b'\x07example\x03com\x00' + struct.pack('>HH', 1, 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(4)
    try:
        s.sendto(query, (sys.argv[1], 53))
        s.recvfrom(512)
        print('answered')
    except OSError as e:
        print('blocked', type(e).__name__)
''')
# Busybox (alpine) and bash (ubuntu) versions of the TCP probe. Review L2: a missing
# tool or an unresolved name must not read as "blocked", so the tool is checked first
# and the callers pass IP addresses or names they resolve separately.
NC_CONNECT = ('command -v nc >/dev/null || { echo no-nc; exit; }; '
              'if nc -z -w 4 "$0" "$1"; then echo open; else echo blocked; fi')
BASH_CONNECT = 'if timeout 5 bash -c "exec 3<>/dev/tcp/$0/$1"; then echo open; else echo blocked; fi'
# Re-review L2': the control for PY_DNS sends the same UDP query (example.com, A) from the
# PBX pod, which has bash but no Python: "answered" or "blocked".
BASH_DNS = ('exec 3<>/dev/udp/$0/53 || { echo blocked; exit; }; '
            r"printf '\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x07example\x03com\x00\x00\x01\x00\x01' >&3; "
            'if timeout 4 head -c 1 <&3 >/dev/null; then echo answered; else echo blocked; fi')


@LAB
@unittest.skipUnless(QA_CONFIG.is_file() and VAD_MODEL.is_file() and ONNX.is_file() and MODEL_ROOT is not None,
                     'needs the local QA clips, the Silero model, an exported detector and the AntiDeepfake checkpoint')
@unittest.skipUnless(shutil.which('docker'), 'needs Docker')
class K3sStackLabTests(unittest.TestCase):
    def docker(self, *args, check=True, timeout=300, input=None):
        done = subprocess.run([shutil.which('docker'), *args], input=input, capture_output=True,
                              text=input is None or isinstance(input, str), timeout=timeout)
        if check and done.returncode != 0:
            out, err = (done.stdout, done.stderr) if isinstance(done.stdout, str) else \
                (done.stdout.decode('utf-8', 'replace'), done.stderr.decode('utf-8', 'replace'))
            self.fail(f'docker {" ".join(args[:4])} failed: {out[-2000:]} {err[-2000:]}')
        return done

    def kubectl(self, *args, check=True, timeout=300, input=None):
        return self.docker('exec', *(('-i',) if input is not None else ()), self.node, 'kubectl', *args,
                           check=check, timeout=timeout, input=input)

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='ava-k3s-')
        self.addCleanup(temp.cleanup)
        folder = Path(temp.name)
        self.folder = folder
        card_dir = folder / 'card'
        card_dir.mkdir()
        mc.write_card(mc.build_card(
            ONNX, version='k3s-l8-int8-static-untrained', backbone='AntiDeepfake MMS-300M, truncated',
            kept_layers=8, metrics={'dev_eer': mc.NOT_MEASURED},
            training_data={'status': 'untrained SLS head (seeded); no AVA training data'},
            failure_modes=['untrained head: scores carry no meaning'],
            licence_restriction='Research and non-commercial use only.'), card_dir / 'model_card.json')

        # The operator's site overlay (deploy/k8s/README.md): the detector's file name and
        # the Secrets generated from files that stay outside Git.
        self.ari_password = secrets.token_hex(16)
        self.db_password = f'{secrets.token_hex(8)}/@+=%#x'  # URL-hostile on purpose
        site = folder / 'site'
        (site / 'secrets').mkdir(parents=True)
        (site / 'secrets' / 'ari-password').write_text(self.ari_password, encoding='ascii')
        (site / 'secrets' / 'db-password').write_text(self.db_password, encoding='ascii')
        (site / 'kustomization.yaml').write_text(textwrap.dedent(f'''\
            apiVersion: kustomize.config.k8s.io/v1beta1
            kind: Kustomization
            resources:
              - ../k8s/lab
            generatorOptions:
              disableNameSuffixHash: true
            secretGenerator:
              - name: ava-secrets
                namespace: ava
                files:
                  - ari-password=secrets/ari-password
                  - db-password=secrets/db-password
              - name: ava-lab-ari
                namespace: ava-lab-pbx
                files:
                  - password=secrets/ari-password
            patches:
              - target: {{kind: ConfigMap, name: ava-config}}
                patch: |-
                  - op: replace
                    path: /data/AVA_ONNX_FILE
                    value: {ONNX.name}
            '''), encoding='utf-8')

        images = folder / 'images'
        images.mkdir()
        self.docker('save', '-o', str(images / 'ava.tar'), *LOCAL_IMAGES, timeout=900)

        self.node = f'ava-k3s-{secrets.token_hex(4)}'
        mounts = [(ONNX.parent, '/var/lib/ava/models/onnx'), (card_dir, '/var/lib/ava/models/card'),
                  (VAD_MODEL.parent, '/var/lib/ava/models/vad'), (MODEL_ROOT, '/var/lib/ava/models/antideepfake'),
                  (images, '/images'), (K8S, '/work/k8s'), (site, '/work/site')]
        volume_args = [arg for host, node in mounts for arg in ('-v', f'{Path(host).as_posix()}:{node}:ro')]
        self.addCleanup(self.docker, 'rm', '-f', '-v', self.node, check=False, timeout=180)
        self.docker('run', '-d', '--name', self.node, '--privileged', '--tmpfs', '/run', '--tmpfs', '/var/run',
                    *volume_args, K3S_IMAGE, 'server', '--disable=traefik', '--disable=servicelb',
                    '--disable=metrics-server', timeout=300)
        self.wait_for_node(180)
        self.docker('exec', self.node, 'ctr', '-n', 'k8s.io', 'images', 'import', '/images/ava.tar', timeout=900)
        listed = self.docker('exec', self.node, 'ctr', '-n', 'k8s.io', 'images', 'ls', '-q').stdout.split()
        for image in LOCAL_IMAGES:
            self.assertIn(f'docker.io/library/{image}', listed, f'{image} imported into the node')

    def wait_for_node(self, seconds):
        deadline, last = time.monotonic() + seconds, ''
        while time.monotonic() < deadline:
            done = self.kubectl('get', 'nodes', '-o', 'jsonpath={.items[*].status.conditions[?(@.type=="Ready")].status}',
                                check=False, timeout=30)
            last = done.stdout + done.stderr
            if done.returncode == 0 and done.stdout.strip() == 'True':
                # The default service account must exist before pods can be created.
                if self.kubectl('-n', 'default', 'get', 'serviceaccount', 'default', check=False).returncode == 0:
                    return
            time.sleep(2)
        self.fail(f'k3s node not ready: {last[-1500:]}')

    def diagnose(self):
        """Pods, events and recent logs, for a failure message."""
        parts = [self.kubectl('get', 'pods', '-A', '-o', 'wide', check=False).stdout,
                 self.kubectl('get', 'events', '-A', '--sort-by=.lastTimestamp', check=False).stdout[-4000:]]
        for namespace, workload in (('ava', 'deploy/live'), ('ava', 'deploy/api'), ('ava', 'statefulset/postgres'),
                                    ('ava', 'statefulset/redis'), ('ava-lab-pbx', 'deploy/asterisk')):
            logs = self.kubectl('-n', namespace, 'logs', workload, '--tail=30', check=False)
            parts.append(f'--- {workload}\n{logs.stdout[-2500:]}{logs.stderr[-500:]}')
        return '\n'.join(parts)

    def wait_available(self, namespace, workload, seconds):
        kind, _, name = workload.partition('/')
        if kind == 'statefulset':
            args = ('rollout', 'status', workload, f'--timeout={seconds}s')
        else:
            args = ('wait', '--for=condition=Available', workload, f'--timeout={seconds}s')
        done = self.kubectl('-n', namespace, *args, check=False, timeout=seconds + 60)
        if done.returncode != 0:
            self.fail(f'{namespace}/{workload} not ready: {done.stdout} {done.stderr}\n{self.diagnose()}')

    def in_pod(self, namespace, workload, *command, check=True, timeout=120, input=None):
        return self.kubectl('-n', namespace, 'exec', *(('-i',) if input is not None else ()), workload, '--',
                            *command, check=check, timeout=timeout, input=input)

    def psql(self, sql):
        return self.in_pod('ava', 'statefulset/postgres', 'psql', '-U', 'ava', '-d', 'ava', '-tAc', sql,
                           check=False).stdout.strip()

    def api_get(self, path):
        return json.loads(self.in_pod('ava', 'deploy/api', 'python', '-c', HTTP_GET, path).stdout)

    def test_the_manifests_run_a_real_call_with_no_egress(self):
        applied = self.kubectl('apply', '-k', '/work/site', check=False, timeout=180)
        self.assertEqual(applied.returncode, 0, f'{applied.stdout} {applied.stderr}')
        self.assertNotIn('Warning', applied.stderr, f'Pod Security warnings: {applied.stderr}')
        for namespace, workload, seconds in (('ava', 'statefulset/postgres', 300), ('ava', 'statefulset/redis', 180),
                                             ('ava-lab-pbx', 'deploy/asterisk', 180), ('ava', 'deploy/live', 360),
                                             ('ava', 'deploy/api', 480)):
            self.wait_available(namespace, workload, seconds)

        # Pod Security "restricted" is enforced in the namespace (a privileged pod is refused).
        refused = self.kubectl('-n', 'ava', 'run', 'psa-probe', '--image=ava-live:dev', '--image-pull-policy=Never',
                               '--privileged', '--restart=Never', '--dry-run=server', check=False)
        self.assertNotEqual(refused.returncode, 0, refused.stdout)
        self.assertIn('PodSecurity', refused.stderr)

        ready = self.api_get('/readyz')
        self.assertEqual(ready['status'], 200, ready)

        # A real call into the lab PBX, tapped from the other namespace.
        clips = json.loads(QA_CONFIG.read_text(encoding='utf-8'))['genuine'][:3]
        payloads = rtp_pcap.rtp_payloads(b''.join(rtp_pcap.wav_to_pcm8k(path) for path in clips), 'ulaw')
        pcap = self.folder / 'inject.pcap'
        rtp_pcap.write_pcap(payloads, 0, pcap)
        self.in_pod('ava-lab-pbx', 'deploy/asterisk', 'sh', '-c', 'cat > /lab/io/inject.pcap',
                    input=pcap.read_bytes())
        caller = f'k3s{secrets.token_hex(4)}'
        sipp = self.in_pod('ava-lab-pbx', 'deploy/asterisk', 'sipp', '127.0.0.1:5060', '-sf',
                           '/lab/sipp/uac_inject.xml', '-s', '700', '-key', 'caller', caller, '-key', 'pt', '0',
                           '-key', 'codec', 'PCMU', '-m', '1', '-d', str(len(payloads) * 20 + 1500), '-i', '127.0.0.1',
                           '-mi', '127.0.0.1', '-min_rtp_port', '16000', '-max_rtp_port', '16100', '-timeout', '60s',
                           '-timeout_error', '-nostdin', check=False, timeout=180)
        self.assertEqual(sipp.returncode, 0, f'sipp: {sipp.stdout[-1500:]} {sipp.stderr[-1500:]}')

        rows, deadline = '', time.monotonic() + 60
        while time.monotonic() < deadline:
            rows = self.psql("SELECT call_id || '|' || coalesce(end_reason, '') || '|' || "
                             '(SELECT count(*) FROM window_scores w WHERE w.call_pk = c.id) FROM calls c')
            if rows.count('|bye|') == 1:
                break
            time.sleep(1)
        self.assertEqual(rows.count('\n'), 0, f'one call row: {rows!r}\n{self.diagnose()}')
        call_id, reason, windows = rows.split('|')
        self.assertEqual(reason, 'bye', rows)
        self.assertGreaterEqual(int(windows), 3, 'windows flushed to PostgreSQL')
        self.assertRegex(call_id, r'^[0-9A-Za-z._-]+$')
        record, deadline = {}, time.monotonic() + 30
        while time.monotonic() < deadline:
            detail = self.api_get(f'/v1/calls/{call_id}')
            record = json.loads(detail['body']) if detail['status'] == 200 else {}
            if record.get('status') == 'ended':
                break
            time.sleep(1)
        self.assertEqual(record.get('status'), 'ended', f'the API registry ends the call (via Redis): {record}')

        # FR-O3: no AVA pod reaches the internet; the control pod (no policy) does.
        control = self.in_pod('ava-lab-pbx', 'deploy/asterisk', 'bash', '-c', BASH_CONNECT, '1.1.1.1', '443',
                              check=False).stdout.strip()
        self.assertEqual(control, 'open', 'control: a pod without the policy reaches the internet from this node')
        egress = {}
        for workload in ('deploy/live', 'deploy/api'):
            probe = self.in_pod('ava', workload, 'python', '-c', EGRESS_PROBE, check=False)
            egress[workload] = probe.stdout.strip()
            self.assertEqual(probe.returncode, 0, f'{workload} reached the internet: {probe.stdout}')
        for workload, own_port in (('statefulset/postgres', '5432'), ('statefulset/redis', '6379')):
            # Positive control in the same pod (review L2): the probe can report "open".
            own = self.in_pod('ava', workload, 'sh', '-c', NC_CONNECT, '127.0.0.1', own_port, check=False).stdout.strip()
            self.assertEqual(own, 'open', f'{workload}: probe control on its own port')
            egress[workload] = self.in_pod('ava', workload, 'sh', '-c', NC_CONNECT, '1.1.1.1', '443',
                                           check=False).stdout.strip()
            self.assertEqual(egress[workload], 'blocked', f'{workload} reached the internet')
        # Review M2: a DNS query straight to a public resolver is blocked too (cluster DNS
        # still forwards names upstream: README "Egress").
        dns_control = self.in_pod('ava-lab-pbx', 'deploy/asterisk', 'bash', '-c', BASH_DNS, '8.8.8.8',
                                  check=False).stdout.strip()
        self.assertEqual(dns_control, 'answered', 'control: the public resolver answers UDP from this node')
        for workload in ('deploy/live', 'deploy/api'):
            direct_dns = self.in_pod('ava', workload, 'python', '-c', PY_DNS, '8.8.8.8', check=False).stdout.strip()
            egress[f'{workload} dns->8.8.8.8'] = direct_dns
            self.assertTrue(direct_dns.startswith('blocked'), f'{workload} queried a public resolver: {direct_dns}')

        # Ingress: the PBX namespace reaches none of the AVA services (the names resolve
        # there, so "blocked" is the policy, review L2).
        for host, port in (('postgres.ava.svc.cluster.local', '5432'), ('redis.ava.svc.cluster.local', '6379'),
                           ('api.ava.svc.cluster.local', '8000')):
            resolved = self.in_pod('ava-lab-pbx', 'deploy/asterisk', 'getent', 'hosts', host, check=False)
            self.assertEqual(resolved.returncode, 0, f'{host} does not resolve in the PBX namespace')
            reach = self.in_pod('ava-lab-pbx', 'deploy/asterisk', 'bash', '-c', BASH_CONNECT, host, port,
                                check=False).stdout.strip()
            self.assertEqual(reach, 'blocked', f'the PBX namespace reached {host}:{port}')
        # Inside the namespace only the named flows work.
        flows = {}
        for workload, host, port, expected in (('deploy/api', 'redis', '6379', 'open'),
                                               ('deploy/api', 'postgres', '5432', 'blocked'),
                                               ('deploy/live', 'api', '8000', 'blocked'),
                                               ('deploy/live', 'redis', '6379', 'open')):
            result = self.in_pod('ava', workload, 'python', '-c', PY_CONNECT, host, port, check=False).stdout.strip()
            flows[f'{workload}->{host}:{port}'] = result
            self.assertEqual(result.split()[0] if result else '', expected, f'{workload} -> {host}:{port}: {result}')

        # 20 Nov (PRD FR-A9 config file plus reload): an edit of the `ava-scoring` ConfigMap
        # reaches the running pod through the kubelet's in-place update of the mounted
        # folder (no subPath, no hash suffix) and live applies it; no restart (checked below).
        metrics_script = ('import urllib.request,sys; sys.stdout.write(urllib.request.urlopen('
                          '"http://127.0.0.1:9100/metrics", timeout=10).read().decode())')

        def config_version():
            text = self.in_pod('ava', 'deploy/live', 'python', '-c', metrics_script).stdout
            return float(next(line.split()[-1] for line in text.splitlines()
                              if line.startswith('ava_live_config_version ')))
        def config_records():  # live's config_* operations-log records, oldest first
            records = []
            for line in self.kubectl('-n', 'ava', 'logs', 'deploy/live', check=False).stdout.splitlines():
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                # Review M1: match the event name, not the text ("starting" has config_fingerprint).
                if isinstance(record, dict) and str(record.get('event', '')).startswith('config_'):
                    records.append(record)
            return records
        config_v = config_version()
        self.assertEqual(config_records(), [], 'nothing to report before the edit')
        document = json.loads((K8S / 'base' / 'scoring.json').read_text(encoding='utf-8'))
        document['scoring']['thresholds']['alert_enter'] = 0.8
        patched = self.kubectl('-n', 'ava', 'patch', 'configmap', 'ava-scoring', '--type', 'merge', '-p',
                               json.dumps({'data': {'scoring.json': json.dumps(document, indent=2)}}), check=False)
        self.assertEqual(patched.returncode, 0, f'{patched.stdout} {patched.stderr}')
        edited_at, reloaded = time.monotonic(), []
        while time.monotonic() - edited_at < 240 and not reloaded:  # kubelet sync period plus cache TTL
            reloaded = config_records()
            if not reloaded:
                time.sleep(3)
        reload_s = time.monotonic() - edited_at
        self.assertTrue(reloaded, f'the ConfigMap edit never reached live\n{self.diagnose()}')
        time.sleep(5)  # more than two watch intervals: a second record (e.g. a half-written read) would show
        reloaded = config_records()
        self.assertEqual([(r['event'], r.get('version')) for r in reloaded], [('config_reloaded', config_v + 1)],
                         reloaded)
        self.assertEqual(config_version(), config_v + 1)

        # Review L3: nothing restarted (the init containers wait for the storage).
        restarts = json.loads(self.kubectl('get', 'pods', '-n', 'ava', '-o', 'json').stdout)['items'] + \
            json.loads(self.kubectl('get', 'pods', '-n', 'ava-lab-pbx', '-o', 'json').stdout)['items']
        counts = {pod['metadata']['name']: sum(s.get('restartCount', 0) for s in
                                               pod['status'].get('initContainerStatuses', []) +
                                               pod['status']['containerStatuses'])
                  for pod in restarts}
        self.assertEqual(set(counts.values()), {0}, f'restarts: {counts}')

        users = {w: self.in_pod('ava', w, 'id', '-u').stdout.strip()
                 for w in ('deploy/live', 'deploy/api', 'statefulset/postgres', 'statefulset/redis')}
        self.assertEqual(users, {'deploy/live': '10001', 'deploy/api': '10001', 'statefulset/postgres': '70',
                                 'statefulset/redis': '999'})
        logs = ''.join(self.kubectl('-n', 'ava', 'logs', w, check=False).stdout
                       for w in ('deploy/live', 'deploy/api', 'statefulset/postgres', 'statefulset/redis'))
        for secret in (self.ari_password, self.db_password):
            self.assertNotIn(secret, logs, 'no password in any AVA pod log')
        self.assertNotIn(caller, logs, 'the caller number is not in the AVA logs')
        kubectl_version = self.kubectl('version', '-o', 'json', check=False).stdout
        server = json.loads(kubectl_version).get('serverVersion', {}).get('gitVersion') \
            if kubectl_version.strip() else None
        print(f'\nobserved: k3s {server}; call {call_id} stored with {windows} windows and ended in the API '
              f'registry; egress {egress}; in-namespace flows {flows}; ConfigMap edit applied by live '
              f'{reload_s:.0f} s after the patch (version {config_v:g} -> {config_v + 1:g})')


if __name__ == '__main__':
    unittest.main()
