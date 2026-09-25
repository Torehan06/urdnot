# syntax=docker/dockerfile:1

# One pinned base image for both stages. The tag names the exact Python patch
# and Debian release. The digest makes the build reproducible. It is a multi-arch
# index digest, so it resolves to arm64 on Apple silicon and amd64 on CI.
ARG PYTHON_IMAGE=python:3.12.14-slim-trixie@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9

# ---------------------------------------------------------------------------
# builder: resolve and install dependencies into a self-contained venv.
# ---------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

# The venv has no pip of its own. The builder's pip installs into it, so the
# runtime image carries no package installer inside the venv.
RUN python -m venv --without-pip /opt/venv

# Copy only the dependency manifest. This layer and the install below are
# rebuilt only when requirements.txt changes, not on every code edit.
COPY app/requirements.txt /tmp/requirements.txt

# The cache mount keeps downloaded wheels between builds without adding them to
# a layer. --only-binary fails the build instead of compiling from source,
# so the image never needs a compiler.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip --python /opt/venv/bin/python install --only-binary=:all: \
        -r /tmp/requirements.txt

# ---------------------------------------------------------------------------
# runtime: the base image, the venv, and the application code, and nothing else.
# ---------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime

# A fixed, high UID/GID that doesn't collide with host users. It is also numeric,
# so Kubernetes `runAsNonRoot` can verify it.
RUN groupadd --system --gid 10001 urdnot \
 && useradd --system --uid 10001 --gid urdnot --no-create-home \
        --home-dir /nonexistent --shell /usr/sbin/nologin urdnot

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8000

WORKDIR /opt/urdnot

# Files stay owned by root, so the service user can read and execute its code
# but not modify it.
COPY --from=builder /opt/venv /opt/venv
COPY app/ ./app/

# Build metadata goes last. An ARG is visible to every later RUN, so a new
# REVISION per commit placed earlier would invalidate the cached layers below it.
ARG VERSION=0.1.0
ARG REVISION=unknown

LABEL org.opencontainers.image.title="urdnot-api" \
      org.opencontainers.image.description="Krogan-themed FastAPI service for a 12-week DevOps curriculum" \
      org.opencontainers.image.source="https://github.com/Torehan06/urdnot" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}"

ENV APP_VERSION=${VERSION}

USER 10001:10001

EXPOSE 8000

# The slim image has no curl, so the probe uses the interpreter that is already
# there. It reads $PORT at probe time, so a runtime port override still works.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --start-interval=1s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('PORT', '8000')}/healthz\", timeout=2)"]

# Exec form makes Python PID 1, so it receives SIGTERM from `docker stop` and
# drains in-flight requests. One Uvicorn process per container; scale with
# replicas, not workers.
CMD ["python", "-m", "app"]
