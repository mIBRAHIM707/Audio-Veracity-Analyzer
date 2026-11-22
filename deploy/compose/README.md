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
| `db-setup` | `ava-api:dev` (`python -m services.store`) | one-shot at every `up`: migrations, then the read-only `ava_reader` login (20 Nov) |
| `api` | `ava-api:dev`, built from `deploy/api/Dockerfile` | the analysis API and dashboard (`python -m services.api`): file analysis with the AntiDeepfake baseline, `/v1/calls` and `WS /v1/events` relayed from Redis, stored history and alert search read from PostgreSQL as `ava_reader` |
| `prometheus` (profile `monitoring`) | `prom/prometheus:v3.16.0@sha256:e0498764…` | scrapes `live` and `api` every 15 s (FR-O6) |
| `grafana` (profile `monitoring`) | `grafana/grafana-oss:13.0.2@sha256:5dad0df1…` | the provisioned "AVA overview (lab)" dashboard (FR-O6) |

The `api` image (17 Nov, attempt 2) is built in two stages:
- `node:22-bookworm-slim@sha256:e9f565d3…` builds the dashboard from the committed
  lockfile (`npm ci --ignore-scripts`, then `tsc` and `vite build`);
- the runtime is the same `python:3.11-slim` digest as `live`, with
  `deploy/api/requirements-api-image.txt`. That is 35 packages (32 until psycopg joined
  on 20 Nov), including
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

## Stored history through a read-only login (20 Nov 2026; backlog 5.1; PRD FR-A3, FR-A6)

`api` serves `GET /v1/history/calls`, `GET /v1/calls/{id}/history` and `GET /v1/alerts`
(with CSV export) from PostgreSQL. It logs in as `ava_reader`, never as the owner `ava`.
The one-shot `db-setup` service runs before it at every `up`
(`python -m services.store reader-role`, `services/store/README.md`):
- it applies the migrations as the owner (live does the same; an advisory lock keeps
  them from interleaving);
- it creates `ava_reader`, or updates it if it exists: login, no other attributes,
  `default_transaction_read_only = on`, at most 8 connections;
- it grants SELECT on `calls`, `window_scores`, `alerts` and `schema_migrations` only, and
  revokes any other table or sequence privilege in the schema;
- it takes TEMP and large-object creation from `PUBLIC` in the `ava` database. That
  covers every non-superuser login there; the owner `ava` is a superuser.

So the accounts, sessions, audit and feedback tables are out of the API's reach, and so
are writes to the database. Two exceptions remain:
- the login can still connect to the cluster's other databases, which pg_hba.conf would
  restrict;
- it reads whole `calls` rows, including the start and end data the routes never serve.

The reader password (`AVA_DB_READER_PASSWORD`) is set as a SCRAM verifier computed in
the job, so the plain text never reaches a server log. Changing it in the env file and
running `up` again rotates it. `api` starts only after `db-setup` exits 0; a
refusal (for example, a reader password shorter than 12 characters) is one JSON line in
`docker compose logs db-setup`. Live does not depend on it.

Not covered: `--auth on` (FR-U7 accounts, feedback and the audit log) needs a login
that can write those tables. It stays off in this stack, which is also why the API is
published nowhere.

## Scoring settings: file plus reload (20 Nov 2026; PRD FR-A9, FR-I4)

`live` starts with `--scoring-config /config/scoring/scoring.json
--scoring-config-watch-s 10`. The folder `/config/scoring` is a read-only bind mount of
`AVA_SCORING_CONFIG_DIR`, by default `deploy/compose/config`, whose `scoring.json` holds
the architecture §7.3 defaults (a unit test checks it parses to exactly those). Live
rereads the file every 10 s (`AVA_SCORING_CONFIG_WATCH_S`, 1-60) and applies a valid
change to calls started afterwards; running calls keep their settings. Hop and the two
degraded-stride keys need a restart, and a change to them is refused. Outcomes go to
live's operations log (`config_reloaded`, `config_rejected`, `config_unreadable`,
`config_unchanged`) and `/metrics` (`ava_live_config_version`); details are in
`services/live/README.md`.

To change settings, copy the folder outside the repository, set
`AVA_SCORING_CONFIG_DIR` to the copy as an ABSOLUTE path, and edit by writing a temporary
file in that folder and renaming it over `scoring.json`. (A relative path resolves
against the Compose project directory, the folder of `compose.yaml`, not the env file's
folder.) The mount is a folder, not a file, on purpose: a single-file bind mount keeps
the old file, so a replacement by rename would never reach the container. Do not edit
the repository's default in place for a site.

Live refuses to start (exit 2, one `refused` line per restart attempt) when the file is
missing or invalid at start-up. A mistyped folder path does this too, because Docker
creates a missing bind-mount source as an empty folder. On a Linux host, the file must
also be readable by uid 10001. After start-up, a missing or invalid file only reports
and keeps the running settings.

Lab evidence (Claude-authored, 20 Nov): `test_a_scoring_config_edit_on_the_host_reaches_
live_without_a_restart` on this Windows host (Docker Desktop, WSL 2 bind mount). A
threshold edit was applied (version 1 to 2), a hop edit was refused as "needs a
restart", a removed file was reported as unreadable, and the restored file as
unchanged. There was no restart, and no path appeared in the log. Run time was 36 s.

## Reaching the dashboard

Nothing is published, and the API has no authentication (`services/api/README.md`).
`api` listens on port 8000 on the internal network only (`--allow-non-loopback`), and
only containers on that network reach it. A browser on the host therefore cannot open
the dashboard from this stack. Exposing it is a deployment decision:
- the address to publish on;
- a second, non-internal network, which also gives `api` a route out;
- authentication (P1 local authentication, backlog 7.3).
None of these is designed here. For a look on a development machine, run the API on the
host instead (`services/api/README.md`), or use the lab demo profile below.

## Demo profile (22 Nov 2026; backlog 11.2; PRD roadmap 5.9, BO-4, BO-8)

`--profile demo` adds `demo-proxy`, so a browser on THIS machine can open the dashboard
for the lab demo (`docs/operations/demo.md`). It is not a way to publish the dashboard to
users:
- `demo-proxy` runs `python -m services.edge` from the `ava-api:dev` image. That is a
  byte forwarder: standard library only, one fixed target `api:8000` taken from its
  command line, at most 32 connections, a 5 s connect timeout and a 300 s idle timeout.
  It logs one `listening` and one `stopped` record and nothing per connection. `Host`,
  `Origin` and WebSocket frames pass through unchanged. A stalled write counts against
  the idle timeout, a link that does not end cleanly is aborted, and after the API
  closes a connection the browser gets 10 s to close its side.
- Compose publishes it on `127.0.0.1:${AVA_DEMO_PORT:-8080}` only. The host side is fixed
  to loopback in `compose.yaml`; there is no setting for another address, and a unit
  test (`tests/unit/test_compose_demo_profile.py`) fails if one appears.
- Networks. `api` joins one more internal network, `demo`, which only `demo-proxy`
  shares. `demo-proxy` is also on `demo-edge`, the only non-internal network, because
  Docker publishes no port for a container on internal networks only. So the forwarder
  CAN reach the internet: on 22 Nov it connected to 1.1.1.1:443 from inside, even with
  the bridge's masquerading turned off (Docker Desktop NATs in its own VM), so that
  setting is not used. Its code connects only to the fixed `api:8000`, it gets no
  secret or mount, and it cannot reach `backend`. `api`, `live` and every other service
  have no default route at all. The lab test's
  egress proof checks both "no default route" and a failed connection for `live` and
  `api` with the profile on.
- Host names. `api` runs with `--allowed-host 127.0.0.1 --allowed-host localhost
  --allowed-host api` (`services/api/hosts.py`): a request whose Host header names
  anything else gets 421, and such a WebSocket is closed before it opens. This stops a
  web page from reaching the unauthenticated API through DNS rebinding (its own name
  resolved to 127.0.0.1). Open the dashboard as `127.0.0.1` or `localhost`.
- No authentication: anyone who can reach 127.0.0.1 on this machine can use the API
  while the profile runs, including other local users and programs. Stop it after the
  demo (`docker compose ... stop demo-proxy`, or `down`). It restarts only on failure,
  so it does not come back by itself after a reboot.
- Linux hosts (review M5, not tested): the forwarder listens on every interface of its
  container. Docker Desktop, used here, does not route the LAN to container addresses.
  On a Linux Docker host, a machine on the same LAN that adds a route to the
  `demo-edge` subnet through this host may reach `<container address>:8080` directly,
  past the loopback binding, if the host forwards IP and its firewall allows it (older
  Docker Engine versions did not block this). Run the demo profile on Linux only on a
  trusted network, or block forwarding to the `demo-edge` subnet.
- Hardening as for the other services: read-only root, every capability dropped,
  `no-new-privileges`, 64 processes, 0.25 CPU, 128 MB, no mounts, no secrets.

Lab evidence (Claude-authored, 22 Nov): `tests/lab/test_compose_demo_lab.py`. It runs the
browser rehearsal `tests/browser/compose-demo-rehearsal.spec.cjs` through the forwarder:
a real tapped call on the live board with its timeline, a batch upload, and the stored
history. It also checks that `/readyz` answers on 127.0.0.1 and not on the host's
network address, that another Host name is refused, that `live` and `api` have no
default route and cannot reach 1.1.1.1:443, and that the forwarder stops cleanly on
SIGTERM. Run times are in `docs/operations/demo.md`.

## Monitoring (profile `monitoring`, 18 Nov 2026; PRD FR-O6)

Add `--profile monitoring` to the `up` and `down` commands above. Prometheus 3.16.0
(Apache-2.0) and Grafana OSS 13.0.2 (AGPL-3.0) are both free and open source. They were
pulled on 18 Nov 2026 and pinned by version and index digest; the images are 389 MB and
1.47 GB on disk.

- Network. A second internal network, `metrics`, holds `live`, `api`, `prometheus` and
  `grafana`. It is always created, also without the profile. Its settings:
  - `AVA_METRICS_SUBNET`, default 10.231.250.0/24. This is outside Docker's default
    pools (172.17-172.31.x and 192.168.x).
  - `AVA_METRICS_DYNAMIC_RANGE`, default 10.231.250.128/25. Other containers get their
    addresses from this range.
  - `AVA_LIVE_METRICS_IP`, default 10.231.250.10: `live`'s fixed address.

  Change the three together. The fixed address must be inside the subnet and outside
  the dynamic range, or another container can take it and `live` cannot be created. If
  the subnet overlaps a network the host already uses, `up` fails for the whole stack.

  `live`'s health endpoint (`/healthz`, `/readyz`, `/metrics`, no authentication) binds
  to the fixed address only. Prometheus can scrape it; nothing on `backend` (Asterisk,
  PostgreSQL, Redis) reaches it. Until 18 Nov it was bound to 127.0.0.1. `prometheus`
  and `grafana` are on `metrics` only, so they cannot reach `backend` or the internet.
  Nothing is published on the host.
- Scraping. `monitoring/prometheus.yml` has jobs `ava-live` (`live:9100`) and `ava-api`
  (`api:8000`). The API's operations log skips `/metrics`, so scrapes add no log lines.
  Retention is 15 days or 2 GB (`AVA_PROMETHEUS_RETENTION`,
  `AVA_PROMETHEUS_RETENTION_SIZE`), in the `promdata` volume.
- Dashboard. `monitoring/grafana/dashboards/ava-overview.json`, provisioned read-only
  (edits in the UI are not saved). It covers each FR-O6 item:
  - latency per stage: the live loop stages, the detector call and the file-analysis
    stages, as p95;
  - queue depth: live windows waiting and in the batcher, and the file job queue;
  - active calls, with capacity and refusals;
  - alerts per minute: the rate of `ava_live_alerts_raised_total` x 60;
  - dropped windows: unscored outcomes and stride skips.
  It also shows the "visible unmonitored/low-quality conditions" of backlog 7.2:
  degraded and unmonitored directions, calls in sustained packet loss, RTP loss and
  concealment. Further panels cover tap cost per audio second, detector and loop health,
  storage and Redis publishing, API responses and the event relay.
- Grafana settings. The admin password comes from the env file
  (`AVA_GRAFANA_ADMIN_PASSWORD`). The Compose file needs it even without the profile, so
  a monitoring start never falls back to Grafana's default `admin`. This breaks existing
  env files written before 18 Nov: add the variable. Grafana applies the password only
  when its `grafanadata` volume is first created. To change it later, use Grafana
  itself or remove the volume. Logs go to the console only (`GF_LOG_MODE`), because the
  root filesystem is read-only. Anonymous access and
  sign-up are off. Everything that calls grafana.com is off: usage reporting, update
  checks, feedback links, the news feed, plugin preinstall, signing-key download, the
  plugin catalogue, external snapshots, public dashboards and cloud migration. The
  network blocks it as well. Grafana's `secret_key` is left at its default. The
  provisioned data source holds no secret, but set `GF_SECURITY_SECRET_KEY` before
  storing any.
- Hardening. Both containers have a read-only root with a `noexec` `/tmp`, drop every
  capability, set `no-new-privileges`, allow at most 256 processes, and are limited to
  0.5 CPU and 512 MB. Data lives in the volumes `promdata` and `grafanadata`.
- Reaching Grafana. Like the dashboard, Grafana is not published (see above). Publishing
  it on the host needs an address, a non-internal network and a decision on
  authentication beyond the single admin. Until then, use
  `docker compose ... exec grafana curl -u admin:<password> http://127.0.0.1:3000/api/...`,
  or the Prometheus API through `exec prometheus wget -qO- http://127.0.0.1:9090/...`.
- Not done: Prometheus alerting rules and Alertmanager (PRD FR-O6 does not ask for
  them), dashboards for PostgreSQL and Redis themselves (no exporters), and log
  shipping.

`tests/unit/test_monitoring_dashboard.py` (CI) renders the real live `/metrics`, and the
API's when FastAPI is installed. It checks that every series, label value and grouping
label used by a dashboard query is exported, and that the checker catches a renamed
metric or label. It also checks the pins, the telemetry settings and the network rules
in `compose.yaml`.

## Security and isolation

- One `internal: true` network: no service can reach the internet. That includes
  telemetry, downloads and CDNs, and was tested by a connection attempt from `live` and
  from `api`. Nothing is published on the host, except the opt-in `demo-proxy` on
  127.0.0.1 (profile `demo`, above), which is also the only container with a route out.
- `live`'s health endpoint (unauthenticated `/metrics`) listens only on its fixed
  address on the internal `metrics` network (18 Nov; see "Monitoring"). Its Compose
  health check reads `/readyz` there. This reverses the 17 Nov choice of 127.0.0.1
  (review L2):
  - `api`, `prometheus` and `grafana` can reach it, and so, probably, can processes on
    a Linux Docker host through the bridge. That host part is inferred, not tested.
  - The containers on `backend` cannot reach it, tested from `asterisk` with a
    positive control.
  - It exposes no call IDs. It does have 8 connection slots, each held for up to 5 s, so
    a misbehaving peer on `metrics` could starve the health check and the scrape. The
    container would then show unhealthy; calls are not affected.
  - Grafana's admin can add a data source pointing at `http://api:8000` and read
    `/v1/calls` through Grafana. Treat the Grafana admin password as giving read access
    to call metadata.
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
  digest. (21 Nov: `uv pip compile` was still refused by the runner's allowlist.)
- SBOM (FR-O7, 21 Nov): `scripts/image_sbom.py` writes a CycloneDX 1.5 JSON SBOM from
  inside a built image. It uses only the standard library, so nothing is added to the
  image. It records:
  - the image ID, and the base image digests that the Dockerfile names. The digests, the
    pins and the npm list come from the mounted checkout, not from the image (property
    `ava:source`). So build and generate from the same checkout, and pass
    `--source-revision "$(git rev-parse HEAD)"` (append `+dirty` if the tree has
    changes);
  - every installed Python distribution, with its stated licence, whether it is pinned,
    and the SHA-256 of its `RECORD`;
  - the Python interpreter;
  - every Debian package dpkg lists as installed;
  - for `api`, the dashboard's npm packages with their lockfile SHA-512. Build-only
    packages get scope `excluded`.

  `--check-pins` exits 1 unless the installed Python set equals the image's requirement
  file. The only exemptions are pip, setuptools and wheel, which come from the base
  image. The exemption is by name. Only the lab test
  (`tests/lab/test_image_sbom_lab.py`) checks that those three still have the base
  image's versions. Run it after a build, from the repository root:

  ```text
  docker run --rm --network none --read-only -v "$PWD:/src:ro" --entrypoint python ava-live:dev \
    -I /src/scripts/image_sbom.py --image-name ava-live \
    --image-id "$(docker image inspect ava-live:dev --format '{{.Id}}')" \
    --dockerfile /src/deploy/live/Dockerfile \
    --requirements /src/deploy/live/requirements-live.txt --root /src --check-pins \
    --source-revision "$(git rev-parse HEAD)" > ava-live.cdx.json
  ```

  For `api`, use `deploy/api/...` and add `--npm-lock /src/dashboard/package-lock.json`.
  On this host on 21 Nov, both pin checks matched. `live` had 109 components: 1 base
  image, 87 Debian, 20 Python and the interpreter. `api` had 242: 2 base images, 87
  Debian, 38 Python, 114 npm and the interpreter. The output is deterministic, with no
  timestamp unless `--timestamp` is given.

  The SBOM does not contain:
  - wheel hashes, which need the hashed lock above;
  - a dependency graph;
  - Debian licences, which dpkg's status file does not hold;
  - files outside the package managers.

  "Generated in CI" needs a workflow job that builds both images and runs the command
  above. The runner may not edit workflows, so a human must add it.
  `tests/unit/test_image_sbom.py` runs in CI. Its CycloneDX schema case skips there,
  because CI installs only `requirements-audio.txt`. It needs `requirements-test.txt`,
  which the workflow does not install yet, so the schema check has run only on this host.
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

## Model change and rollback (22 Nov 2026; backlog 10.QA; PRD FR-O4)

A model change is an env-file change (`AVA_ONNX_DIR`, `AVA_ONNX_FILE`, `AVA_CARD_DIR`,
and `AVA_SCORING_CONFIG_DIR` if the settings change too), followed by `up -d live`.
Keep the previous env file. Put each model version in its own folders and never
overwrite files in place (`write_card` already refuses to overwrite a card); otherwise
the previous env file no longer points at the previous model.

Live refuses before it opens anything if the new files do not agree with each other or
with the settings, and also if the model fails to load or warm up. It writes one
`refused` JSON line per restart attempt naming the problem, keeps restarting and never
becomes healthy. A model that agrees with its card but performs worse is NOT caught;
rolling back is the remedy there too.

Calls never stop, because the tap is passive. Calls that start while live refuses are
not monitored, and nothing is stored for them. `up -d live` stops the running container,
so a call in progress at that moment gets a stored `monitor_lost` end and may be
re-tapped as a second session once a working live is back (see the live-restart case in
the evidence above). Make model changes when no calls are active.

To roll back, put the previous env file back and run `up -d live` again. The stored rows
stay, and migrations already applied are not applied again.

Check after every change:

```text
docker compose ... ps live                  # healthy, not restarting
docker compose ... logs --no-log-prefix live   # the latest "starting" line shows model_version and onnx_sha256
```

Lab evidence (Claude-authored, 22 Nov):
`test_a_refused_model_change_never_stops_calls_and_a_rollback_restores_scoring`.
Live started with the 14 Nov static-INT8 export (untrained head) and stored a real call.
Three bad changes were then deployed through the env file:
- a card for "another export" (same file name, other SHA-256): refused with "ONNX file
  does not match the card (hash or size)", 7 attempts while a real call ran and
  completed. Nothing was stored for that call, and live never started during it;
- a model file name missing from the folder: refused with "ONNX model not found";
- a scoring file with a 2 s window: refused with "scoring config disagrees with the card
  ... differs from the model card window 4.0".
In each case live wrote no `starting` line, was never healthy, and put no host path in
the log, and no password appeared there. After the rollback, live was healthy in 7.2 s
and 6.9 s (two runs) with the same model version and ONNX hash. It applied no migration
(`schema_migrations` unchanged), kept the earlier row, and stored a new call to `bye`
with 5 windows. Run time was 92 s on this host, with the images already built.
Independent review: conditional pass, no High. M1 (calls in progress get `monitor_lost`)
and the Lows were fixed, and the case was run again. Two Nits about duplicated helper
code were not changed.

This covers the files on the model volume. It does not cover a new image version: roll
back an image by its previous tag (the images are tagged, not pinned by digest, see
above). There are no down migrations. If the newer image applied a migration, the older
image refuses to start against that database ("database has migration N ... that this
checkout does not know", `services/store/migrate.py`). So does an older `db-setup` (the
`api` image also migrates), and then `api` does not start. Rolling back such an upgrade
therefore also needs the database as it was before it, so back up PostgreSQL before an
image upgrade. The image upgrade itself is not tested. The backup and restore procedure
is in `docs/operations/deployment-guide.md` ("Backup and restore") and is lab-tested
(`test_a_database_backup_restores_into_a_new_stack_and_scoring_resumes`, 22 Nov).

## Not done

Publishing the dashboard to the host (see above), hashed requirements and SBOM generation in CI (FR-O7; the generator
exists, see above), log shipping (FR-O6), image
signing. The FR-O8 deployment guide is `docs/operations/deployment-guide.md` (22 Nov).
Grafana is done (profile `monitoring`, 18 Nov), but it is not published.
