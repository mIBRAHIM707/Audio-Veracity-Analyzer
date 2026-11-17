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
| `api` | `ava-api:dev`, built from `deploy/api/Dockerfile` | the analysis API and dashboard (`python -m services.api`): file analysis with the AntiDeepfake baseline, `/v1/calls` and `WS /v1/events` relayed from Redis |

The `api` image (17 Nov, attempt 2) is built in two stages:
- `node:22-bookworm-slim@sha256:e9f565d3…` builds the dashboard from the committed
  lockfile (`npm ci --ignore-scripts`, then `tsc` and `vite build`);
- the runtime is the same `python:3.11-slim` digest as `live`, with
  `deploy/api/requirements-api-image.txt`. That is 32 packages, including
  `torch==2.8.0+cpu` from the PyTorch CPU index, so there are no CUDA wheels.
The image is 1.44 GB on disk (305 MB compressed) and took about 6 minutes to build here
on a slow link. The checkpoint (`AVA_MODEL_ROOT`) is mounted read-only. `/readyz`
verifies the checkpoint's pinned hashes, loads it and runs one warm-up inference. In
the 17 Nov lab runs that took 26.3 s with 4 threads, then 22.3 s with the 2-CPU limit. Model-load caveat, observed on 17 Nov: with no writable
`/tmp`, loading the model fails. torch 2.8 creates a `TemporaryDirectory` in
`torch.distributed.nn.jit.instantiator` when torchaudio builds the backbone
(`FileNotFoundError: No usable temporary directory`). The tmpfs provides it.

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
docker build -f deploy/api/Dockerfile -t ava-api:dev .
docker build -t ava-lab-asterisk:dev lab/asterisk
docker compose --env-file D:/AVA-Data/deploy/stack.env -f deploy/compose/compose.yaml --profile lab up -d --wait
docker compose --env-file D:/AVA-Data/deploy/stack.env -f deploy/compose/compose.yaml --profile lab down
```

Copy `stack.env.example` to a file outside the repository and fill it in: the ARI and
database passwords, the folders and file names of the exported model and its card, the
Silero folder and the AntiDeepfake checkpoint folder (all mounted read-only). Never commit the filled-in file. The live
service gets the ARI password, the database password (`PGPASSWORD`, kept out of the
URL so any character works) and the Redis URL only as environment variables, and
never prints them. Anyone with Docker access can read them (`docker compose config`,
`docker inspect`). Check the host first with
`python -m lab.preflight` (`lab/asterisk/README.md`).

## Reaching the dashboard

Nothing is published, and the API has no authentication (`services/api/README.md`).
`api` listens on port 8000 on the internal network only (`--allow-non-loopback`), and
only containers on that network reach it. A browser on the host therefore cannot open
the dashboard from this stack. Exposing it is a deployment decision:
- the address to publish on;
- a second, non-internal network, which also gives `api` a route out;
- authentication (P1 local authentication, backlog 7.3).
None of these is designed here. For a look on a development machine, run the API on the
host instead (`services/api/README.md`).

## Security and isolation

- One `internal: true` network: no service can reach the internet. That includes
  telemetry, downloads and CDNs, and was tested by a connection attempt from `live` and
  from `api`. Nothing is published on the host.
- `live`'s health is checked from inside its container (`/readyz` on 127.0.0.1:9100,
  the Compose health check), so no other container reaches `live`'s unauthenticated
  `/metrics`. To scrape it, bind it to a network address and attach a collector network
  deliberately.
- `api` is different (review M2): it listens on 0.0.0.0:8000 without authentication.
  Every container on `backend` can reach all of its routes: `postgres`, `redis`,
  `asterisk` and `live`. The routes are `/metrics`, `/v1/calls`, `WS /v1/events` and
  `/v1/analyze`. That is accepted for this lab network only. Anything else placed on the
  network can read call metadata and submit files.
- CPU and memory (review M1): `api` is limited to 2 CPUs (`AVA_API_CPUS`), 3 GB of
  memory (`AVA_API_MEMORY`) and 2 torch threads (`AVA_API_THREADS`). That keeps a long
  file analysis from taking the whole machine from the live scorer. It is a coarse
  bound only. PRD FR-I9 says file jobs get at most 25 % of inference capacity while
  calls are active, with live first. That rule works inside one process (the
  micro-batcher, `services/inference/capacity.py`). Between these two containers it is
  NOT enforced. The 3 GB is an estimate, not measured at peak. The lab test analysed
  one short clip. The peak would be the model (~1.3 GB) plus two 100 MB uploads, four
  queued jobs and the tmpfs, which counts against the same limit. If that goes over,
  the kernel kills the API, Docker restarts it, and in-memory jobs are lost.
- `api`'s `/readyz` stays 200 after its first warm-up, also through a Redis outage
  (review L7). Relay loss is reported in its `/metrics` (`ava_event_relay_*`) and to
  WebSocket clients as `stream.gap`, not by readiness.
- `live` and `api` run as uid 10001 under `init`. Each has a read-only root filesystem
  with only `/tmp` writable (`noexec`): 64 MB for `live`, and 256 MB for `api`, where
  uploads spool (2 concurrent, up to 100 MB each). Both drop every capability, set
  `no-new-privileges` and allow at most 256 processes. `api`'s health check calls
  `/readyz` inside the container with a 180 s start period for the first model load. Model, card and VAD are mounted read-only. The restart policy
  is `unless-stopped`, so the scorer returns after a host or Docker restart. A refused
  start (FR-O4: missing or mismatched model or card) therefore shows as a container
  restarting in `docker compose ps`, with one `refused` JSON line per attempt in its
  log. Watch for that after any change of model files.
- The images hold no weights, audio, keys or settings with secrets. The root
  `.dockerignore` admits only:
  - `requirements-*.txt` and the two image requirement files;
  - `services/**/*.py` and the SQL migrations;
  - `models/export/*.py` and `models/smoke/antideepfake.py`;
  - `data/prepare/*.py`;
  - the dashboard sources (`package.json`, the lockfile, `index.html`, the configs and
    `src/**/*.ts|tsx|css`; there is no `public/` folder).
  So a stray file under those folders cannot reach an image.
- Packages: `pip install --no-deps` installs exactly the pinned set, followed by
  `pip check`. That is 17 packages for `live` (the 17 Nov build resolved no others) and
  32 for `api`; the `api` file pins its transitive packages, resolved by
  `pip install --dry-run --report` in the base image. `tests/unit/test_deploy_images.py`
  (CI) checks three things: every base image is pinned by digest, every package is
  pinned exactly and never twice, and no index is allowed except the PyTorch CPU one.
  It does not prove the set is complete (review L5). `pip check` sees only declared
  dependencies, so a new import of a package outside the set builds fine. It fails at
  run time: at start-up, at model load or on the first request, which the lab test
  exercises.
  With that extra index, pip took `packaging` and `Jinja2` from download.pytorch.org;
  same versions, but only hashes would prove the same files.
- Licences of the 32 `api` packages, read from their metadata in the built image on
  17 Nov: BSD-3-Clause or BSD (torch, torchaudio, numpy, starlette, uvicorn, httpx,
  httpcore, click, idna, fsspec, Jinja2, MarkupSafe, networkx, sympy, mpmath, psutil,
  websockets); MIT (fastapi, pydantic, pydantic_core, annotated-types, anyio, filelock,
  h11, redis, typing-inspection); Apache-2.0 (python-multipart, safetensors);
  Apache-2.0 or BSD (packaging); PSF-2.0 (typing_extensions); MPL-2.0 (certifi);
  LGPL-2.1-or-later (soxr, used as a library). All are free and open source. Hash verification (FR-O7) needs a
  hashed Linux lock. The command is `uv pip compile deploy/live/requirements-live.txt
  --python-version 3.11 --python-platform x86_64-manylinux_2_28 --generate-hashes -o
  deploy/live/requirements-live.lock`, then `--require-hashes`. It is outside the
  runner's command allowlist, so a human must run it (the same applies to
  `deploy/api/requirements-api-image.txt`, with the PyTorch CPU index). The built images
  `ava-live:dev`, `ava-api:dev` and `ava-lab-asterisk:dev` are referenced by tag, not
  digest.
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

17 Nov, attempt 2: the same test now also brings up `api` with the real AntiDeepfake
checkpoint and checks:
- `api` healthy through its health check, with `/readyz` 200 (warm-up 26.3 s);
- the dashboard's `index.html` and every script and stylesheet it references served.
  The references are all relative bundled files, and none imports code from a URL;
- before the call, the API's relay reports `ava_event_relay_subscribed 1`, since Redis
  pub/sub drops anything published before the subscription;
- a WebSocket subscriber inside `api` receives the real call through
  live -> Redis -> relay. In the run, that was 7 events from `call.started` to
  `call.ended`, including `score.updated`, with strictly increasing seqs and no
  `stream.gap`;
- `/v1/calls/{id}` shows the call `ended`;
- a public CSALT clip posted to `/v1/analyze` returns 200 with the start record's model
  ID and per-window scores. The 4.8 s clip gives one window, so the verdict was
  `undetermined`. This is the uncalibrated baseline, not an accuracy result;
- the start record shows live events and Redis reachable, with the dashboard on;
- no caller number in the `api` log;
- `api` cannot reach 1.1.1.1:443 and runs as uid 10001.
Run time was 69 s with the images built.

## Not done

Publishing the dashboard to the host (see above), Kubernetes manifests and the
NetworkPolicy, hashed requirements and an SBOM (FR-O7), log shipping and Grafana
(FR-O6), image signing, and the full FR-O8 deployment guide (hardware, PBX tap
configuration, upgrade, backup).
