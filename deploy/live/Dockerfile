# AVA live service image (backlog 10.2; PRD FR-O4 "model artifacts load from a local
# volume; no model download at runtime", FR-O7 images pinned by digest; architecture
# deployment). Build from the repository root (the root .dockerignore admits only the
# code and requirement files this image needs):
#   docker build -f deploy/live/Dockerfile -t ava-live:dev .
# The model, its card and the Silero VAD come from read-only mounts at run time; the
# image holds no weights, audio, keys or configuration with secrets. CPU only.
FROM python:3.11-slim@sha256:07d4824036af339fa1b6ef769cc99d9c10a141769538b52b3b597c84f7a7b535

# ORT_DISABLE_TELEMETRY: ONNX Runtime's official builds send telemetry by default (1DS
# on Linux); the code forces this too (services/inference/ort_runtime.py), this covers
# anything else in the image.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    ORT_DISABLE_TELEMETRY=1

# --no-deps: exactly the pinned set (17 packages; the 17 Nov build resolved no others),
# so a build never pulls an unpinned dependency. Not yet hash-verified (FR-O7 partial:
# a hashed Linux lock needs `uv pip compile --generate-hashes`, see deploy/compose/README.md).
COPY requirements-*.txt deploy/live/requirements-live.txt /tmp/requirements/
# --only-binary: a pin without a Linux wheel fails the build instead of compiling from
# source with unpinned build tools.
RUN pip install --no-cache-dir --no-deps --only-binary=:all: -r /tmp/requirements/requirements-live.txt \
 && pip check \
 && rm -rf /tmp/requirements \
 && useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin ava

WORKDIR /app
COPY services/ /app/services/
COPY models/export/ /app/models/export/
# The shared train/serve conditioning (services/inference/analysis.py imports it).
COPY data/prepare/ /app/data/prepare/

USER ava
# RTP from the PBX arrives on ephemeral UDP ports; the health endpoint port is chosen
# with --health-port. Nothing is listened to unless the command line asks for it.
ENTRYPOINT ["python", "-m", "services.live"]
CMD ["--help"]
