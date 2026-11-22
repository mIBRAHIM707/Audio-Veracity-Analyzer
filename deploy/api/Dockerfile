# AVA analysis API and dashboard image (backlog 10.2; PRD FR-A1/A3/A4/A8, FR-U1-U4,
# FR-O4 "model artifacts load from a local volume; no model download at runtime", FR-O7
# images pinned by digest; architecture §2.5, §2.6 "served by the API on the same
# origin", ADR-6). Build from the repository root (the root .dockerignore admits only
# the code, dashboard sources and requirement files the images need):
#   docker build -f deploy/api/Dockerfile -t ava-api:dev .
# The AntiDeepfake checkpoint comes from a read-only mount at run time; the image holds
# no weights, audio, keys or configuration with secrets. CPU only (PyTorch CPU wheels).

# Stage 1: the dashboard, built from the committed lockfile. Packages are downloaded at
# build time only; the result is static files with relative paths and no CDN.
FROM node:22-bookworm-slim@sha256:e9f565d32859f0cbfb52125eafe8be39390f24dac302dc3cdf566e8ba3ab21ee AS dashboard
ENV NPM_CONFIG_UPDATE_NOTIFIER=false \
    NPM_CONFIG_FUND=false \
    NPM_CONFIG_AUDIT=false
WORKDIR /build
COPY dashboard/package.json dashboard/package-lock.json ./
# --ignore-scripts: no install script runs (esbuild and rollup find their platform
# packages without one).
RUN npm ci --ignore-scripts
COPY dashboard/index.html dashboard/tsconfig.json dashboard/vite.config.ts ./
COPY dashboard/src/ ./src/
RUN npm run build

# Stage 2: the API.
FROM python:3.11-slim@sha256:07d4824036af339fa1b6ef769cc99d9c10a141769538b52b3b597c84f7a7b535

# ORT_DISABLE_TELEMETRY: nothing in this image imports ONNX Runtime; set anyway so a
# later import cannot start its telemetry (see deploy/live/Dockerfile).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    ORT_DISABLE_TELEMETRY=1

# --no-deps: exactly the pinned set in requirements-api-image.txt (35 packages since psycopg
# joined on 20 Nov), so a
# build never pulls an unpinned dependency; --only-binary: no source builds.
COPY requirements-*.txt deploy/api/requirements-api-image.txt /tmp/requirements/
RUN pip install --no-cache-dir --no-deps --only-binary=:all: -r /tmp/requirements/requirements-api-image.txt \
 && pip check \
 && rm -rf /tmp/requirements \
 && useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin ava

WORKDIR /app
COPY services/ /app/services/
COPY models/smoke/antideepfake.py /app/models/smoke/antideepfake.py
COPY data/prepare/ /app/data/prepare/
COPY --from=dashboard /build/dist/ /app/dashboard/

USER ava
ENTRYPOINT ["python", "-m", "services.api"]
CMD ["--help"]
