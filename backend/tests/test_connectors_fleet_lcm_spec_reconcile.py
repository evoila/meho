# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""fleet-lcm (VCF 9 Fleet LCM Service) real-spec reconcile lane (#3036) — the #2980 harness.

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

Ingested ops are not reconciled here. The connector's 51 ``/v1/*`` operations
arrive via G0.7 spec ingestion (``source_kind="ingested"``), so an operator
ingest of the same pinned spec produces those op_ids **by construction** — a
reconcile of ingested rows against the spec they were ingested from is
tautological. What this lane guards is the connector's **hand-coded** surface:
its ``_*_PATH`` probe/fingerprint constant(s), introspected from the live class
constants (the #2944 pattern — never a hardcoded mirror), which a typo or a
renamed endpoint would silently break. The connector hand-codes exactly one
path — the ``GET /v1/health`` reachability probe — and dispatches it GET.

No server-base fold. Unlike the vcf-operations lane (whose spec declares the
*relative* server base ``/suite-api`` that ``parse_openapi`` folds onto every
path key, #1796) and the vcf-logs lane (relative ``/api/v2``), this spec's
server url ``https://vcf.broadcom.com/fleet-lcm`` is **absolute**.
``parse_openapi``'s ``_server_base_path`` returns ``""`` for an absolute
server (scheme/authority present) — the ``/fleet-lcm`` base points at a
spec-declared host meho does not dispatch against, so only the operator's
target host carries it. The served op_ids are therefore the raw ``/v1/*`` path
keys (verified: the pinned spec serves ``GET:/v1/health``, no ``/fleet-lcm``
prefix), and the connector's ``_FLEET_LCM_HEALTH_PATH`` constant declares the
same raw ``/v1/health`` — the comparison is byte-for-byte against what an
operator ingest of the same spec would dispatch.

A red lane here is the guard surfacing a real finding, not harness noise —
triage per docs/decisions/spec-reconcile-guards-standard.md.
"""

from __future__ import annotations

from meho_backplane.connectors.fleet_lcm import connector as _connector
from tests._spec_shelf import (
    assert_op_ids_served,
    openapi_served_op_ids,
    require_shelf_spec,
)

_SPEC_DIR = "fleet-lcm-9.0"
_SPEC_FILE = "fleet-lcm-openapi.yaml"
_SPEC_LABEL = f"{_SPEC_DIR}/{_SPEC_FILE}"


def _path_constants() -> dict[str, str]:
    """The live ``_*_PATH`` string constants of ``connector``, by constant name."""
    return {
        name: value
        for name, value in vars(_connector).items()
        if name.endswith("_PATH") and isinstance(value, str) and value.startswith("/")
    }


def _declared_op_ids() -> set[str]:
    """Every hand-coded ``METHOD:/path`` the fleet-lcm connector dispatches.

    Sweeps the live module constants (never a hardcoded mirror). The connector
    hand-codes exactly one path — the ``GET /v1/health`` reachability probe —
    and dispatches it GET (the 51 ``/v1/*`` operational ops are ingested, not
    hand-coded, and are reconciled by ingestion itself, not this lane).
    """
    return {f"GET:{path}" for path in _path_constants().values()}


def test_hand_coded_path_surface_is_pinned() -> None:
    """Guard: the introspection finds the hand-coded probe literal.

    Pinning the exact ``_*_PATH`` constant-name set, its value, and the
    assembled op_id set (the #2944 guard shape) means a renamed or dropped
    constant — or a new path that skips the convention — fails here until the
    reconcile is updated consciously, so the declared set can never silently
    shrink to a vacuous pass. Runs unconditionally (no shelf needed).
    """
    assert sorted(_path_constants()) == ["_FLEET_LCM_HEALTH_PATH"]
    assert _connector._FLEET_LCM_HEALTH_PATH == "/v1/health"
    assert _declared_op_ids() == {"GET:/v1/health"}


def test_declared_op_ids_are_served_by_the_pinned_spec() -> None:
    """Every hand-coded op_id is served by the pinned fleet-lcm-9.0 spec.

    Arms when the shelf provides ``fleet-lcm-9.0/fleet-lcm-openapi.yaml``
    (``require_shelf_spec`` skips uniformly otherwise). Uses the standard
    :func:`~tests._spec_shelf.openapi_served_op_ids` (the spec is a clean
    OpenAPI 3.0.4 document whose ``securitySchemes`` are well-formed
    ``type: http`` schemes, so ``parse_openapi`` accepts it) with **no**
    server-base fold — see the module docstring for the absolute-server
    reasoning.
    """
    spec_path = require_shelf_spec(_SPEC_DIR, _SPEC_FILE)
    served = openapi_served_op_ids(spec_path)
    assert_op_ids_served(_declared_op_ids(), served, spec_label=_SPEC_LABEL)
