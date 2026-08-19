# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""vcf-automation real-spec reconcile lane (#2983) — the #2980 harness.

Asserts the hand-coded ``METHOD:/path`` literals the vcf-automation
connector dispatches against the pinned ``vcf-automation-9.0`` shelf.
Parse-only set compare — no DB, no embeddings, no containers — so it
lives in the required unit sweep per the lane-placement rule in
``docs/decisions/spec-reconcile-guards-standard.md``. Uniform skip when
the shelf is unconfigured (``tests/_spec_shelf.py`` contract).

VCFA is dual-plane and its shelf is a **mosaic** (the shelf's
``vcf-automation-9.0/MANIFEST.md`` is the provenance record), so the
lane splits by plane:

* **Tenant plane — armed.** Every ``/iaas/api/*`` op_id asserts against
  the pinned ``swagger-vra-sdk-go-v0.6.5/vra-iaas.json`` — the IaaS API
  Swagger 2.0 document vendored in the **Apache-2.0**
  `vmware/vra-sdk-go <https://github.com/vmware/vra-sdk-go>`_ repo at
  tag ``v0.6.5``, the closest published surface to the VCFA 9.x
  consumption plane (the on-prem appliance serves the same path
  shapes). ``parse_openapi`` accepts OpenAPI 3.0/3.1 only (#2090), so
  the lane supplies its own served-set extraction from the Swagger 2.0
  document — the standard's non-OpenAPI-artifact clause. No OpenAPI 3
  conversion is pinned (unlike nsx-9.0's): these fragments are never
  operator-ingested — the typed ops exist precisely because ingest
  rejects them — so a converted artifact would be a test-only
  fabrication with no dispatch-fidelity value.
* **Provider plane — evidenced exclusion.** No wire-level provider
  (cloudapi / classic ``/api/*``) spec exists or can be pinned: the
  vendor publishes no artifact (no ``automation/`` directory in
  ``vmware/vcf-api-specs``; go-vcloud-director and
  terraform-provider-vcfa vendor no swagger — the SDK's hand-written
  endpoint map is the machine-readable surface) and the appliance
  serves none (``/cloudapi/1.0.0/openapi*`` → 404, probed 2026-05-16).
  Evidence, coverage, and the activation trigger live in the
  ``vcf-automation-9.0`` entry of the standard doc's
  Evidenced-exclusions section. The manifest pin below still sweeps
  the provider op_ids so the enumeration stays complete and a future
  spec activates against the full set.

The declared set is introspected from the connector's live constants
(the #2944 pattern — never a hardcoded mirror), across the two modules
that hand-code a path:

* :mod:`~meho_backplane.connectors.vcf_automation.typed_ops` — the
  seven ``*_PATH`` op constants. Every typed handler dispatches through
  the connector's ``_request_json(target, "GET", ...)``, so each
  declares ``GET:``.
* :mod:`~meho_backplane.connectors.vcf_automation._routing` — the two
  ``*_SESSION_PATH`` login endpoints (both POSTed by ``._auth``) and
  the two ``*_VERSION_PATH`` unauthenticated fingerprint probes (both
  GET). ``TENANT_VERSION_PATH`` collapses onto the typed
  ``GET:/iaas/api/about`` op_id by value.

First run against the real shelf surfaced one finding — caught by the
shelf's provenance record rather than a spec parse, since the affected
path is provider-plane: ``vcfa.provider.region.list`` targeted
``GET:/cloudapi/1.0.0/regions``, which the live product 404s (Region
moved to the ``vcf/`` cloudapi prefix on VCFA 9.0); repointed to
``GET:/cloudapi/vcf/regions`` in the same PR per the #2970 protocol.
Rationale on the constant; protocol:
docs/decisions/spec-reconcile-guards-standard.md.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from meho_backplane.connectors.vcf_automation import _routing as _routing_module
from meho_backplane.connectors.vcf_automation import typed_ops as _typed_ops_module
from meho_backplane.connectors.vcf_automation._routing import plane_for_path
from meho_backplane.connectors.vcf_automation.typed_ops import VCFA_TYPED_OPS
from tests._spec_shelf import assert_op_ids_served, require_shelf_spec

if TYPE_CHECKING:
    from pathlib import Path

_SPEC_DIR = "vcf-automation-9.0"
_TENANT_SPEC_FILE = "swagger-vra-sdk-go-v0.6.5/vra-iaas.json"
_TENANT_SPEC_LABEL = f"{_SPEC_DIR}/{_TENANT_SPEC_FILE}"

#: The Swagger 2.0 path-item keys that are HTTP operations (its fixed
#: verb vocabulary); everything else on a path item (``parameters``,
#: vendor extensions) is not a served operation.
_SWAGGER2_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch"})

#: HTTP method per ``_routing`` constant-name suffix. The two
#: ``*_SESSION_PATH`` login endpoints are POSTed (``._auth``'s
#: ``vcfa_provider_login`` / ``tenant_login``); the two
#: ``*_VERSION_PATH`` probes are GET (``connector.fingerprint`` via
#: ``_try_probe``). A new ``*_PATH`` constant matching neither suffix
#: fails the sweep loudly so its method is mapped consciously.
_ROUTING_METHOD_BY_SUFFIX = {
    "_SESSION_PATH": "POST",
    "_VERSION_PATH": "GET",
}


def _path_constants(module: object) -> dict[str, str]:
    """The live ``*_PATH`` string constants of *module*, by constant name."""
    return {
        name: value
        for name, value in vars(module).items()
        if name.endswith("_PATH") and isinstance(value, str) and value.startswith("/")
    }


def _declared_op_ids() -> set[str]:
    """Every hand-coded ``METHOD:/path`` the vcf-automation connector dispatches.

    Sweeps the live module constants (never a hardcoded mirror): all
    typed-op paths dispatch as ``GET`` (each handler is a
    ``_request_json(target, "GET", ...)`` call); the ``_routing``
    constants map to their method by name suffix
    (:data:`_ROUTING_METHOD_BY_SUFFIX`).
    """
    declared = {f"GET:{path}" for path in _path_constants(_typed_ops_module).values()}
    for name, path in _path_constants(_routing_module).items():
        for suffix, method in _ROUTING_METHOD_BY_SUFFIX.items():
            if name.endswith(suffix):
                declared.add(f"{method}:{path}")
                break
        else:
            raise AssertionError(
                f"_routing.{name} matches no suffix in _ROUTING_METHOD_BY_SUFFIX — "
                "map the new constant's HTTP method there consciously so the "
                "reconcile keeps covering it."
            )
    return declared


def test_path_constant_names_are_pinned() -> None:
    """Guard: the introspection finds every hand-coded path constant.

    Pinning the exact constant-name sets (the #2944 guard shape) means a
    renamed or dropped ``*_PATH`` constant — or a new one that skips the
    ``_PATH`` suffix convention — can't silently shrink the reconciled
    set to a vacuous pass.
    """
    assert sorted(_path_constants(_typed_ops_module)) == [
        "PROVIDER_ORGS_PATH",
        "PROVIDER_REGIONS_PATH",
        "PROVIDER_SITE_PATH",
        "TENANT_ABOUT_PATH",
        "TENANT_DEPLOYMENTS_PATH",
        "TENANT_DEPLOYMENT_DETAIL_PATH",
        "TENANT_PROJECTS_PATH",
    ]
    assert sorted(_path_constants(_routing_module)) == [
        "PROVIDER_SESSION_PATH",
        "PROVIDER_VERSION_PATH",
        "TENANT_SESSION_PATH",
        "TENANT_VERSION_PATH",
    ]


def test_typed_op_paths_are_covered_by_the_swept_constants() -> None:
    """Guard: no typed op can carry an inline path the sweep misses.

    Every :data:`VCFA_TYPED_OPS` entry's ``path`` must be one of the
    swept ``typed_ops`` module constants — an op added with an inline
    literal (skipping the shared-constant convention the module
    documents) would otherwise dodge the reconcile.
    """
    swept = set(_path_constants(_typed_ops_module).values())
    unswept = {op.op_id: op.path for op in VCFA_TYPED_OPS if op.path not in swept}
    assert not unswept, (
        f"typed ops with paths outside the swept *_PATH constants: {unswept} — "
        "route the path through a module-level *_PATH constant so the "
        "reconcile lane covers it."
    )


def test_vcfa_hand_coded_op_id_manifest_is_pinned() -> None:
    """Pin the swept op_id set so drift can't silently shrink the reconcile.

    Runs unconditionally (no shelf needed), so the enumeration is proven
    on every sweep — both planes, including the provider op_ids whose
    spec-assert is an evidenced exclusion today. A new hand-coded path,
    a renamed constant, or a method change must update this manifest
    consciously — and the lane's declared set moves with it.
    """
    assert _declared_op_ids() == {
        "GET:/api/versions",
        "GET:/cloudapi/1.0.0/orgs",
        "GET:/cloudapi/1.0.0/site",
        "GET:/cloudapi/vcf/regions",
        "GET:/iaas/api/about",
        "GET:/iaas/api/deployments",
        "GET:/iaas/api/deployments/{id}",
        "GET:/iaas/api/projects",
        "POST:/cloudapi/1.0.0/sessions/provider",
        "POST:/iaas/api/login",
    }


def _swagger2_served_op_ids(spec_path: Path) -> set[str]:
    """Extract the served ``METHOD:/path`` set from a Swagger 2.0 document.

    The pinned tenant spec is Swagger 2.0, which the ingest parser
    rejects by decision (#2090) — so this lane extracts directly instead
    of routing through :func:`tests._spec_shelf.openapi_served_op_ids`
    (the standard's non-OpenAPI-artifact clause). Served op_ids are the
    ``basePath``-folded path keys crossed with their operation-verb
    keys; ``vra-iaas.json`` pins ``basePath: /``, so its paths fold
    unchanged. Format drift on the shelf (a re-pin that is no longer
    Swagger 2.0, or an empty path map) fails loudly rather than
    regressing the lane to a vacuous compare.
    """
    document = json.loads(spec_path.read_text(encoding="utf-8"))
    if document.get("swagger") != "2.0":
        pytest.fail(
            f"{_TENANT_SPEC_LABEL}: expected a Swagger 2.0 document "
            f"(got swagger={document.get('swagger')!r} / "
            f"openapi={document.get('openapi')!r}) — the shelf artifact "
            "format moved; update this lane's extraction."
        )
    base = str(document.get("basePath") or "").rstrip("/")
    served = {
        f"{method.upper()}:{base}{path}"
        for path, item in document.get("paths", {}).items()
        for method in item
        if method.lower() in _SWAGGER2_METHODS
    }
    if not served:
        pytest.fail(
            f"{_TENANT_SPEC_LABEL}: extracted zero served operations — the "
            "shelf artifact layout moved; update this lane's extraction."
        )
    return served


def test_tenant_hand_coded_paths_are_served_by_the_pinned_iaas_spec() -> None:
    """Every tenant-plane op_id is served by the pinned vra-iaas.json spec.

    The tenant subset is selected with the connector's own
    :func:`~meho_backplane.connectors.vcf_automation._routing.plane_for_path`
    — the same classifier the transport uses to pick the auth plane —
    so the lane's plane split can never drift from dispatch reality.
    A red run here is the guard surfacing a finding; triage per
    docs/decisions/spec-reconcile-guards-standard.md.
    """
    spec_path = require_shelf_spec(_SPEC_DIR, _TENANT_SPEC_FILE)
    served = _swagger2_served_op_ids(spec_path)
    declared = {
        op_id for op_id in _declared_op_ids() if plane_for_path(op_id.partition(":")[2]) == "tenant"
    }
    assert_op_ids_served(declared, served, spec_label=_TENANT_SPEC_LABEL)
