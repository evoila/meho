# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

# syntax=docker/dockerfile:1.7

# ---------- Base image (digest-pinned manifest list) ------------------------
# Pin the base image by its OCI manifest-list digest, not by the floating
# `:3.12-slim` tag. The manifest-list digest indirects into the per-arch
# image (amd64, arm64, …) automatically when buildx builds for multiple
# `--platform` targets — same line, correct arch, byte-identical input.
#
# Refresh policy: every digest bump lands in a dedicated PR titled
# "chore(backend): bump python:3.12-slim base digest to <new>" so the
# supply-chain audit trail records when and why the input moved. Run
# `docker manifest inspect python:3.12-slim` (or the Docker Hub `tags`
# API for the current digest) to verify before bumping.
#
# Pinned digest : sha256:ec948fa5f90f4f8907e89f4800cfd2d2e91e391a4bce4a6afa77ba265bc3a2fe
# Recorded on   : 2026-05-10
# Source        : docker.io/library/python:3.12-slim (Debian bookworm,
#                 Python 3.12.x, multi-arch manifest list)
ARG PYTHON_BASE_DIGEST=sha256:ec948fa5f90f4f8907e89f4800cfd2d2e91e391a4bce4a6afa77ba265bc3a2fe

# ---------- Builder ----------------------------------------------------------
# Resolves and installs locked dependencies into /app/.venv. The two-step
# `uv sync` (deps first, then project) keeps the dependency layer cacheable
# across source-only rebuilds — see
# https://docs.astral.sh/uv/guides/integration/docker/.
#
# Multi-arch invariant: buildx runs this whole Dockerfile once per target
# `--platform` (e.g. linux/amd64 and linux/arm64). We do *not* split the
# builder onto `--platform=$BUILDPLATFORM` because the backplane depends
# on compiled-wheel packages (`asyncpg`, `cryptography`, `pydantic-core`)
# that must be installed for the **target** arch — cross-compiling those
# from a builder running on a different arch is fragile. Instead we
# accept QEMU emulation cost for the non-native target (typically 3–5×
# slower than native for arm64 on amd64 hosts; documented in
# `docs/codebase/backend.md`). `BUILDPLATFORM` and `TARGETPLATFORM` are
# auto-populated by BuildKit and surfaced in the build log so CI runs
# show which arch each layer compiled on.
FROM python@${PYTHON_BASE_DIGEST} AS builder

ARG TARGETPLATFORM
ARG BUILDPLATFORM
RUN echo "Builder running on BUILDPLATFORM=${BUILDPLATFORM} for TARGETPLATFORM=${TARGETPLATFORM}"

# uv installer image — pinned to a specific version AND manifest-list digest
# so the build is byte-identical across rebuilds and the supply-chain audit
# names the exact uv binary. Refresh in the same dedicated PR that bumps
# PYTHON_BASE_DIGEST when the surrounding toolchain moves.
#
# Pinned uv     : 0.11.12
# Manifest-list : sha256:3a59a3cdd5f7c217faa36e32dbc7fddbb0412889c2a0a5229f6d790e5a019dd7
# Verify        : crane digest ghcr.io/astral-sh/uv:0.11.12
COPY --from=ghcr.io/astral-sh/uv:0.11.12@sha256:3a59a3cdd5f7c217faa36e32dbc7fddbb0412889c2a0a5229f6d790e5a019dd7 /uv /uvx /bin/

ENV UV_PYTHON_DOWNLOADS=0 \
    UV_LINK_MODE=copy \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Step 1: install dependencies only (cacheable).
# README.md is co-copied here because hatchling resolves
# `[project].readme = "README.md"` at wheel-build time during the second
# `uv sync` below; staging it in the deps-only layer costs ~1 KB on a
# cache hit and avoids needing a separate COPY before that step.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

# Step 2: copy the project source and install the project itself, non-editable
# so the wheel ends up in the venv rather than as a `.pth` reference. The
# wheel build pulls in `alembic.ini` and `alembic/` via hatch's
# `[tool.hatch.build.targets.wheel.force-include]` (see backend/pyproject.toml);
# hatch errors out when a force-include source is missing, so both must
# exist in the build context at the moment of `uv sync --no-editable`.
COPY src ./src
COPY alembic ./alembic
COPY alembic.ini ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# ---------- Runtime ----------------------------------------------------------
FROM python@${PYTHON_BASE_DIGEST} AS runtime

# Non-root user (uid 1001 — fixed for k8s SecurityContext alignment).
RUN useradd --system --uid 1001 --gid 0 --no-create-home --shell /usr/sbin/nologin meho

# Build-identity stamps surfaced via GET /version. CI passes these via
# `docker build --build-arg GIT_SHA=$(git rev-parse HEAD) \
#                --build-arg BUILD_DATE=$(date -u +%Y-%m-%dT%H:%M:%SZ)`
# (G2.4-T2 wires the exact invocation). When unset, /version falls back
# to "unknown" so locally-built images don't crash on startup.
ARG GIT_SHA=unknown
ARG BUILD_DATE=unknown

# OCI image annotations — the published image is self-describing and the
# registry can link back to the source repo, license, and revision without
# a separate metadata store. Spec:
# https://github.com/opencontainers/image-spec/blob/main/annotations.md
LABEL org.opencontainers.image.source="https://github.com/evoila/meho" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.title="meho-backplane" \
      org.opencontainers.image.description="MEHO governance-layer backplane (HTTP server, observability, audit, federation)." \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.vendor="evoila"

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    GIT_SHA=${GIT_SHA} \
    BUILD_DATE=${BUILD_DATE}

WORKDIR /app

# Copy only the prepared venv from the builder; source ships inside the wheel
# installed in /app/.venv.
COPY --from=builder --chown=meho:0 /app/.venv /app/.venv

USER 1001:0

EXPOSE 8000

# Two execution modes share this image:
#
# 1. **Serve** (default) — uvicorn runs the FastAPI app on :8000. This
#    is what the Deployment rolls; the CMD below is the unmodified
#    invocation.
#
# 2. **Migrate** — the Helm pre-install / pre-upgrade Job (G2.5-T3)
#    overrides the container command to run
#    ``python -m meho_backplane.db.migrate`` before the Deployment
#    rolls forward, so the schema is at ``alembic upgrade head`` by
#    the time pods come up. No additional Dockerfile entrypoint is
#    needed: ``/app/.venv/bin`` is on PATH (set above), the
#    ``meho_backplane`` package is installed into the venv via the
#    builder stage, and ``alembic.ini`` + the ``alembic/`` tree ship
#    as wheel package data (force-included by ``pyproject.toml``) so
#    ``meho_backplane.db.migrate`` resolves the config via
#    ``importlib.resources`` without needing the source tree on disk.
#
#    The Helm Job spec passes:
#        command: ["python"]
#        args: ["-m", "meho_backplane.db.migrate"]
#    plus ``env: [{name: DATABASE_URL, valueFrom: ...}]``. The runner
#    exits 0 on success, 1 on failure; ``backoffLimit`` on the Job
#    handles retries.
#
# Exec form forwards SIGTERM cleanly to uvicorn (k8s pod-shutdown contract).
CMD ["uvicorn", "meho_backplane.main:app", "--host", "0.0.0.0", "--port", "8000"]
