# Compose deployment of the live stack (backlog 10.2; 17 Nov 2026)

PRD FR-O4 (model from a local volume, no runtime download), FR-O7 (partial: public
images pinned by digest, Python packages pinned by version with `--no-deps`, no hashes
yet), FR-O8 (partial), the no-telemetry constraint; architecture §2.4, §2.7, ADR-7. A
local, CPU-only LAB stack:

| Service | Image | Role |
|---|---|---|
| `postgres` | `postgres:16-alpine@sha256:f377d590…` | durable call, window and alert records (migrations applied by `live` at start) |
| `redis` | `redis:7-alpine@sha256:7f7d30b7…`, AOF on | live event fan-out to the API |
| `live` | `ava-live:dev`, built from `deploy/live/Dockerfile` | the tap + scorer (`python -m services.live`) |
| `asterisk` (profile `lab`) | `ava-lab-asterisk:dev`, built from `lab/asterisk` | the lab PBX |

The analysis API and dashboard (`python -m services.api`) are not in this stack yet:
their image needs torch for the file-analysis path (a separate, larger image).

## Scope: lab only

As written, this stack works only with the lab PBX inside it. All services share one
`internal: true` network with nothing published, so an Asterisk outside the stack
cannot:
- reach ARI or resolve the receiver host (`AVA_RECEIVER_HOST`, default `live`);
- send RTP to the receiver's ephemeral UDP ports.

`live` also cannot reach an outside `AVA_ARI_URL`. Without `--profile lab`, `live`
never becomes ready (it keeps retrying `http://asterisk:8088`). A deployment against a
real PBX needs a deliberate second network to the PBX (ARI over TLS, a fixed published
RTP port range, the PBX's address as `AVA_RECEIVER_HOST`'s peer). That network is not
designed or tested here.

## Build and run

```text
docker build -f deploy/live/Dockerfile -t ava-live:dev .
docker build -t ava-lab-asterisk:dev lab/asterisk
docker compose --env-file D:/AVA-Data/deploy/stack.env -f deploy/compose/compose.yaml --profile lab up -d --wait
docker compose --env-file D:/AVA-Data/deploy/stack.env -f deploy/compose/compose.yaml --profile lab down
```

Copy `stack.env.example` to a file outside the repository and fill it in: the ARI and
database passwords, the folders and file names of the exported model and its card, and
the Silero folder (all mounted read-only). Never commit the filled-in file. The live
service gets the ARI password, the database password (`PGPASSWORD`, kept out of the
URL so any character works) and the Redis URL only as environment variables, and
never prints them. Anyone with Docker access can read them (`docker compose config`,
`docker inspect`). Check the host first with
`python -m lab.preflight` (`lab/asterisk/README.md`).

## Security and isolation

- One `internal: true` network: no service can reach the internet. That includes
  telemetry, downloads and CDNs, and was tested by a connection attempt from `live`.
  Nothing is published on the host.
- Health is checked from inside the container (`/readyz` on 127.0.0.1:9100, the
  Compose health check), so no other container reaches the unauthenticated `/metrics`.
  To scrape it, bind it to a network address and attach a collector network
  deliberately.
- `live` runs as uid 10001 under `init`. It has a read-only root filesystem with only
  `/tmp` writable (64 MB, `noexec`), every capability dropped, `no-new-privileges` and
  at most 256 processes. Model, card and VAD are mounted read-only. The restart policy
  is `unless-stopped`, so the scorer returns after a host or Docker restart. A refused
  start (FR-O4: missing or mismatched model or card) therefore shows as a container
  restarting in `docker compose ps`, with one `refused` JSON line per attempt in its
  log. Watch for that after any change of model files.
- The image holds no weights, audio, keys or settings with secrets. The root
  `.dockerignore` admits only `requirements-*.txt`, `services/**/*.py`, the SQL
  migrations, `models/export/*.py` and `data/prepare/*.py`, so a stray file under those
  folders cannot reach the image.
- Packages: `pip install --no-deps` installs exactly the 17 pinned packages (the 17 Nov
  build resolved no others), followed by `pip check`. Hash verification (FR-O7) needs a
  hashed Linux lock. The command is `uv pip compile deploy/live/requirements-live.txt
  --python-version 3.11 --python-platform x86_64-manylinux_2_28 --generate-hashes -o
  deploy/live/requirements-live.lock`, then `--require-hashes`. It is outside the
  runner's command allowlist, so a human must run it. The built images `ava-live:dev`
  and `ava-lab-asterisk:dev` are referenced by tag, not digest.
- ONNX Runtime telemetry is off in three ways:
  - the image sets `ORT_DISABLE_TELEMETRY=1`;
  - the code forces it before the first import (`services/inference/ort_runtime.py`);
  - the network has no egress.
  ONNX Runtime's official builds send telemetry by default, through 1DS on Linux
  (onnxruntime `docs/Privacy.md`, read 17 Nov 2026). Without the variable, the 1.32.0
  wheel initialised its telemetry client at import in this image. The lab test asserts
  that no telemetry message appears in the live log. This was verified for Linux only;
  the Windows wheel's ETW backend, used on development machines, was not checked.
- Redis has no password: it is reachable only on the internal network. A deployment
  that shares the network must add `requirepass` (and a password in the URL).
- PostgreSQL uses a password from the env file. The image's own `trust` setting covers
  local socket connections inside its container only.

## Evidence (Claude-authored, 17 Nov)

`tests/lab/test_compose_stack_lab.py` (opt-in, `AVA_TEST_DOCKER_LAB=1`) builds a
disposable project (random name and passwords, env file in a temporary folder). It uses
the 14 Nov static-INT8 export with an UNTRAINED head, so the scores carry no meaning. A
real call with three public CSALT clips into extension 700 gave the results below. The
database password contained `/@+=%#`, which shows the password works outside the URL.
- `live` healthy through its Compose health check;
- no telemetry message in the live log;
- one `calls` row ending `bye`, with 5 windows in PostgreSQL;
- `call_started` and `call_ended` in the JSON event log;
- the start record showing ARI, Redis and migrations OK;
- no password in any container log, and no caller number in the live log (the lab
  PBX's own console logs caller IDs);
- a connection from `live` to 1.1.1.1:443 blocked;
- `live` running as uid 10001.

The project and its volumes are removed afterwards. Run time was about 32 s on this
host, with the images already built (the live image builds in about 95 s from the pip
wheels).

## Not done

The API/dashboard image and its Compose service, Kubernetes manifests and the
NetworkPolicy, hashed requirements and an SBOM (FR-O7), log shipping and Grafana
(FR-O6), image signing, and the full FR-O8 deployment guide (hardware, PBX tap
configuration, upgrade, backup).
