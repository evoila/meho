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
import re
from types import SimpleNamespace
from typing import TYPE_CHECKING
from urllib.parse import quote

import pytest

from meho_backplane.connectors.vcf_automation import _paths as _paths_module
from meho_backplane.connectors.vcf_automation import _provider_paths as _provider_paths_module
from meho_backplane.connectors.vcf_automation import _routing as _routing_module
from meho_backplane.connectors.vcf_automation import typed_ops as _typed_ops_module
from meho_backplane.connectors.vcf_automation._routing import plane_for_path
from meho_backplane.connectors.vcf_automation.deploy_ops import VCFA_DEPLOY_OPS
from meho_backplane.connectors.vcf_automation.feature_flag_ops import VCFA_FEATURE_FLAG_OPS
from meho_backplane.connectors.vcf_automation.image_ops import VCFA_IMAGE_OPS
from meho_backplane.connectors.vcf_automation.network_ops import VCFA_NETWORK_OPS
from meho_backplane.connectors.vcf_automation.provider_network_ops import (
    VCFA_PROVIDER_NETWORK_OPS,
)
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
    for name, path in _path_constants(_paths_module).items():
        if name not in _PATHS_METHODS:
            raise AssertionError(
                f"_paths.{name} has no entry in _PATHS_METHODS — map the new "
                "constant's HTTP method(s) there consciously so the reconcile "
                "keeps covering it."
            )
        declared.update(f"{method}:{path}" for method in _PATHS_METHODS[name])
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
        *_path_constants(_provider_paths_module).values(),
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
            *VCFA_PROVIDER_NETWORK_OPS,
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


#: HTTP method(s) per ``_provider_paths`` constant: the provider organization
#: settings and networking ops. A new constant missing here fails the sweep.
_PROVIDER_PATHS_METHODS: dict[str, frozenset[str]] = {
    "PROVIDER_ORG_PATH": frozenset({"GET", "PUT"}),
    "PROVIDER_ORG_NETWORKING_PATH": frozenset({"GET", "PUT"}),
    "PROVIDER_TIER0_ROUTERS_PATH": frozenset({"GET"}),
    "PROVIDER_IP_SPACES_PATH": frozenset({"GET", "POST"}),
    "PROVIDER_IP_SPACE_PATH": frozenset({"GET", "PUT", "DELETE"}),
    "PROVIDER_GATEWAYS_PATH": frozenset({"GET", "POST"}),
    "PROVIDER_GATEWAY_PATH": frozenset({"GET", "DELETE"}),
    "PROVIDER_IP_SPACE_ASSOCIATIONS_PATH": frozenset({"GET", "POST"}),
    "PROVIDER_IP_SPACE_ASSOCIATION_PATH": frozenset({"GET", "DELETE"}),
    "PROVIDER_REGIONAL_NETWORKING_PATH": frozenset({"GET", "POST"}),
    "PROVIDER_REGIONAL_NETWORKING_SETTING_PATH": frozenset({"GET", "DELETE"}),
    "PROVIDER_EDGE_CLUSTERS_PATH": frozenset({"GET"}),
    "INFRA_TASK_PATH": frozenset({"GET"}),
}

#: The legacy ``/api`` task read is an evidenced exclusion: no 9.x reference
#: lists a legacy call, and the vendor's own SDK (``go-vcloud-director`` v3)
#: uses it on VCF Automation 9 -- the task read behind every ``202`` task link.
_PROVIDER_LEGACY_API_OP_IDS: frozenset[str] = frozenset({"GET:/api/task/{id}"})


def _provider_network_op_ids(version: str) -> set[str]:
    """Every ``METHOD:/path`` the networking ops send to a target of *version*.

    Each path goes through the same
    :func:`~meho_backplane.connectors.vcf_automation._provider_paths.provider_path`
    the handlers use, so the ``/cloudapi/vcf/`` to ``/cloudapi/v1/`` switch is
    checked as it runs. The ``{name}`` placeholders stay in the path.
    """
    target = SimpleNamespace(fingerprint={"version": version}, version=None)
    constants = _path_constants(_provider_paths_module)
    declared: set[str] = set()
    for name, path in constants.items():
        placeholders = {key: f"{{{key}}}" for key in _placeholder_names(path)}
        resolved = _provider_paths_module.provider_path(target, path, **placeholders)
        for marker in placeholders.values():
            resolved = resolved.replace(quote(marker, safe=""), marker)
        declared.update(f"{method}:{resolved}" for method in _PROVIDER_PATHS_METHODS[name])
    return declared


def _placeholder_names(path: str) -> list[str]:
    return re.findall(r"\{(\w+)\}", path)


def _without_braces(op_ids: set[str]) -> set[str]:
    """The reference lists write a path parameter with or without braces; compare both ways."""
    return {op_id.replace("{", "").replace("}", "") for op_id in op_ids}


def test_provider_network_path_constants_are_pinned() -> None:
    assert sorted(_path_constants(_provider_paths_module)) == sorted(_PROVIDER_PATHS_METHODS)


def test_provider_network_paths_switch_to_v1_on_9_1() -> None:
    assert "POST:/cloudapi/vcf/ipSpaces" in _provider_network_op_ids("9.0")
    on_91 = _provider_network_op_ids("9.1")
    assert "POST:/cloudapi/v1/ipSpaces" in on_91
    assert "DELETE:/cloudapi/v1/providerGateways/{gatewayId}" in on_91
    assert "PUT:/cloudapi/1.0.0/orgs/{orgUrn}" in on_91
    assert not any("/cloudapi/vcf/" in op_id for op_id in on_91)


@pytest.mark.parametrize(
    ("spec_dir", "list_file", "version"),
    _PROVIDER_REFERENCE_LISTS,
    ids=[version for _, _, version in _PROVIDER_REFERENCE_LISTS],
)
def test_provider_network_paths_are_in_the_reference_list_of_each_version(
    spec_dir: str, list_file: str, version: str
) -> None:
    """Every call of the organization settings and networking ops is in that version's list."""
    list_path = require_shelf_spec(spec_dir, list_file)
    label = f"{spec_dir}/{list_file}"
    served = _without_braces(_reference_list_op_ids(list_path, label))
    declared = _without_braces(_provider_network_op_ids(version) - _PROVIDER_LEGACY_API_OP_IDS)
    assert_op_ids_served(declared, served, spec_label=label)


def test_provider_network_legacy_task_read_is_the_only_unlisted_call() -> None:
    """Guard: the evidenced exclusion holds exactly the legacy task read."""
    for version in ("9.0", "9.1"):
        legacy = {op_id for op_id in _provider_network_op_ids(version) if ":/api/" in op_id}
        assert legacy == _PROVIDER_LEGACY_API_OP_IDS
