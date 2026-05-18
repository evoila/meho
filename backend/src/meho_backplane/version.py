# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Build-identity surface — ``GET /version``.

Returns the immutable triple operators rely on to confirm *which* image
is running in the cluster:

* ``git_sha`` — full commit hash injected by CI at ``docker build`` time
  via ``--build-arg GIT_SHA``.
* ``build_date`` — ISO-8601 UTC timestamp injected the same way.
* ``chart_version`` — the deployed helm chart's ``.Chart.Version``,
  injected as the ``CHART_VERSION`` env var by the chart's Deployment
  template. ``null`` when unset (local ``uvicorn`` runs, bare-image
  starts) so the field never pretends an unknown release is a known
  one.

Reading the values from environment variables (rather than baking them
into a generated ``_version.py``) keeps the runtime image generic and
the build pipeline simple: the same wheel can be re-tagged with
different ``GIT_SHA`` / ``BUILD_DATE`` values just by rebuilding the
runtime layer. Missing env vars fall back to ``"unknown"`` so local
``uvicorn`` runs don't crash before the operator sets the build
arguments.
"""

import os

from fastapi import APIRouter

__all__ = ["router"]

_UNKNOWN: str = "unknown"

router = APIRouter(tags=["version"])


@router.get("/version")
async def version() -> dict[str, str | None]:
    """Return the build-identity payload.

    Each field falls back to ``"unknown"`` when its env var is unset or
    empty, which keeps local development frictionless without ever
    pretending an unknown build is a known one.
    """
    return {
        "git_sha": os.environ.get("GIT_SHA") or _UNKNOWN,
        "build_date": os.environ.get("BUILD_DATE") or _UNKNOWN,
        "chart_version": os.environ.get("CHART_VERSION") or None,
    }
