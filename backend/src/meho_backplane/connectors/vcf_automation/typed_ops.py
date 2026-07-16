# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed VCFA read ops on the dual-plane session (T5 #2305).

VCF Automation ships **no vendor OpenAPI spec at all** — the provider
(management) plane publishes no machine-readable artifact and the tenant
plane ships only Swagger 2.0 fragments the ingest parser rejects by
decision (#2090). The now-retired G3.6 curated core (#2358) therefore
described ``is_enabled`` curation over ingested rows that, absent an
ingest, never actually exist: those curated op ids were dispatch-inert
on a real deploy. Typed conversion is the only path to a *working* VCFA
read surface.

This module converts the **audited read set** (evoila/meho#2294 row 22:
"org/region list + provider health"; the VCFA follow-up: "org/region
list, /iaas/api/projects + about") to ``source_kind="typed"`` operations
that dispatch through the connector's own dual-plane session — no
``endpoint_descriptor`` catalog state required. Five ops:

Provider plane (``/cloudapi/1.0.0/*`` — Basic-auth →
``X-VMWARE-VCLOUD-ACCESS-TOKEN`` JWT session):

* ``vcfa.provider.org.list`` — ``GET /cloudapi/1.0.0/orgs``
* ``vcfa.provider.region.list`` — ``GET /cloudapi/1.0.0/regions``
* ``vcfa.provider.health`` — ``GET /cloudapi/1.0.0/site`` (appliance
  site identity + product version; the provider-plane health/probe
  surface ``fingerprint`` and the operator use to confirm which VCFA
  instance a target points at and that it answers)

Tenant plane (``/iaas/api/*`` — JSON-body login → ``{"token": …}``
session):

* ``vcfa.tenant.project.list`` — ``GET /iaas/api/projects``
* ``vcfa.tenant.about`` — ``GET /iaas/api/about``

Every op declares the **plane it rides** (``provider`` / ``tenant``).
The declaration is not merely documentation: the plane a request
authenticates on is chosen at transport time by
:func:`~meho_backplane.connectors.vcf_automation._routing.plane_for_path`
applied to the op's path (``/iaas/api/*`` → tenant, everything else →
provider). :func:`_validate_typed_op_planes` asserts at import time that
each op's declared ``plane`` matches ``plane_for_path(op.path)`` — a
drift (e.g. a provider op pointed at ``/iaas/…``) fails the import
rather than surfacing as a misrouted HTTP 401 in production, since both
planes carry a ``Bearer <token>`` header but reject the other plane's
token.

The dataclass + tuple shape mirrors
:mod:`meho_backplane.connectors.argocd.ops` so the registration walk in
:meth:`~meho_backplane.connectors.vcf_automation.connector.VcfAutomationConnector.register_typed_operations`
reads identically to that sibling. Handler methods live on the connector
(each a thin :meth:`_request_json` call) so the descriptor's
``handler_ref`` round-trips through the dispatcher's
:func:`~meho_backplane.operations._handler_resolve.import_handler` walk
against a ``module.ClassName.method`` dotted path.

Endpoint + response-field facts are pinned to the VCF Automation 9.0 API
references: the cloudapi provider family at
https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-9-0-and-later/9-0/administration-sdks-cli-and-tools/about-the-vcf-automation-api.html
and the tenant IaaS family at
https://developer.broadcom.com/xapis/vm-apps-org-provisioning-service/latest/.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Literal

from meho_backplane.connectors.vcf_automation._routing import Plane, plane_for_path

__all__ = [
    "PROVIDER_ORGS_PATH",
    "PROVIDER_REGIONS_PATH",
    "PROVIDER_SITE_PATH",
    "TENANT_ABOUT_PATH",
    "TENANT_PROJECTS_PATH",
    "VCFA_TYPED_OPS",
    "VCFA_TYPED_WHEN_TO_USE_BY_GROUP",
    "VcfaTypedOp",
]


# Request paths, shared verbatim between the op metadata below and the
# connector handler bodies so the two never drift. plane_for_path()
# reads these to pick the auth plane at transport time.
PROVIDER_ORGS_PATH: Final[str] = "/cloudapi/1.0.0/orgs"
PROVIDER_REGIONS_PATH: Final[str] = "/cloudapi/1.0.0/regions"
PROVIDER_SITE_PATH: Final[str] = "/cloudapi/1.0.0/site"
TENANT_PROJECTS_PATH: Final[str] = "/iaas/api/projects"
TENANT_ABOUT_PATH: Final[str] = "/iaas/api/about"


@dataclass(frozen=True)
class VcfaTypedOp:
    """Metadata for one typed VCFA read op registered at startup.

    Fields mirror the keyword arguments
    :func:`~meho_backplane.operations.typed_register.register_typed_operation`
    accepts so the registrar splats the dataclass into the helper without
    per-op boilerplate. ``handler_attr`` is the attribute name on
    :class:`~meho_backplane.connectors.vcf_automation.connector.VcfAutomationConnector`
    exposing the async handler; the registrar resolves the bound method
    against the class so the dispatcher's ``handler_ref`` import walk can
    recover the callable from the persisted ``module.ClassName.method``
    path.

    ``plane`` records the auth plane the op rides (``"provider"`` /
    ``"tenant"``); ``path`` is the request path. The two are cross-checked
    at import time by :func:`_validate_typed_op_planes` against
    :func:`~meho_backplane.connectors.vcf_automation._routing.plane_for_path`
    so a declared-plane / path drift fails the import.
    """

    op_id: str
    handler_attr: str
    plane: Plane
    path: str
    summary: str
    description: str
    parameter_schema: dict[str, Any]
    response_schema: dict[str, Any] | None
    group_key: str
    tags: tuple[str, ...]
    safety_level: Literal["safe", "caution", "dangerous"]
    requires_approval: bool
    llm_instructions: dict[str, Any] | None


#: Curated ``when_to_use`` blurb per group. ``register_typed_operation``
#: requires a non-empty string whenever ``group_key`` is set (#731); the
#: registrar looks each op's ``group_key`` up here. Two groups, one per
#: plane — every blurb names its plane so the agent's group-selection
#: step never collapses a tenant question onto the provider group (or
#: vice versa). Group keys are deliberately distinct from the ingested
#: browse-group keys (``provider-orgs`` etc.) so the typed OperationGroup
#: rows never collide with an ingested group's row on the
#: ``(product, version, impl_id, group_key)`` natural key.
VCFA_TYPED_WHEN_TO_USE_BY_GROUP: Final[dict[str, str]] = {
    "vcfa-provider-reads": (
        "Use on the VCFA **provider (management) plane** to read the "
        "cross-tenant appliance view a system administrator sees: list "
        "organizations on the appliance (vcfa.provider.org.list), list "
        "regions — the VCFA 9 evolution of the vCloud-Director provider "
        "VDC, each backing compute/network under an NSX domain "
        "(vcfa.provider.region.list), or read appliance site identity + "
        "product version as a health/probe before heavier reads "
        "(vcfa.provider.health). Provider-plane ops authenticate with the "
        "admin@System (or equivalent) Basic-auth session and never "
        "succeed against a tenant token. For per-tenant project / "
        "deployment reads switch to the tenant-plane group."
    ),
    "vcfa-tenant-reads": (
        "Use on the VCFA **tenant plane** to read within one tenant "
        "organization: list projects, the deployment-scoping construct "
        "every deployment belongs to (vcfa.tenant.project.list), or read "
        "the IaaS API self-describe surface — supported API versions + "
        "latest version — as a tenant-plane reachability/version probe "
        "(vcfa.tenant.about). Tenant-plane ops authenticate with the "
        "tenant org login (POST /iaas/api/login) and never succeed "
        "against the provider JWT. For the cross-tenant org/region view "
        "switch to the provider-plane group."
    ),
}


# ---------------------------------------------------------------------------
# Shared parameter-schema fragments
# ---------------------------------------------------------------------------

#: The cloudapi provider-plane pagination query params (FIQL-style
#: ``page`` / ``pageSize``) shared by org.list + region.list.
_PROVIDER_PAGINATION_PROPERTIES: dict[str, Any] = {
    "page": {
        "type": "integer",
        "minimum": 1,
        "description": "1-based page number for the provider-plane result set. Omit for page 1.",
    },
    "pageSize": {
        "type": "integer",
        "minimum": 1,
        "maximum": 128,
        "description": "Page size (max 128). Omit for the appliance default.",
    },
}


# ---------------------------------------------------------------------------
# Provider plane
# ---------------------------------------------------------------------------

_PROVIDER_ORG_LIST = VcfaTypedOp(
    op_id="vcfa.provider.org.list",
    handler_attr="provider_org_list",
    plane="provider",
    path=PROVIDER_ORGS_PATH,
    summary="List VCFA organizations on the appliance (provider plane).",
    description=(
        "Lists organizations on the VCFA appliance via "
        "GET /cloudapi/1.0.0/orgs on the provider (management) plane — the "
        "cross-tenant inventory a system administrator sees (every tenant "
        "appears here). Supports FIQL-style 'page' / 'pageSize' pagination. "
        "Returns a 'values' array; each entry carries id, name, displayName, "
        "description, isEnabled, and orgVdcCount, plus 'resultTotal' for "
        "pagination. safety_level=safe, read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": dict(_PROVIDER_PAGINATION_PROPERTIES),
        "additionalProperties": False,
    },
    response_schema={
        "type": "object",
        "properties": {
            "values": {"type": ["array", "null"]},
            "resultTotal": {"type": ["integer", "null"]},
        },
        "additionalProperties": True,
    },
    group_key="vcfa-provider-reads",
    tags=("read-only", "vcfa", "provider"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call on the provider plane to enumerate the organizations "
            "(tenants) on a VCFA appliance — the system-admin inventory "
            "entry point. Paginate with 'page' / 'pageSize' on large "
            "appliances."
        ),
        "output_shape": (
            "{values: [Org, ...], resultTotal: int}. Each Org carries id, "
            "name, displayName, isEnabled, orgVdcCount."
        ),
        "next_step": (
            "Switch to the tenant plane (vcfa.tenant.project.list) for "
            "per-org reads, or vcfa.provider.region.list for the compute "
            "inventory backing those orgs."
        ),
    },
)

_PROVIDER_REGION_LIST = VcfaTypedOp(
    op_id="vcfa.provider.region.list",
    handler_attr="provider_region_list",
    plane="provider",
    path=PROVIDER_REGIONS_PATH,
    summary="List VCFA regions on the appliance (provider plane).",
    description=(
        "Lists VCFA regions via GET /cloudapi/1.0.0/regions on the provider "
        "plane — the VCFA 9 evolution of the vCloud-Director provider VDC. "
        "Each region groups compute/memory/networking under one NSX domain, "
        "typically backed by one or more VCF workload domains. Supports "
        "'page' / 'pageSize' pagination. Returns a 'values' array; each "
        "entry carries id, name, description, nsxManager, supervisors, and "
        "isEnabled, plus 'resultTotal'. Use to answer 'what compute capacity "
        "does this appliance offer' or 'which region backs tenant X'. "
        "safety_level=safe, read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": dict(_PROVIDER_PAGINATION_PROPERTIES),
        "additionalProperties": False,
    },
    response_schema={
        "type": "object",
        "properties": {
            "values": {"type": ["array", "null"]},
            "resultTotal": {"type": ["integer", "null"]},
        },
        "additionalProperties": True,
    },
    group_key="vcfa-provider-reads",
    tags=("read-only", "vcfa", "provider"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call on the provider plane to list the appliance's regions and "
            "the compute/network capacity each offers."
        ),
        "output_shape": (
            "{values: [Region, ...], resultTotal: int}. Each Region carries "
            "id, name, nsxManager, supervisors, isEnabled."
        ),
        "next_step": (
            "Cross-reference a region id against tenant-plane deployment "
            "reads to map workloads to regions."
        ),
    },
)

_PROVIDER_HEALTH = VcfaTypedOp(
    op_id="vcfa.provider.health",
    handler_attr="provider_health",
    plane="provider",
    path=PROVIDER_SITE_PATH,
    summary="Read VCFA provider-plane appliance health/identity (site).",
    description=(
        "Reads VCFA appliance site identity via GET /cloudapi/1.0.0/site on "
        "the provider plane — the provider-plane health/probe surface. "
        "Returns id, name, description, restName, and the product version "
        "string identifying the appliance build. A 2xx here confirms the "
        "provider plane is reachable and which VCFA instance the target "
        "points at; it is the provider-plane analogue of the tenant-plane "
        "vcfa.tenant.about probe. safety_level=safe, read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    response_schema={
        "type": "object",
        "properties": {
            "id": {"type": ["string", "null"]},
            "name": {"type": ["string", "null"]},
            "productVersion": {"type": ["string", "null"]},
        },
        "additionalProperties": True,
    },
    group_key="vcfa-provider-reads",
    tags=("read-only", "vcfa", "provider", "health"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call as a pre-flight provider-plane probe: confirm the "
            "appliance answers and read its product version before heavier "
            "provider reads, or as a post-deploy health check."
        ),
        "output_shape": (
            "{id, name, description, restName, productVersion}. The "
            "productVersion string identifies the appliance build."
        ),
        "next_step": (
            "Confirm productVersion is in the supported range, then proceed "
            "to vcfa.provider.org.list or vcfa.provider.region.list."
        ),
    },
)


# ---------------------------------------------------------------------------
# Tenant plane
# ---------------------------------------------------------------------------

_TENANT_PROJECT_LIST = VcfaTypedOp(
    op_id="vcfa.tenant.project.list",
    handler_attr="tenant_project_list",
    plane="tenant",
    path=TENANT_PROJECTS_PATH,
    summary="List projects within the tenant organization (tenant plane).",
    description=(
        "Lists projects within the tenant organization via "
        "GET /iaas/api/projects on the tenant plane. Projects are the "
        "deployment-scoping construct — every deployment belongs to exactly "
        "one project. Supports OData-style $filter / $orderby / $top / $skip "
        "query params. Returns a 'content' array; each entry carries id, "
        "name, description, organizationId, administrators[], members[], and "
        "operationTimeout, plus totalElements / totalPages page metadata. "
        "safety_level=safe, read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "$filter": {
                "type": "string",
                "minLength": 1,
                "description": ("Optional OData filter expression (e.g. \"name eq 'prod'\")."),
            },
            "$orderby": {
                "type": "string",
                "minLength": 1,
                "description": "Optional OData order-by expression.",
            },
            "$top": {
                "type": "integer",
                "minimum": 1,
                "description": "Optional page size (OData $top).",
            },
            "$skip": {
                "type": "integer",
                "minimum": 0,
                "description": "Optional offset (OData $skip).",
            },
        },
        "additionalProperties": False,
    },
    response_schema={
        "type": "object",
        "properties": {
            "content": {"type": ["array", "null"]},
            "totalElements": {"type": ["integer", "null"]},
            "totalPages": {"type": ["integer", "null"]},
        },
        "additionalProperties": True,
    },
    group_key="vcfa-tenant-reads",
    tags=("read-only", "vcfa", "tenant"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call on the tenant plane to list projects in the tenant org — "
            "the scoping construct for deployments and blueprint access. "
            "Narrow with $filter when the operator named a project."
        ),
        "output_shape": (
            "{content: [Project, ...], totalElements, totalPages}. Each "
            "Project carries id, name, organizationId, administrators[]."
        ),
        "next_step": ("Use a project id as the $filter scope for a follow-up deployment read."),
    },
)

_TENANT_ABOUT = VcfaTypedOp(
    op_id="vcfa.tenant.about",
    handler_attr="tenant_about",
    plane="tenant",
    path=TENANT_ABOUT_PATH,
    summary="Read the tenant IaaS API self-describe surface (tenant plane).",
    description=(
        "Reads the tenant IaaS API self-describe surface via "
        "GET /iaas/api/about on the tenant plane — supportedApis[] (each "
        "with an apiVersion + documentation URL) and latestApiVersion. A "
        "2xx here confirms tenant-plane reachability and version "
        "negotiation; it is the tenant-plane analogue of the provider-plane "
        "vcfa.provider.health probe. safety_level=safe, read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    response_schema={
        "type": "object",
        "properties": {
            "latestApiVersion": {"type": ["string", "null"]},
            "supportedApis": {"type": ["array", "null"]},
        },
        "additionalProperties": True,
    },
    group_key="vcfa-tenant-reads",
    tags=("read-only", "vcfa", "tenant", "health"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call as a tenant-plane probe: confirm the IaaS API answers and "
            "read the latest supported API version before tenant catalog "
            "reads, or as a post-deploy health check."
        ),
        "output_shape": ("{supportedApis: [{apiVersion, ...}], latestApiVersion}."),
        "next_step": (
            "Confirm latestApiVersion is in the supported range, then "
            "proceed to vcfa.tenant.project.list."
        ),
    },
)


#: The five typed VCFA read ops the connector registers at lifespan
#: startup — the audited read set (#2294). Ordered provider → tenant,
#: probe last within each plane, to match the operator's typical drill
#: path (inventory first, health as needed).
VCFA_TYPED_OPS: Final[tuple[VcfaTypedOp, ...]] = (
    _PROVIDER_ORG_LIST,
    _PROVIDER_REGION_LIST,
    _PROVIDER_HEALTH,
    _TENANT_PROJECT_LIST,
    _TENANT_ABOUT,
)


def _validate_typed_op_planes() -> None:
    """Assert every op's declared ``plane`` matches ``plane_for_path(op.path)``.

    Load-bearing: the auth plane a request rides is picked at transport
    time by :func:`~meho_backplane.connectors.vcf_automation._routing.plane_for_path`
    on the op's path, not by the ``plane`` field. The field is the
    op author's declaration of intent; this check keeps it honest so a
    provider op accidentally pointed at ``/iaas/…`` (or vice versa) fails
    the import rather than surfacing as a misrouted HTTP 401 at dispatch.
    Also asserts each op references a group with a curated
    ``when_to_use`` blurb.
    """
    for op in VCFA_TYPED_OPS:
        derived = plane_for_path(op.path)
        if derived != op.plane:
            raise AssertionError(
                f"VCFA typed op {op.op_id!r} declares plane={op.plane!r} but "
                f"plane_for_path({op.path!r}) returns {derived!r}"
            )
        if op.group_key not in VCFA_TYPED_WHEN_TO_USE_BY_GROUP:
            raise AssertionError(
                f"VCFA typed op {op.op_id!r} references group {op.group_key!r} "
                f"with no curated when_to_use in VCFA_TYPED_WHEN_TO_USE_BY_GROUP"
            )


_validate_typed_op_planes()
