# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

# syntax=docker/dockerfile:1.7

# Connector overlay image: the public MEHO image plus connector packages.
#
# Build it with build.sh next to this file. It makes the build context:
# this Dockerfile, overlay_check.py, connectors/*.whl (the connector
# packages) and deps/*.whl (the extra wheels they need).
#
# BASE_IMAGE is the public image pinned by its digest, for example
# ghcr.io/evoila/meho@sha256:<digest>. A tag alone is refused.
#
# The "install" stage checks the wheels, installs them with uv from the
# local folders (no package index), checks the result against the base
# image, and writes /app/connector-packages.json. The final stage starts
# again from the base image and copies only the new files. uv and the
# wheel files stay out of the final image, and so does pip.
#
# See docs/codebase/connector-packages.md.

ARG BASE_IMAGE

FROM ${BASE_IMAGE} AS install
ARG BASE_IMAGE
USER root
COPY --from=ghcr.io/astral-sh/uv:0.11.12@sha256:3a59a3cdd5f7c217faa36e32dbc7fddbb0412889c2a0a5229f6d790e5a019dd7 /uv /usr/local/bin/uv
COPY overlay_check.py /overlay/overlay_check.py
COPY connectors/ /overlay/connectors/
COPY deps/ /overlay/deps/
ENV PYTHONDONTWRITEBYTECODE=1 \
    UV_NO_CACHE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
# The checks run with the system Python in isolated mode without the site
# module (-I -S): no .pth file of /app/.venv can run code in them.
RUN set -eu; \
    case "${BASE_IMAGE}" in \
      *@sha256:*) ;; \
      *) echo "BASE_IMAGE must be pinned by digest (name@sha256:<digest>), got: ${BASE_IMAGE}" >&2; exit 1 ;; \
    esac; \
    check() { /usr/local/bin/python3 -I -S /overlay/overlay_check.py "$@"; }; \
    check check-wheels --connectors /overlay/connectors --deps /overlay/deps; \
    check snapshot --venv /app/.venv --out /overlay/before.json; \
    uv pip install --python /app/.venv/bin/python --offline --no-index --no-build \
      --find-links /overlay/connectors --find-links /overlay/deps \
      /overlay/connectors/*.whl; \
    check finish --venv /app/.venv --before /overlay/before.json \
      --connectors /overlay/connectors --out /overlay/out

FROM ${BASE_IMAGE}
COPY --from=install /overlay/out/ /
