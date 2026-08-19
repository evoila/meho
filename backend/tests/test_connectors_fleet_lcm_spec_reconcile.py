# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""fleet-lcm (VCF 9 Fleet LCM Service) real-spec reconcile lane (#3036, #3047) — #2980 harness.

Asserts every hand-coded ``METHOD:/path`` the modern ``fleet-lcm`` connector
dispatches is served by the pinned ``fleet-lcm-9.0/fleet-lcm-openapi.yaml`` — the
VCF 9 Fleet LCM Service OpenAPI document vendored in the **public, Apache-2.0**
`vmware/vcf-api-specs <https://github.com/vmware/vcf-api-specs>`_ repo (pinned at
``c3f3b52c``; provenance + sha256 in the shelf's ``fleet-lcm-9.0/MANIFEST.md``).
Parse-only set compare — no DB, no embeddings, no containers — so it lives in the
required unit sweep per the lane-placement rule in
``docs/decisions/spec-reconcile-guards-standard.md``. Uniform skip when the shelf
is unconfigured (``tests/_spec_shelf.py`` contract).

Distinct surface from the legacy vcf-fleet lane. The paired dormant lane
``test_connectors_vcf_fleet_spec_reconcile.py`` reconciles the legacy
``fleet-rest`` impl's ``/lcm/lcops/api/v2/*`` (vRSLCM) literals against a
*different* spec (``vcf-fleet-9.0/vrslcm-lcm-openapi.json``). This lane
reconciles the **modern** ``fleet-lcm`` impl's ``/v1/*`` surface. The two impls
of ``product=fleet`` are resolved per target by fingerprint (see
``test_connectors_fleet_dual_impl_resolution.py``).

What this lane guards (#3047). The connector's hand-coded surface is now the
**13-op typed read core** (``source_kind="typed"``): its ``/v1/*`` request-path
templates live as ``_*_PATH`` constants in
:mod:`~meho_backplane.connectors.fleet_lcm._paths`, and every typed handler
builds its concrete path from one via ``fill_path`` — so a typo, a renamed
endpoint, or a placeholder-name drift would dispatch a URL the real service
never serves. This lane introspects those live ``_paths`` constants (the #2944
pattern — never a hardcoded mirror) plus the connector's
``_FLEET_LCM_HEALTH_PATH`` reachability probe, and asserts each assembled
``GET:/path`` is served by the pinned spec. The typed health op and the probe
share ``/v1/health`` — the pin asserts that link so the two never drift apart.
(The wider 51-op ``/v1/*`` surface arrives as ``source_kind="ingested"`` breadth
via an operator ingest of *this same spec* — reconciling ingested rows against
the spec they were ingested from is tautological, so only the hand-coded typed
paths are guarded here.)

No server-base fold. Unlike the vcf-operations lane (whose spec declares the
*relative* server base ``/suite-api`` that ``parse_openapi`` folds onto every
path key, #1796) and the vcf-logs lane (relative ``/api/v2``), this spec's
server url ``https://vcf.broadcom.com/fleet-lcm`` is **absolute**.
``parse_openapi``'s ``_server_base_path`` returns ``""`` for an absolute
server (scheme/authority present) — the ``/fleet-lcm`` base points at a
spec-declared host meho does not dispatch against, so only the operator's
target host carries it. The served op_ids are therefore the raw ``/v1/*`` path
keys (verified: the pinned spec serves ``GET:/v1/health``, no ``/fleet-lcm``
prefix), and the ``_paths`` constants declare the same raw ``/v1/*`` — the
comparison is byte-for-byte against what an operator ingest of the same spec
would dispatch.

A red lane here is the guard surfacing a real finding, not harness noise —
triage per docs/decisions/spec-reconcile-guards-standard.md.
"""

from __future__ import annotations

from meho_backplane.connectors.fleet_lcm import _paths
from meho_backplane.connectors.fleet_lcm import connector as _connector
from meho_backplane.connectors.fleet_lcm.typed_ops import FLEET_LCM_TYPED_OPS
from tests._spec_shelf import (
    assert_op_ids_served,
    openapi_served_op_ids,
    require_shelf_spec,
)

_SPEC_DIR = "fleet-lcm-9.0"
_SPEC_FILE = "fleet-lcm-openapi.yaml"
_SPEC_LABEL = f"{_SPEC_DIR}/{_SPEC_FILE}"

#: The exact ``_*_PATH`` template-constant name set the typed read core declares
#: in :mod:`~meho_backplane.connectors.fleet_lcm._paths`. Pinned so a dropped or
#: renamed template — or a new one that skips the convention — fails the guard
#: consciously rather than silently shrinking the reconciled surface.
_EXPECTED_PATH_CONSTANT_NAMES = frozenset(
    {
        "_HEALTH_PATH",
        "_SYSTEM_PATH",
        "_CONFIG_PATH",
        "_SDDC_LCMS_PATH",
        "_SDDC_LCM_PATH",
        "_COMPONENTS_PATH",
        "_COMPONENT_PATH",
        "_COMPONENT_STATUS_PATH",
        "_TASKS_PATH",
        "_TASK_PATH",
        "_UPGRADE_PLANS_PATH",
        "_UPGRADE_PLAN_PATH",
        "_RELEASE_VERSIONS_PATH",
    }
)

#: The full hand-coded ``GET:/path`` surface the typed read core dispatches, plus
#: the connector's health probe (which reuses the ``/v1/health`` typed path).
_EXPECTED_OP_IDS = frozenset(
    {
        "GET:/v1/health",
        "GET:/v1/system",
        "GET:/v1/config",
        "GET:/v1/sddc-lcms",
        "GET:/v1/sddc-lcms/{sddcLcmId}",
        "GET:/v1/components",
        "GET:/v1/components/{componentId}",
        "GET:/v1/components/{componentId}/status",
        "GET:/v1/tasks",
        "GET:/v1/tasks/{taskId}",
        "GET:/v1/upgrade-plans",
        "GET:/v1/upgrade-plans/{planId}",
        "GET:/v1/release-versions",
    }
)


def _typed_path_constants() -> dict[str, str]:
    """The live ``_*_PATH`` template constants of the ``_paths`` module."""
    return {
        name: value
        for name, value in vars(_paths).items()
        if name.endswith("_PATH") and isinstance(value, str) and value.startswith("/")
    }


def _declared_op_ids() -> set[str]:
    """Every hand-coded ``GET:/path`` the fleet-lcm connector dispatches.

    The 13 typed read-core path templates (:mod:`._paths`) union the connector's
    ``GET /v1/health`` reachability probe (which reuses the typed health path).
    All are dispatched GET. Swept from live module constants — never a hardcoded
    mirror.
    """
    paths = set(_typed_path_constants().values())
    paths.add(_connector._FLEET_LCM_HEALTH_PATH)
    return {f"GET:{path}" for path in paths}


def test_hand_coded_path_surface_is_pinned() -> None:
    """Guard: the introspection finds exactly the typed read-core path surface.

    Pins the ``_*_PATH`` template-constant name set, the assembled op_id set (the
    #2944 guard shape), the probe⇄typed-health link, and the one-path-per-typed-op
    invariant — so the declared set can never silently shrink to a vacuous pass or
    drift from the read core the connector actually ships. Runs unconditionally
    (no shelf needed).
    """
    assert set(_typed_path_constants()) == set(_EXPECTED_PATH_CONSTANT_NAMES)
    # The typed health op and the connector's reachability probe are the same
    # endpoint — pin the link so a change to one is forced through the other.
    assert _paths._HEALTH_PATH == _connector._FLEET_LCM_HEALTH_PATH == "/v1/health"
    assert _declared_op_ids() == set(_EXPECTED_OP_IDS)
    # One dispatched path per typed op (health's path is shared with the probe,
    # so the distinct-path count equals the typed-op count).
    assert len(FLEET_LCM_TYPED_OPS) == len(_declared_op_ids()) == 13


def test_declared_op_ids_are_served_by_the_pinned_spec() -> None:
    """Every hand-coded op_id is served by the pinned fleet-lcm-9.0 spec.

    Arms when the shelf provides ``fleet-lcm-9.0/fleet-lcm-openapi.yaml``
    (``require_shelf_spec`` skips uniformly otherwise). Uses the standard
    :func:`~tests._spec_shelf.openapi_served_op_ids` (the spec is a clean
    OpenAPI 3.0.4 document whose ``securitySchemes`` are well-formed
    ``type: http`` schemes, so ``parse_openapi`` accepts it) with **no**
    server-base fold — see the module docstring for the absolute-server
    reasoning. The by-id placeholders (``{sddcLcmId}`` / ``{componentId}`` /
    ``{taskId}`` / ``{planId}``) are byte-for-byte the spec's own path-parameter
    names, so the templated op_ids compare directly against the served keys.
    """
    spec_path = require_shelf_spec(_SPEC_DIR, _SPEC_FILE)
    served = openapi_served_op_ids(spec_path)
    assert_op_ids_served(_declared_op_ids(), served, spec_label=_SPEC_LABEL)
