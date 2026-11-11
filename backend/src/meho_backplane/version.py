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

It also lists ``connector_packages``: the connector packages this image
carries (name, version, wheel digest and the folders each adds). The list
comes from the allowlist file the overlay image build writes, read once at
start (:func:`~meho_backplane.connector_packages.installed_connector_packages`).
It is empty for the plain public image.

It also lists ``agent_switches``: each setting that changes what an AI
agent sees, keyed by its env name, with its value (for example
``"SEARCH_CLEAR_LEAD": false``). A setting is on the list when its field in
:class:`~meho_backplane.settings.Settings` carries the
:data:`~meho_backplane.settings.AGENT_FACING` mark. A benchmark run reads
this list to record which switches were on. The list holds only names and
``true`` / ``false``. It does not show settings per tenant or per agent
definition.

Reading the values from environment variables (rather than baking them
into a generated ``_version.py``) keeps the runtime image generic and
the build pipeline simple: the same wheel can be re-tagged with
different ``GIT_SHA`` / ``BUILD_DATE`` values just by rebuilding the
runtime layer. Missing env vars fall back to ``"unknown"`` so local
``uvicorn`` runs don't crash before the operator sets the build
arguments.
"""

import hashlib
import os

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from meho_backplane.connector_packages import ConnectorPackage, installed_connector_packages
from meho_backplane.settings import AGENT_FACING, Settings, get_settings

__all__ = [
    "VersionResponse",
    "agent_switches",
    "build_git_sha",
    "deployed_version_label",
    "router",
    "running_release_id",
]

_UNKNOWN: str = "unknown"

#: Display length for a bare commit hash in the UI label. Twelve hex
#: chars matches the container-ecosystem short-id convention (docker /
#: kubelet imageID truncation) and stays collision-safe at this repo's
#: scale; the full 40-char value remains available via ``GET /version``.
_SHA_LABEL_LEN: int = 12

router = APIRouter(tags=["version"])


class VersionResponse(BaseModel):
    """``GET /version`` response body."""

    model_config = ConfigDict(frozen=True)

    git_sha: str
    build_date: str
    chart_version: str | None
    connector_packages: list[ConnectorPackage] = []
    agent_switches: dict[str, bool] = {}


def agent_switches(settings: Settings) -> dict[str, bool]:
    """Return each agent-facing setting of *settings*, keyed by env name, sorted.

    A setting is agent-facing when its field carries the
    :data:`~meho_backplane.settings.AGENT_FACING` mark. The env name is the
    field name in upper case, the name ``get_settings`` reads it from.
    """
    marked = sorted(
        name
        for name, field in Settings.model_fields.items()
        if isinstance(field.json_schema_extra, dict) and field.json_schema_extra == AGENT_FACING
    )
    return {name.upper(): getattr(settings, name) for name in marked}


def build_git_sha() -> str:
    """Return the commit hash of the running build, or ``"unknown"``.

    The release identity ``GET /version`` reports as ``git_sha``. The
    operation catalog stamp (:mod:`meho_backplane.operations.catalog_release`)
    uses the same value, so both name a release the same way.
    """
    return os.environ.get("GIT_SHA") or _UNKNOWN


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


def _connector_packages_digest(packages: tuple[ConnectorPackage, ...]) -> str:
    """Return a short digest of the name, version and wheel digest of *packages*.

    The order of the allowlist does not matter: the same packages give the
    same digest. Twelve hex chars, like the short commit hash in the UI.
    """
    lines = sorted(f"{p.name} {p.version} {p.wheel_digest}" for p in packages)
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()[:_SHA_LABEL_LEN]


def running_release_id() -> str | None:
    """Return the identity of the running release, or ``None`` when unknown.

    An approval request is bound to this value (#5030): a person approves
    one exact release of MEHO. The value is the full ``GIT_SHA``, plus the
    ``CHART_VERSION`` when it is set, plus a digest of the installed
    connector packages when the image carries any. These are the same
    values ``GET /version`` shows, so a new chart or a new package in an
    image with the same commit is another release. ``None`` when
    ``GIT_SHA`` is unset or ``unknown``: only a local development run has
    no release identity, and then MEHO does not bind or check a release.
    """
    git_sha = os.environ.get("GIT_SHA")
    if not git_sha or git_sha == _UNKNOWN:
        return None
    chart_version = os.environ.get("CHART_VERSION")
    release = f"{git_sha} (chart {chart_version})" if chart_version else git_sha
    packages = installed_connector_packages()
    if packages:
        release = f"{release} (packages {_connector_packages_digest(packages)})"
    return release


@router.get("/version", response_model=VersionResponse)
async def version() -> VersionResponse:
    """Return the build identity, the connector packages and the agent switches.

    Each build field falls back to ``"unknown"`` when its env var is unset
    or empty, which keeps local development frictionless without ever
    pretending an unknown build is a known one. ``connector_packages`` is
    empty for the plain public image. ``agent_switches`` lists each
    agent-facing setting with its value.
    """
    return VersionResponse(
        git_sha=build_git_sha(),
        build_date=os.environ.get("BUILD_DATE") or _UNKNOWN,
        chart_version=os.environ.get("CHART_VERSION") or None,
        connector_packages=list(installed_connector_packages()),
        agent_switches=agent_switches(get_settings()),
    )
