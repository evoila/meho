# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

# syntax=docker/dockerfile:1.7

# ---------- Base image (digest-pinned, tag-qualified manifest list) ---------
# Pin the base image by its OCI manifest-list digest, not by the floating
# `:3.12-slim` tag alone. The manifest-list digest indirects into the
# per-arch image (amd64, arm64, …) automatically when buildx builds for
# multiple `--platform` targets — same line, correct arch, byte-identical
# input.
#
# The pin is written *inline on both FROM lines* as
# `python:3.12-slim@sha256:<digest>` (tag + digest) rather than via an
# `ARG`, because Dependabot's docker ecosystem only bumps literal,
# tag-qualified `FROM` references — it does not resolve an ARG-held
# digest (dependabot-core#2057 / #4597). With the literal form the
# docker block in `.github/dependabot.yml` opens an automatic
# `chore(deps)` PR whenever upstream rebuilds the tag, so the digest no
# longer silently ages. Both FROM lines carry the SAME digest and move
# together in that PR. The uv installer image pin below is refreshed via
# the separate dedicated-PR convention documented there.
#
# Pinned digest : sha256:c3d81d25b3154142b0b42eb1e61300024426268edeb5b5a26dd7ddf64d9daf28
# Recorded on   : 2026-07-15
# Source        : docker.io/library/python:3.12-slim (Debian trixie,
#                 Python 3.12.x, multi-arch manifest list)

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
FROM python:3.14-slim@sha256:d3400aa122fa42cf0af0dbe8ec3091b047eac5c8f7e3539f7135e86d855dc015 AS builder

ARG TARGETPLATFORM
ARG BUILDPLATFORM
RUN echo "Builder running on BUILDPLATFORM=${BUILDPLATFORM} for TARGETPLATFORM=${TARGETPLATFORM}"

# uv installer image — pinned to a specific version AND manifest-list digest
# so the build is byte-identical across rebuilds and the supply-chain audit
# names the exact uv binary. Refresh in a dedicated PR when the surrounding
# toolchain moves (this pin is NOT tracked by the Dependabot docker block,
# which watches the `python:3.12-slim` base FROM lines).
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

# ---------- Operator-console CSS (Initiative #337, Task #863) ---------------
# Build the compiled Tailwind 4 + DaisyUI 5 bundle for the /ui surface using
# the **standalone Tailwind CLI binary** -- no `node_modules`, no `npm`, no
# parallel CI job (decision #10 in docs/decisions/locked-decisions.md).
#
# The CLI is pinned by version AND SHA256, mirroring the kubeconform install
# pattern in .github/workflows/ci.yml so the supply-chain audit trail records
# the exact byte sequence we trust to run. Refresh policy: bump in a
# dedicated PR titled "chore(ui): bump Tailwind CLI to <version>" that
# updates BOTH the version and the per-arch SHA256s here AND the version
# row in backend/src/meho_backplane/ui/static/src/vendor/VENDOR.md
# (DaisyUI / HTMX / Alpine / Cytoscape vendor manifest); the two move
# together because Tailwind 4 + DaisyUI 5 are co-versioned in our stack
# decision.
#
# Multi-arch: TARGETPLATFORM is set by buildx (e.g. linux/amd64,
# linux/arm64). We pick the matching glibc binary -- the python:3.12-slim
# base is Debian (glibc), not Alpine (musl), so the -musl variants are not
# used.
#
# The CLI compiles against the **installed wheel's** static/src tree -- the
# wheel install above puts templates/ + static/src/ at
# /app/.venv/lib/python3.12/site-packages/meho_backplane/ui/. We write the
# minified output to .../static/dist/tailwind.css INSIDE the installed
# package tree so the runtime stage's `COPY --from=builder /app/.venv` picks
# up the artifact automatically (no extra COPY layer required).
#
# `static/dist/` is .gitignore'd; only built artifacts live there. Local dev
# runs the same CLI in --watch mode against the source tree (see
# docs/codebase/ui.md).
ARG TAILWIND_VER=v4.3.0
ARG TAILWIND_SHA_AMD64=73f0e5459054e5cfaa8ab6f3b940f3fbe0f13cc7fd83bc24e7c655033c203400
ARG TAILWIND_SHA_ARM64=8f48dcb72be3b351c10563c5329b4638ba8516820dc3b3a1609625a166e87cbd
# python:3.12-slim ships Python but NOT curl; rather than apt-get'ing curl just
# to download one file (and re-cleaning the apt lists afterwards), we use the
# stdlib `urllib.request` already on PATH. The `sha256sum -c -` integrity
# check below is the load-bearing trust gate -- the transport layer is HTTPS
# either way -- so dropping curl removes one moving part without weakening
# the supply-chain story. `sha256sum` ships in coreutils, which the slim
# image already includes.
RUN set -eux; \
    case "${TARGETPLATFORM:-linux/amd64}" in \
      linux/amd64) TW_ARCH=linux-x64; TW_SHA="${TAILWIND_SHA_AMD64}" ;; \
      linux/arm64) TW_ARCH=linux-arm64; TW_SHA="${TAILWIND_SHA_ARM64}" ;; \
      *) echo "Unsupported TARGETPLATFORM=${TARGETPLATFORM}" >&2; exit 1 ;; \
    esac; \
    python -c "import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])" \
      "https://github.com/tailwindlabs/tailwindcss/releases/download/${TAILWIND_VER}/tailwindcss-${TW_ARCH}" \
      /tmp/tailwindcss; \
    echo "${TW_SHA}  /tmp/tailwindcss" | sha256sum -c -; \
    install -m 0755 /tmp/tailwindcss /usr/local/bin/tailwindcss; \
    rm /tmp/tailwindcss; \
    UI_SP="/app/.venv/lib/python3.12/site-packages/meho_backplane/ui"; \
    test -f "${UI_SP}/static/src/styles.css"; \
    mkdir -p "${UI_SP}/static/dist"; \
    tailwindcss \
      -i "${UI_SP}/static/src/styles.css" \
      -o "${UI_SP}/static/dist/tailwind.css" \
      --minify; \
    test -s "${UI_SP}/static/dist/tailwind.css"

# ---------- Runtime ----------------------------------------------------------
FROM python:3.14-slim@sha256:d3400aa122fa42cf0af0dbe8ec3091b047eac5c8f7e3539f7135e86d855dc015 AS runtime

# Non-root user (uid 1001 — fixed for k8s SecurityContext alignment).
RUN useradd --system --uid 1001 --gid 0 --no-create-home --shell /usr/sbin/nologin meho

# Remove the base image's own pip (site-packages pip 25.0.1) while still
# root. Nothing in this stage runs pip — dependencies are installed into
# /app/.venv by the builder via uv, and the runtime is a uvicorn CMD — so
# the base pip is dead weight whose CVEs (e.g. CVE-2026-8643) accrue on
# every Trivy scan with no code path to reach. Uninstalling is strictly
# better than upgrading: it needs no recurring re-bump.
RUN python -m pip uninstall -y pip

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

# ---------- Bake the default embedding model into the image (#574) ----------
# Pre-download the shipped default fastembed model into an IMAGE LAYER at
# /opt/meho/model-cache (== Settings.retrieval_model_cache_dir default ==
# embedding.BAKED_MODEL_CACHE_DIR). Rationale:
#
#   * Out-of-the-box startup (evoila/meho#574). The chart previously relied
#     on a runtime HuggingFace download into a *persistent RWO PVC*.
#     fastembed treats an existing snapshot dir as "model present" and never
#     re-fetches, so one interrupted/partial first download — or a CSI
#     volume that doesn't preserve the HF cache's
#     `snapshots/<sha>/*.onnx -> ../../blobs/<hash>` symlinks — poisons that
#     PVC permanently and EVERY fresh pod CrashLoops identically with
#     `onnxruntime ... NO_SUCHFILE ... model_optimized.onnx`. An image layer
#     preserves symlinks, is read-only, and removes the PVC from the
#     correctness path for the shipped default.
#   * Air-gap (the egress half of evoila/meho#572): first boot needs no
#     HuggingFace egress.
#   * Regression guard (reporter ask #1): `meho_backplane.retrieval.warm`
#     instantiates TextEmbedding(default) AND runs one real embed, so a
#     fastembed<->model-repo drift fails THIS BUILD (non-zero RUN) instead
#     of CrashLooping in production — the exact failure green ci.yml on a
#     runtime-fetched model cannot catch.
#
# Pre-seed the fastembed snapshot from the build context BEFORE the warm RUN
# (#1623). `.model-preseed/` is tracked as an empty dir (only a `.gitkeep`
# sentinel) and is populated by the chart.yml ``helm-test`` lane, which
# restores it from the unit lane's actions/cache entry (same cache key) and
# stages the `models--qdrant--bge-small-en-v1.5-onnx-q` HF snapshot into it.
# fastembed's bake (`download_model`) probes the cache_dir local-first
# (`snapshot_download(..., local_files_only=True)`); a present snapshot is a
# hit, so the warm RUN below performs ZERO HuggingFace requests on a warm
# helm-test build — killing the per-IP 429 flake on GitHub-hosted runners even
# when the BuildKit layer cache misses (per-build BUILD_DATE/GIT_SHA args +
# per-commit venv content invalidate it). Cold/local `docker build backend/`
# with no pre-seed: the dir holds only `.gitkeep`, the local probe misses, and
# the warm RUN downloads exactly once as before. The COPY is a no-op layer in
# the cold path (sentinel only), so the published image is unchanged. The warm
# RUN still instantiates the model + runs one real embed (the #574 drift guard)
# in BOTH paths — pre-seeding never short-circuits that assertion.
COPY .model-preseed/ /opt/meho/model-cache/

# No uv build-cache mount here: the download must land in the image layer,
# not in a discarded BuildKit cache. Runs while still root so it can create
# /opt/meho; chowned to the runtime uid (1001, gid 0) afterwards so the
# non-root USER below can read it (including any pre-seeded snapshot files and
# the `.gitkeep` sentinel COPYed above). `chmod -R g=u` keeps it group-readable
# for the arbitrary-uid / restricted-PSA case (gid is always 0).
RUN python -m meho_backplane.retrieval.warm \
 && chown -R 1001:0 /opt/meho \
 && chmod -R g=u /opt/meho

USER 1001:0

EXPOSE 8000

# Three execution modes share this image:
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
# 3. **Runner** — the push-only satellite runner (Initiative #2415)
#    overrides the container command to run
#    ``python -m meho_backplane.runner``. It is headless: no FastAPI
#    app, no uvicorn, no inbound listener, and no local Postgres /
#    Valkey. It dials the central instance outbound, executes
#    centrally-authorized read-only operations locally against the same
#    connector surface, and reports results back — with an on-disk retry
#    spool covering uplink outages. Like Migrate, it runs from this same
#    image via command override; no extra Dockerfile stage is needed.
#
#    The runner Deployment spec passes:
#        command: ["python"]
#        args: ["-m", "meho_backplane.runner"]
#    plus ``env: [{name: MEHO_RUNNER_CENTRAL_URL, ...}, ...]`` (the
#    ``MEHO_RUNNER_*`` namespace; no chassis env). It exits 0 on
#    SIGTERM (clean tick-loop unwind) and 1 on a configuration error
#    naming the missing/invalid variable.
#
# Exec form forwards SIGTERM cleanly to uvicorn (k8s pod-shutdown contract).
#
# ``--proxy-headers`` tells uvicorn to honour ``X-Forwarded-Proto`` and
# ``X-Forwarded-For`` from upstream proxies when populating the ASGI
# scope. Without it, FastAPI's ``redirect_slashes`` 307 reflects the
# on-the-wire scheme (``http://`` once TLS terminates at the cluster
# Ingress) into the ``Location`` header, downgrading the client's
# second hop and creating a MITM window — surfaced by the 2026-05-20
# RDC dogfood (Signal #3, Issue #730).
#
# ``--proxy-headers`` alone trusts *any* connecting peer's
# ``X-Forwarded-*`` headers, which is unsafe in a cluster where pods
# can reach each other directly. uvicorn's
# :class:`ProxyHeadersMiddleware` restricts trust to the IPs / CIDRs
# listed in ``--forwarded-allow-ips`` (equivalently the
# ``FORWARDED_ALLOW_IPS`` env var); when unset, the default is the
# loopback-only ``127.0.0.1``, which fails-closed in-cluster (the
# Ingress controller's pod IP is never the loopback). The Helm chart
# wires this knob via ``config.forwardedAllowIps`` →
# ``FORWARDED_ALLOW_IPS`` env var (see
# ``deploy/charts/meho/values.yaml`` and the operator-side contract
# at ``docs/cross-repo/reverse-proxy-contract.md``); operators pin
# the value to their Ingress controller's pod CIDR for production
# deploys.
CMD ["uvicorn", "meho_backplane.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
