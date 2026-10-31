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
  the pinned ``swagger-vra-sdk-go-v0.6.5/vra-iaas.json``, and every
  ``/blueprint/api/*`` / ``/deployment/api/*`` op_id against its sibling
  ``vra-blueprint.json`` / ``vra-catalog-deployment.json`` — the IaaS API
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
* **Provider region path — armed per version (#4604).** The shelf pins
  one reference list per VCFA version, made from Broadcom's public
  reference pages (``<dir>/<version>-provider-infrastructure-apis-
  reference-operations.tsv`` in ``vcf-automation-9.0-reference/``,
  ``vcf-automation-9.1/`` and ``vcf-automation-9.1.1/``). The path
  :func:`~meho_backplane.connectors.vcf_automation.typed_ops.provider_regions_path`
  picks for each version must be a ``GET`` line in that version's list.
* **Region and quota paths — armed per version.** Every
  :mod:`~meho_backplane.connectors.vcf_automation._infra_paths` call, in the
  path form :func:`~meho_backplane.connectors.vcf_automation._infra_paths.provider_vcf_path`
  gives each version (``/cloudapi/vcf/`` on 9.0, ``/cloudapi/v1/`` on 9.1 and
  9.1.1), must be a line of that version's reference list. Path parameter
  names are compared without their braces, because the lists write some of
  them bare (``vdcUrn``). Two legacy ``/api`` calls are an evidenced
  exclusion: no 9.x reference lists a legacy call, and the vendor's own SDK
  (``go-vcloud-director`` v3) uses them on VCF Automation 9 -- the vCenter
  refresh (``govcd/vsphere_vcenter.go``: "there is no OpenAPI endpoint for
  this operation") and the task read behind every ``202`` task link.

The declared set is introspected from the connector's live constants
(the #2944 pattern — never a hardcoded mirror), across the two modules
that hand-code a path:

* :mod:`~meho_backplane.connectors.vcf_automation.typed_ops` — the
  six ``*_PATH`` read-op constants. Every typed read dispatches through
  the connector's ``_request_json(target, "GET", ...)``, so each
  declares ``GET:``; the org and project constants are also POSTed
  by the #3890 creates (:data:`_TYPED_OPS_EXTRA_METHODS`).
* :mod:`~meho_backplane.connectors.vcf_automation._paths` — the #3890
  provisioning paths and the image-op paths, each mapped to its method(s) in
  :data:`_PATHS_METHODS`. The ``/oauth/*`` token endpoints are
  provider-plane by :func:`plane_for_path`, so they sit in the
  evidenced exclusion with the rest of the provider plane.
* :mod:`~meho_backplane.connectors.vcf_automation._routing` — the three
  ``*_SESSION_PATH`` login endpoints (all POSTed by ``._auth``; the CSP
  token mint is the #3865 VCFA 9.1 fallback) and
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
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from meho_backplane.connectors.vcf_automation import _infra_paths as _infra_paths_module
from meho_backplane.connectors.vcf_automation import _paths as _paths_module
from meho_backplane.connectors.vcf_automation import _routing as _routing_module
from meho_backplane.connectors.vcf_automation import typed_ops as _typed_ops_module
from meho_backplane.connectors.vcf_automation._infra_paths import (
    LEGACY_API_PATHS,
    provider_vcf_path,
)
from meho_backplane.connectors.vcf_automation._routing import plane_for_path
from meho_backplane.connectors.vcf_automation.deploy_ops import VCFA_DEPLOY_OPS
from meho_backplane.connectors.vcf_automation.feature_flag_ops import VCFA_FEATURE_FLAG_OPS
from meho_backplane.connectors.vcf_automation.image_ops import VCFA_IMAGE_OPS
from meho_backplane.connectors.vcf_automation.infra_ops import VCFA_INFRA_OPS
from meho_backplane.connectors.vcf_automation.network_ops import VCFA_NETWORK_OPS
from meho_backplane.connectors.vcf_automation.provisioning_ops import VCFA_PROVISIONING_OPS
from meho_backplane.connectors.vcf_automation.typed_ops import (
    VCFA_TYPED_OPS,
    provider_regions_path,
)
from tests._spec_shelf import assert_op_ids_served, require_shelf_spec

if TYPE_CHECKING:
    from pathlib import Path

_SPEC_DIR = "vcf-automation-9.0"
#: The tenant plane spans three APIs; each has its own Swagger 2.0 file on the
#: shelf. ``plane_for_path`` sends all three to the tenant login.
_TENANT_SPEC_FILES = (
    "swagger-vra-sdk-go-v0.6.5/vra-iaas.json",
    "swagger-vra-sdk-go-v0.6.5/vra-blueprint.json",
    "swagger-vra-sdk-go-v0.6.5/vra-catalog-deployment.json",
)
_TENANT_SPEC_LABEL = f"{_SPEC_DIR} tenant specs (vra-iaas, vra-blueprint, vra-catalog-deployment)"

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


#: HTTP method(s) per ``_paths`` constant -- the provisioning surface
#: (#3890). Several paths serve both a lookup ``GET`` and a write. A new
#: ``_paths`` constant missing here fails the sweep loudly so its method is
#: mapped consciously (same contract as :data:`_ROUTING_METHOD_BY_SUFFIX`).
_PATHS_METHODS: dict[str, frozenset[str]] = {
    "OAUTH_REGISTER_PATH": frozenset({"POST"}),
    "OAUTH_TOKEN_PATH": frozenset({"POST"}),
    "ORG_SESSION_CURRENT_PATH": frozenset({"DELETE"}),
    "ORG_SESSION_PATH": frozenset({"POST"}),
    "PROVIDER_FEATURE_FLAGS_PATH": frozenset({"GET"}),
    "PROVIDER_FEATURE_FLAG_PATH": frozenset({"GET", "PUT"}),
    "PROVIDER_GLOBAL_ROLES_PATH": frozenset({"GET", "POST"}),
    "PROVIDER_GLOBAL_ROLE_PATH": frozenset({"DELETE"}),
    "PROVIDER_GLOBAL_ROLE_PUBLISH_ALL_PATH": frozenset({"POST"}),
    "PROVIDER_GLOBAL_ROLE_PUBLISH_PATH": frozenset({"POST"}),
    "PROVIDER_GLOBAL_ROLE_RIGHTS_PATH": frozenset({"GET", "PUT"}),
    "PROVIDER_GLOBAL_ROLE_TENANTS_PATH": frozenset({"GET"}),
    "PROVIDER_RIGHTS_PATH": frozenset({"GET"}),
    "PROVIDER_ROLES_PATH": frozenset({"GET"}),
    "PROVIDER_TOKENS_PATH": frozenset({"GET"}),
    "PROVIDER_TOKEN_PATH": frozenset({"DELETE"}),
    "PROVIDER_USERS_PATH": frozenset({"GET", "POST"}),
    # Image ops.
    "TENANT_FABRIC_IMAGES_PATH": frozenset({"GET"}),
    "TENANT_FABRIC_IMAGE_PATH": frozenset({"GET"}),
    "TENANT_IMAGE_ENUMERATION_PATH": frozenset({"POST"}),
    "TENANT_IMAGE_PROFILES_PATH": frozenset({"GET"}),
    "TENANT_IMAGE_PROFILE_PATH": frozenset({"GET", "PATCH"}),
    "TENANT_REQUEST_TRACKER_PATH": frozenset({"GET"}),
    # A project's own network and its first deployment (tenant plane).
    "TENANT_BLUEPRINT_REQUESTS_PATH": frozenset({"POST"}),
    "TENANT_BLUEPRINT_REQUEST_PATH": frozenset({"GET"}),
    "TENANT_DEPLOYMENT_API_DEPLOYMENT_PATH": frozenset({"DELETE"}),
    "TENANT_FABRIC_COMPUTES_PATH": frozenset({"GET"}),
    "TENANT_FABRIC_NETWORKS_PATH": frozenset({"GET"}),
    "TENANT_FABRIC_NETWORK_PATH": frozenset({"GET", "PATCH"}),
    "TENANT_NETWORK_PROFILES_PATH": frozenset({"GET", "POST"}),
    "TENANT_REGIONS_PATH": frozenset({"GET"}),
    "TENANT_ZONES_PATH": frozenset({"GET"}),
}

#: HTTP method(s) per ``_infra_paths`` constant -- the region and quota ops.
#: Same contract as :data:`_PATHS_METHODS`: a new constant must be mapped here.
_INFRA_PATHS_METHODS: dict[str, frozenset[str]] = {
    "INFRA_NSX_MANAGERS_PATH": frozenset({"GET", "POST"}),
    "INFRA_REGIONS_PATH": frozenset({"POST"}),
    "INFRA_REGION_PATH": frozenset({"GET", "PUT"}),
    "INFRA_REGION_STORAGE_POLICIES_PATH": frozenset({"GET"}),
    "INFRA_SUPERVISORS_PATH": frozenset({"GET"}),
    "INFRA_TASK_PATH": frozenset({"GET"}),
    "INFRA_TEST_CONNECTION_PATH": frozenset({"POST"}),
    "INFRA_TRUSTED_CERTIFICATES_PATH": frozenset({"POST"}),
    "INFRA_VCENTERS_PATH": frozenset({"GET", "POST"}),
    "INFRA_VCENTER_PATH": frozenset({"PUT"}),
    "INFRA_VCENTER_REFRESH_PATH": frozenset({"POST"}),
    "INFRA_VDCS_PATH": frozenset({"GET", "POST"}),
    "INFRA_VDC_PATH": frozenset({"PUT"}),
    "INFRA_VDC_STORAGE_POLICIES_PATH": frozenset({"GET", "POST"}),
    "INFRA_VDC_STORAGE_POLICY_PATH": frozenset({"PUT"}),
    "INFRA_VDC_VM_CLASSES_PATH": frozenset({"GET", "PUT"}),
    "INFRA_VM_CLASSES_PATH": frozenset({"GET"}),
    "INFRA_ZONES_PATH": frozenset({"GET"}),
}

#: ``typed_ops`` read-path constants the provisioning writes also ``POST``
#: to (org create, project create) -- on top of the ``GET`` every
#: ``typed_ops`` constant declares.
_TYPED_OPS_EXTRA_METHODS: dict[str, frozenset[str]] = {
    "PROVIDER_ORGS_PATH": frozenset({"POST"}),
    "TENANT_PROJECTS_PATH": frozenset({"POST"}),
}


def _path_constants(module: object) -> dict[str, str]:
    """The live ``*_PATH`` string constants of *module*, by constant name."""
    return {
        name: value
        for name, value in vars(module).items()
        if name.endswith("_PATH") and isinstance(value, str) and value.startswith("/")
    }


def _mapped_op_ids(module: object, methods_by_name: dict[str, frozenset[str]]) -> set[str]:
    """``METHOD:/path`` for every ``*_PATH`` constant of *module*, by its mapped methods."""
    declared: set[str] = set()
    for name, path in _path_constants(module).items():
        if name not in methods_by_name:
            raise AssertionError(
                f"{name} has no entry in the methods map of its module — map the new "
                "constant's HTTP method(s) consciously so the reconcile keeps covering it."
            )
        declared.update(f"{method}:{path}" for method in methods_by_name[name])
    return declared


def _declared_op_ids() -> set[str]:
    """Every hand-coded ``METHOD:/path`` the vcf-automation connector dispatches.

    Sweeps the live module constants (never a hardcoded mirror): all
    typed-op paths dispatch as ``GET`` (each handler is a
    ``_request_json(target, "GET", ...)`` call); the ``_routing``
    constants map to their method by name suffix
    (:data:`_ROUTING_METHOD_BY_SUFFIX`).
    """
    typed_paths = _path_constants(_typed_ops_module)
    declared = {f"GET:{path}" for path in typed_paths.values()}
    for name, methods in _TYPED_OPS_EXTRA_METHODS.items():
        declared.update(f"{method}:{typed_paths[name]}" for method in methods)
    declared |= _mapped_op_ids(_paths_module, _PATHS_METHODS)
    declared |= _mapped_op_ids(_infra_paths_module, _INFRA_PATHS_METHODS)
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
        "PROVIDER_REGIONS_V1_PATH",
        "TENANT_ABOUT_PATH",
        "TENANT_DEPLOYMENTS_PATH",
        "TENANT_DEPLOYMENT_DETAIL_PATH",
        "TENANT_PROJECTS_PATH",
    ]
    assert sorted(_path_constants(_paths_module)) == sorted(_PATHS_METHODS)
    assert sorted(_path_constants(_infra_paths_module)) == sorted(_INFRA_PATHS_METHODS)
    assert sorted(_path_constants(_routing_module)) == [
        "PROVIDER_SESSION_PATH",
        "PROVIDER_VERSION_PATH",
        "TENANT_CSP_SESSION_PATH",
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
    swept = {
        *_path_constants(_typed_ops_module).values(),
        *_path_constants(_paths_module).values(),
        *_path_constants(_routing_module).values(),
        *_path_constants(_infra_paths_module).values(),
    }
    unswept = {
        op.op_id: op.path
        for op in (
            *VCFA_TYPED_OPS,
            *VCFA_PROVISIONING_OPS,
            *VCFA_IMAGE_OPS,
            *VCFA_NETWORK_OPS,
            *VCFA_DEPLOY_OPS,
            *VCFA_FEATURE_FLAG_OPS,
            *VCFA_INFRA_OPS,
        )
        if op.path not in swept
    }
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
        "GET:/cloudapi/v1/regions",
        "GET:/cloudapi/vcf/regions",
        "GET:/iaas/api/about",
        "GET:/iaas/api/deployments",
        "GET:/iaas/api/deployments/{id}",
        "GET:/iaas/api/projects",
        "POST:/cloudapi/1.0.0/sessions/provider",
        "POST:/csp/gateway/am/api/login",
        "POST:/iaas/api/login",
        # Provisioning surface (#3890).
        "DELETE:/cloudapi/1.0.0/globalRoles/{id}",
        "DELETE:/cloudapi/1.0.0/sessions/current",
        "DELETE:/cloudapi/1.0.0/tokens/{id}",
        "GET:/cloudapi/1.0.0/globalRoles/{id}/tenants",
        "GET:/cloudapi/1.0.0/globalRoles",
        "GET:/cloudapi/1.0.0/globalRoles/{id}/rights",
        "GET:/cloudapi/1.0.0/rights",
        "GET:/cloudapi/1.0.0/roles",
        "GET:/cloudapi/1.0.0/tokens",
        "GET:/cloudapi/1.0.0/users",
        "POST:/cloudapi/1.0.0/globalRoles",
        "POST:/cloudapi/1.0.0/globalRoles/{id}/tenants/publish",
        "POST:/cloudapi/1.0.0/globalRoles/{id}/tenants/publishAll",
        "POST:/cloudapi/1.0.0/orgs",
        "POST:/cloudapi/1.0.0/sessions",
        "POST:/cloudapi/1.0.0/users",
        "POST:/iaas/api/projects",
        "POST:/oauth/{context}/register",
        "POST:/oauth/{context}/token",
        "PUT:/cloudapi/1.0.0/globalRoles/{id}/rights",
        # Image ops.
        "GET:/iaas/api/fabric-images",
        "GET:/iaas/api/fabric-images/{id}",
        "GET:/iaas/api/image-profiles",
        "GET:/iaas/api/image-profiles/{id}",
        "GET:/iaas/api/request-tracker/{id}",
        "PATCH:/iaas/api/image-profiles/{id}",
        "POST:/iaas/api/cloud-accounts/{id}/private-image-enumeration",
        # A project's own network and its first deployment.
        "DELETE:/deployment/api/deployments/{deploymentId}",
        "GET:/blueprint/api/blueprint-requests/{requestId}",
        "GET:/iaas/api/fabric-computes",
        "GET:/iaas/api/fabric-networks-vsphere",
        "GET:/iaas/api/fabric-networks-vsphere/{id}",
        "GET:/iaas/api/network-profiles",
        "GET:/iaas/api/regions",
        "GET:/iaas/api/zones",
        "PATCH:/iaas/api/fabric-networks-vsphere/{id}",
        "POST:/blueprint/api/blueprint-requests",
        "POST:/iaas/api/network-profiles",
        # Feature flags (provider plane).
        "GET:/cloudapi/1.0.0/featureFlags",
        "GET:/cloudapi/1.0.0/featureFlags/{id}",
        "PUT:/cloudapi/1.0.0/featureFlags/{id}",
        # Regions and organization quotas (provider plane, 9.0 path form).
        "GET:/api/task/{id}",
        "GET:/cloudapi/1.0.0/virtualCenters",
        "GET:/cloudapi/vcf/nsxManagers",
        "GET:/cloudapi/vcf/regionStoragePolicies",
        "GET:/cloudapi/vcf/regions/{regionUrn}",
        "GET:/cloudapi/vcf/supervisors",
        "GET:/cloudapi/vcf/virtualDatacenterStoragePolicies",
        "GET:/cloudapi/vcf/virtualDatacenters",
        "GET:/cloudapi/vcf/virtualDatacenters/{vdcUrn}/virtualMachineClasses",
        "GET:/cloudapi/vcf/virtualMachineClasses",
        "GET:/cloudapi/vcf/zones",
        "POST:/api/admin/extension/vimServer/{id}/action/refresh",
        "POST:/cloudapi/1.0.0/ssl/trustedCertificates",
        "POST:/cloudapi/1.0.0/testConnection",
        "POST:/cloudapi/1.0.0/virtualCenters",
        "POST:/cloudapi/vcf/nsxManagers",
        "POST:/cloudapi/vcf/regions",
        "POST:/cloudapi/vcf/virtualDatacenterStoragePolicies",
        "POST:/cloudapi/vcf/virtualDatacenters",
        "PUT:/cloudapi/1.0.0/virtualCenters/{vcUrn}",
        "PUT:/cloudapi/vcf/regions/{regionUrn}",
        "PUT:/cloudapi/vcf/virtualDatacenterStoragePolicies/{vdcStoragePolicyUrn}",
        "PUT:/cloudapi/vcf/virtualDatacenters/{vdcUrn}",
        "PUT:/cloudapi/vcf/virtualDatacenters/{vdcUrn}/virtualMachineClasses",
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
            f"{spec_path.name}: expected a Swagger 2.0 document "
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
            f"{spec_path.name}: extracted zero served operations — the "
            "shelf artifact layout moved; update this lane's extraction."
        )
    return served


def test_tenant_hand_coded_paths_are_served_by_the_pinned_iaas_spec() -> None:
    """Every tenant-plane op_id is served by the pinned IaaS, blueprint or deployment spec.

    The tenant subset is selected with the connector's own
    :func:`~meho_backplane.connectors.vcf_automation._routing.plane_for_path`
    — the same classifier the transport uses to pick the auth plane —
    so the lane's plane split can never drift from dispatch reality.
    A red run here is the guard surfacing a finding; triage per
    docs/decisions/spec-reconcile-guards-standard.md.
    """
    served: set[str] = set()
    for spec_file in _TENANT_SPEC_FILES:
        served |= _swagger2_served_op_ids(require_shelf_spec(_SPEC_DIR, spec_file))
    declared = {
        op_id for op_id in _declared_op_ids() if plane_for_path(op_id.partition(":")[2]) == "tenant"
    }
    assert_op_ids_served(declared, served, spec_label=_TENANT_SPEC_LABEL)


#: Per-version provider reference lists on the shelf (#4604): the shelf
#: directory, the TSV file, and the product version a target carries.
_PROVIDER_REFERENCE_LISTS = (
    (
        "vcf-automation-9.0-reference",
        "vcf-automation-9.0-provider-infrastructure-apis-reference-operations.tsv",
        "9.0",
    ),
    (
        "vcf-automation-9.1",
        "vcf-automation-9.1-provider-infrastructure-apis-reference-operations.tsv",
        "9.1",
    ),
    (
        "vcf-automation-9.1.1",
        "vcf-automation-9.1.1-provider-infrastructure-apis-reference-operations.tsv",
        "9.1.1",
    ),
)


def _reference_list_op_ids(list_path: Path, label: str) -> set[str]:
    """Read the ``METHOD:/path`` set from a shelf reference list (TSV).

    The list has one header line (``method``, ``path``, ...) and one line
    per operation. A header that moved fails loudly rather than turning
    the compare into a vacuous pass.
    """
    lines = list_path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].split("\t")[:2] != ["method", "path"]:
        pytest.fail(f"{label}: expected a TSV header starting with 'method<TAB>path'.")
    served = {
        f"{cols[0].upper()}:{cols[1]}"
        for cols in (line.split("\t") for line in lines[1:])
        if len(cols) >= 2
    }
    if not served:
        pytest.fail(f"{label}: read zero operations; the list layout moved.")
    return served


@pytest.mark.parametrize(
    ("spec_dir", "list_file", "version"),
    _PROVIDER_REFERENCE_LISTS,
    ids=[version for _, _, version in _PROVIDER_REFERENCE_LISTS],
)
def test_region_path_is_in_the_reference_list_of_each_version(
    spec_dir: str, list_file: str, version: str
) -> None:
    """The region path picked for a version is in that version's reference list."""
    list_path = require_shelf_spec(spec_dir, list_file)
    label = f"{spec_dir}/{list_file}"
    served = _reference_list_op_ids(list_path, label)
    target = SimpleNamespace(fingerprint={"version": version}, version=None)
    declared = {f"GET:{provider_regions_path(target)}"}
    assert_op_ids_served(declared, served, spec_label=label)


def _without_braces(op_id: str) -> str:
    """``PUT:/a/{vdcUrn}`` → ``PUT:/a/vdcUrn``: the lists write some names bare."""
    return op_id.replace("{", "").replace("}", "")


def test_legacy_infra_paths_are_the_only_unlisted_ones() -> None:
    """Guard: the evidenced exclusion holds exactly the two legacy ``/api`` paths."""
    assert set(LEGACY_API_PATHS) == {
        "/api/admin/extension/vimServer/{id}/action/refresh",
        "/api/task/{id}",
    }
    assert all(path.startswith("/api/") for path in LEGACY_API_PATHS)


@pytest.mark.parametrize(
    ("spec_dir", "list_file", "version"),
    _PROVIDER_REFERENCE_LISTS,
    ids=[version for _, _, version in _PROVIDER_REFERENCE_LISTS],
)
def test_infra_paths_are_in_the_reference_list_of_each_version(
    spec_dir: str, list_file: str, version: str
) -> None:
    """Every region and quota call, in a version's path form, is in that version's list."""
    list_path = require_shelf_spec(spec_dir, list_file)
    label = f"{spec_dir}/{list_file}"
    served = {_without_braces(op_id) for op_id in _reference_list_op_ids(list_path, label)}
    target = SimpleNamespace(fingerprint={"version": version}, version=None)
    declared = {
        _without_braces(f"{method}:{provider_vcf_path(target, path)}")
        for method, _, path in (
            op_id.partition(":")
            for op_id in _mapped_op_ids(_infra_paths_module, _INFRA_PATHS_METHODS)
        )
        if path not in LEGACY_API_PATHS
    }
    assert_op_ids_served(declared, served, spec_label=label)
