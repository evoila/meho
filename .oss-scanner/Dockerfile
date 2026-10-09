# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# Build file for the Anthropic OSS Scanner (free AI security scanning for
# open-source projects). This is NOT the production image. The production
# image is backend/Dockerfile.
#
# How the scanner uses this file:
#   - It clones this repository and builds this file with the checkout as
#     the build context. `COPY . /src` puts the checkout at /src.
#   - The build has network access. The scan after it has NO network.
#     So this file downloads everything that the build and the tests need.
#   - The scanner adds its own tools (Claude Code, debuggers) on top and
#     runs as root.
#
# What this image contains:
#   - backend/ (Python, the main code): every locked dependency, including
#     the test tools, in /src/backend/.venv (already on PATH).
#   - cli/ (Go): every module, the `meho` binary with debug info at
#     /src/cli/bin/meho, and a warm build and test cache.
#   - helm, so the chart tests can render deploy/charts/meho offline.
#
# Versions follow CI and the production image: Python 3.14 (same base
# image digest as backend/Dockerfile), uv 0.11.12, Go 1.26.9 (the floor in
# cli/go.mod), helm v3.16.4 (chart.yml). Bump them together.
#
# Try it locally from a clean checkout:
#   docker build -f .oss-scanner/Dockerfile -t meho-oss-scan .
#   docker run --rm -it --network none meho-oss-scan bash

FROM python:3.14-slim@sha256:d3400aa122fa42cf0af0dbe8ec3091b047eac5c8f7e3539f7135e86d855dc015

COPY --from=ghcr.io/astral-sh/uv:0.11.12@sha256:3a59a3cdd5f7c217faa36e32dbc7fddbb0412889c2a0a5229f6d790e5a019dd7 /uv /uvx /usr/local/bin/
COPY --from=golang:1.26.9-trixie@sha256:f89535b7caea67fa9ff0ba009894bff8f3be915e49cb635477c8ce04e045db5d /usr/local/go /usr/local/go

ENV DEBIAN_FRONTEND=noninteractive \
    PATH=/src/backend/.venv/bin:/usr/local/go/bin:/root/go/bin:$PATH \
    UV_PYTHON_DOWNLOADS=0 \
    UV_LINK_MODE=copy \
    GOTOOLCHAIN=local

# gcc and libc6-dev: `go test -race` needs cgo. git: some tests read /src/.git.
# curl: downloads helm below.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gcc git libc6-dev \
 && rm -rf /var/lib/apt/lists/*

# helm, pinned by version and checksum for each CPU type.
ARG HELM_VERSION=v3.16.4
ARG HELM_SHA256_AMD64=fc307327959aa38ed8f9f7e66d45492bb022a66c3e5da6063958254b9767d179
ARG HELM_SHA256_ARM64=d3f8f15b3d9ec8c8678fbf3280c3e5902efabe5912e2f9fcf29107efbc8ead69
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "${arch}" in \
      amd64) sha="${HELM_SHA256_AMD64}" ;; \
      arm64) sha="${HELM_SHA256_ARM64}" ;; \
      *) echo "no helm checksum for ${arch}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/helm.tgz "https://get.helm.sh/helm-${HELM_VERSION}-linux-${arch}.tar.gz"; \
    echo "${sha}  /tmp/helm.tgz" | sha256sum -c -; \
    tar -xzf /tmp/helm.tgz -C /tmp "linux-${arch}/helm"; \
    install -m 0755 "/tmp/linux-${arch}/helm" /usr/local/bin/helm; \
    rm -rf /tmp/helm.tgz "/tmp/linux-${arch}"; \
    helm version

# Download the dependencies first, so a source-only change rebuilds fast.
# uv's download cache stays in a build-only cache mount (about 1 GB that
# the image does not need, because the scan never re-installs).
COPY backend/pyproject.toml backend/uv.lock backend/README.md /src/backend/
RUN --mount=type=cache,target=/root/.cache/uv \
    cd /src/backend && uv sync --locked --all-groups --no-install-project
COPY cli/go.mod cli/go.sum /src/cli/
RUN cd /src/cli && go mod download

# Scanner contract: the checkout lives inside the image, at /src.
COPY . /src

# Python: install the backend itself (editable, like CI does).
# The small spaCy English model lets the redaction tests use the real
# name-and-address detector instead of skipping (CI does the same).
ARG SPACY_MODEL_URL=https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl
ARG SPACY_MODEL_SHA256=1932429db727d4bff3deed6b34cfc05df17794f4a52eeb26cf8928f7c1a0fb85
RUN --mount=type=cache,target=/root/.cache/uv \
    set -eux; \
    cd /src/backend; \
    uv sync --locked --all-groups; \
    curl -fsSL -o /tmp/en_core_web_sm-3.8.0-py3-none-any.whl "${SPACY_MODEL_URL}"; \
    echo "${SPACY_MODEL_SHA256}  /tmp/en_core_web_sm-3.8.0-py3-none-any.whl" | sha256sum -c -; \
    uv pip install --python /src/backend/.venv/bin/python --no-deps /tmp/en_core_web_sm-3.8.0-py3-none-any.whl; \
    rm /tmp/en_core_web_sm-3.8.0-py3-none-any.whl; \
    python -c "import meho_backplane, spacy; spacy.load('en_core_web_sm')"

# Go: build every package, and the CLI binary with debug info (no -s -w).
RUN cd /src/cli \
 && go build ./... \
 && go build -o /src/cli/bin/meho ./cmd/meho

# From here on, behave like the offline scan: never reach for the network.
# The two MEHO_* settings match the CI unit lane.
ENV UV_OFFLINE=1 \
    UV_NO_SYNC=1 \
    GOPROXY=off \
    MEHO_REDACTION_SPACY_MODEL=en_core_web_sm \
    MEHO_SKIP_SPEC_INGEST_TESTS=1

# Run the cheap tests so we know the image works: the Go tests and the
# backend's security tests. A failing test does not fail the build. The
# full offline unit lane is in threat_model.md, "How to exercise it".
RUN cd /src/cli \
 && (go test -race ./... || echo "WARNING: go test reported failures")
RUN cd /src/backend \
 && (timeout 1200 python -m pytest -q -n 4 --dist loadscope --timeout=300 \
       tests/test_auth_*.py tests/test_approval_*.py tests/test_audit_*.py \
       tests/test_mcp_human_only_surface.py tests/test_mcp_surface_conformance.py \
       tests/test_targets_ssrf_guard.py tests/test_untrusted_text_envelope.py \
       tests/test_redaction_*.py tests/test_secret_leak_checks.py \
       tests/test_result_handle_*.py tests/test_destructive_*.py \
     || echo "WARNING: backend security tests reported failures")

WORKDIR /src
