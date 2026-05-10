# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

# syntax=docker/dockerfile:1.7

# ---------- Builder ----------------------------------------------------------
# Resolves and installs locked dependencies into /app/.venv. The two-step
# `uv sync` (deps first, then project) keeps the dependency layer cacheable
# across source-only rebuilds — see
# https://docs.astral.sh/uv/guides/integration/docker/.
FROM python:3.12-slim AS builder

# Pin uv at a digest in CI when reproducibility matters; the ":latest" tag
# is acceptable for the chassis bootstrap.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

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
# so the wheel ends up in the venv rather than as a `.pth` reference.
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# ---------- Runtime ----------------------------------------------------------
FROM python:3.12-slim AS runtime

# Non-root user (uid 1001 — fixed for k8s SecurityContext alignment).
RUN useradd --system --uid 1001 --gid 0 --no-create-home --shell /usr/sbin/nologin meho

# Build-identity stamps surfaced via GET /version. CI passes these via
# `docker build --build-arg GIT_SHA=$(git rev-parse HEAD) \
#                --build-arg BUILD_DATE=$(date -u +%Y-%m-%dT%H:%M:%SZ)`
# (G2.4 will pin that exact invocation). When unset, /version falls back
# to "unknown" so locally-built images don't crash on startup.
ARG GIT_SHA=unknown
ARG BUILD_DATE=unknown

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
