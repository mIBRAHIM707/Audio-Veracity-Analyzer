# AVA on k3s (single node)

Backlog 10.2; PRD FR-O2 (Kubernetes manifests with health probes, resource limits and
persistent volumes), FR-O3 (NetworkPolicy denies egress), FR-O4 (models from a local
volume), BO-7; architecture §7.2 and ADR-8. Compose (`deploy/compose`) stays the
development stack; these manifests are the deployment form the PRD asks for.

## Layout

- `base/`: namespace `ava` (Pod Security `restricted` enforced), ConfigMap
  `ava-config`, the model volume, PostgreSQL 16 and Redis 7 StatefulSets with their
  claims, the `live` scorer and the `api` Deployments, and the NetworkPolicies.
- `lab/`: overlay for the laboratory. Adds the Asterisk lab PBX in namespace
  `ava-lab-pbx` and points live's ARI URL and its PBX policy at it.

Not here yet: an Ingress with TLS for the dashboard (the API has no authentication;
publication waits for that decision), Prometheus and Grafana (Compose `monitoring`
profile only), SIPREC and the webhook (P2), and a registry (the AVA images are local
builds imported into the node).

## Deploying

1. Build the images and import them into the node's containerd (no registry):

   ```text
   docker build -f deploy/live/Dockerfile -t ava-live:dev .
   docker build -f deploy/api/Dockerfile -t ava-api:dev .
   docker save -o ava.tar ava-live:dev ava-api:dev
   sudo k3s ctr images import ava.tar
   ```

2. Put the model files on the node under `/var/lib/ava/models`: `onnx/<file>`,
   `card/model_card.json`, `vad/silero_vad.onnx` and `antideepfake/` (the checkpoint
   folder). Files must be readable, and folders traversable, by uid 10001; nothing
   needs write access. See `base/models.yaml`. The volume's `claimRef` names namespace
   `ava`: an overlay that renames the namespace must patch it too.

3. Write a site overlay outside this repository. It names the detector file, the
   PBX, and generates the Secrets from files that never enter Git. Write the secret
   files without a trailing newline (`printf '%s' ... > file`, not `echo`), or the
   newline becomes part of the password and ARI login fails:

   ```yaml
   # site/kustomization.yaml
   apiVersion: kustomize.config.k8s.io/v1beta1
   kind: Kustomization
   resources:
     - <path to>/deploy/k8s/base
   generatorOptions: {disableNameSuffixHash: true}
   secretGenerator:
     - name: ava-secrets
       namespace: ava
       files:
         - ari-password=secrets/ari-password   # the PBX's ARI password for user `ava`
         - db-password=secrets/db-password     # any characters; never inside a URL
   patches:
     - target: {kind: ConfigMap, name: ava-config}
       patch: |-
         - {op: replace, path: /data/AVA_ONNX_FILE, value: <file name>}
         - {op: replace, path: /data/AVA_ARI_URL, value: http://<PBX address>:8088}
     - target: {kind: NetworkPolicy, name: live-pbx}
       patch: |-
         - {op: replace, path: /spec/egress/0/to/0/ipBlock/cidr, value: <PBX address>/32}
         - {op: replace, path: /spec/ingress/0/from/0/ipBlock/cidr, value: <PBX address>/32}
   ```

4. `kubectl apply -k site/`, then `kubectl -n ava get pods`. `live` is ready only while
   its ARI stream is connected (FR-A8); an unedited base, with the documentation
   address 192.0.2.10, never becomes ready.

The PBX must send tapped RTP to the live pod's own address (the tap passes
`$(POD_IP)` to ExternalMedia), so the PBX needs a route to the pod network. On a
single k3s node with the PBX outside the cluster that is a host route to the node's
pod CIDR; this is a site decision, not settled here.

## Security controls

- Pod Security `restricted` on namespace `ava`: non-root users (10001 for AVA, 70 for
  PostgreSQL, 999 for Redis), no privilege escalation, all capabilities dropped,
  seccomp `RuntimeDefault`, read-only root filesystems with bounded memory `emptyDir`s
  for `/tmp`. No service account token is mounted (nothing calls the Kubernetes API).
- No hostPath in any pod (restricted refuses it): the models come from a static
  `PersistentVolume` bound to the claim `ava-models`, mounted read-only.
- Public images are pinned by digest (the same as Compose); local images use
  `imagePullPolicy: Never`, so the node never pulls an AVA image.
- Secrets only by reference (`ava-secrets`); none in Git, none in the ConfigMap; the
  database URL carries no password (libpq reads `PGPASSWORD`).

## Egress

`default-deny` denies every ingress and egress in the namespace; named policies then
allow DNS to kube-dns, live to PostgreSQL, Redis and the PBX's ARI, api to Redis, the
PBX's RTP into live (UDP 32768-60999, Linux's default ephemeral range where the
receivers bind; change it with the node's `net.ipv4.ip_local_port_range`), and api
ingress from pods labelled `ava.giki/api-client: "true"` (none by default). Kubelet
probes come from the node, which k3s's policy controller admits (observed in the lab).

live and api set `shareProcessNamespace: true`, so the pause process is PID 1 and a
stop during the model load is not ignored (Compose: `init: true`). Each waits for its
storage in an init container (same image, at most 5 minutes) instead of restarting
while PostgreSQL or Redis comes up.

Limit (FR-O3 not fully met): cluster DNS forwards outside names upstream, so an AVA
pod can still resolve a public name (the query leaves the node through CoreDNS, a
possible leak of query names) though it cannot connect to the address, nor query a
public resolver directly. A site that must stop DNS lookups too restricts CoreDNS's
forwarders; FR-O3's whole-stack run with outbound DNS and HTTP blocked is not yet done
on k3s.

## Evidence

Static checks: `tests/unit/test_k8s_manifests.py`, Claude-authored (19 Nov 2026), 15
cases. They need PyYAML from `requirements-test.txt` (kept out of
`requirements-audio.txt`, which the service images include); the CI workflow installs
only `requirements-audio.txt`, so in CI 14 of them skip until a human adds
`requirements-test.txt` to the workflow (the automation may not edit workflows). The
one that runs everywhere checks that no image requirement file includes the test pins.

Lab (opt-in, `AVA_TEST_K3S_LAB=1`): `tests/lab/test_k3s_stack_lab.py`, Claude-authored
(19 Nov 2026), on rancher/k3s v1.34.2+k3s1 (digest-pinned) in Docker Desktop 29.9.3:

```text
uv run --no-project --python 3.11 --env-file <file with AVA_TEST_K3S_LAB=1> \
  --with-requirements requirements-audio.txt --with-requirements requirements-store.txt \
  --with-requirements requirements-export.txt python -m unittest tests.lab.test_k3s_stack_lab -v
```

First run (before review fixes), OK in 147 s: every pod ready under Pod Security
restricted with no Pod Security warning; a real SIPp call from the lab PBX tapped from
the other namespace, stored with `bye` and 5 windows, ended in the API registry; TCP to
1.1.1.1:443 refused from live, api, postgres and redis while the PBX pod (no policy)
connected; api -> redis open, api -> postgres and live -> api blocked; the PBX
namespace blocked from postgres, redis and api. Images built on 19 Nov; the detector
is the untrained 14 Nov export, so scores carry no meaning.

After the review fixes (same day), OK in 217 s: no pod restarted (live waited in its
init container); the call stored with `bye` and 5 windows and ended in the API; own-
port controls open in postgres and redis; a raw UDP DNS query to 8.8.8.8 timed out from
live and api; the PBX namespace blocked from all three services although their names
resolve there. (A run in between failed only because Docker Desktop's WSL engine
crashed mid-run.) Kubelet httpGet probes worked under default-deny.
