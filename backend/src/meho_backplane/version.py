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

__all__ = ["deployed_version_label", "router"]

_UNKNOWN: str = "unknown"

#: Display length for a bare commit hash in the UI label. Twelve hex
#: chars matches the container-ecosystem short-id convention (docker /
#: kubelet imageID truncation) and stays collision-safe at this repo's
#: scale; the full 40-char value remains available via ``GET /version``.
_SHA_LABEL_LEN: int = 12

router = APIRouter(tags=["version"])


def deployed_version_label() -> str:
    """Return the operator-facing label for the running build.

    Single source of truth with :func:`version` below: both read the
    same ``CHART_VERSION`` / ``GIT_SHA`` environment variables, so the
    UI footer and ``GET /version`` can never disagree about which
    image is live (#1698 — the footer used to render the static
    package ``__version__``, which is pinned to ``0.1.0-dev`` by
    design and never tracks the deployed release).

    Precedence mirrors how meaningful each value is to an operator:

    * ``CHART_VERSION`` — the helm release identity (``0.14.0`` on a
      tag deploy, ``0.1.<date>-<sha>`` calver on main deploys),
      rendered with a ``v`` prefix when not already carrying one.
    * ``GIT_SHA`` — bare-image runs (``docker run`` without the
      chart): the first 12 hash chars. The Dockerfile defaults the
      build arg to the literal ``unknown``, which intentionally falls
      through to the fallback below.
    * ``"unknown"`` — local ``uvicorn`` runs with no build metadata;
      the same never-pretend posture ``GET /version`` takes.
    """
    chart_version = os.environ.get("CHART_VERSION")
    if chart_version:
        return chart_version if chart_version.startswith("v") else f"v{chart_version}"
    git_sha = os.environ.get("GIT_SHA")
    if git_sha and git_sha != _UNKNOWN:
        return git_sha[:_SHA_LABEL_LEN]
    return _UNKNOWN


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
