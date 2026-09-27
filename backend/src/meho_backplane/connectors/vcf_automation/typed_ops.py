# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed VCFA read ops on the dual-plane session (T5 #2305).

The provider (management) plane publishes no machine-readable spec at
all; for the tenant plane the vendor's public SDK ships only Swagger 2.0
fragments the ingest parser rejects by decision (#2090) -- a 9.1
appliance does serve an OpenAPI 3.0.1 IaaS document at
``/iaas-api/swagger/v3/api-docs/2021-07-15``, but no such artifact is
pinned here. The now-retired G3.6 curated core (#2358) therefore
described ``is_enabled`` curation over ingested rows that, absent an
ingest, never actually exist: those curated op ids were dispatch-inert
on a real deploy. Typed conversion is the only path to a *working* VCFA
read surface.

This module converts the **audited read set** (evoila/meho#2294 row 22:
"org/region list + provider health"; the VCFA follow-up: "org/region
list, /iaas/api/projects + about") to ``source_kind="typed"`` operations
that dispatch through the connector's own dual-plane session — no
``endpoint_descriptor`` catalog state required. Seven ops — the five
#2294 audited reads plus the #2839 tenant deployment list and the
#2960 tenant deployment detail read:

Provider plane (``/cloudapi/1.0.0/*`` — Basic-auth →
``X-VMWARE-VCLOUD-ACCESS-TOKEN`` JWT session):

* ``vcfa.provider.org.list`` — ``GET /cloudapi/1.0.0/orgs``
* ``vcfa.provider.region.list`` — ``GET /cloudapi/vcf/regions``
* ``vcfa.provider.health`` — authenticated ``GET /cloudapi/1.0.0/orgs``
  (``pageSize=1``) plus the unauthenticated ``GET /iaas/api/about`` API
  version read, returned as one structured health object. Both paths serve
  GET on VCFA 9.0 and 9.1; the former ``GET /cloudapi/1.0.0/site`` answers
  405 on 9.1 (evoila/meho#3865).

Tenant plane (``/iaas/api/*`` — JSON-body login → ``{"token": …}``
session):

* ``vcfa.tenant.project.list`` — ``GET /iaas/api/projects``
* ``vcfa.tenant.deployment.list`` — ``GET /iaas/api/deployments``
* ``vcfa.tenant.deployment.get`` — ``GET /iaas/api/deployments/{id}``
* ``vcfa.tenant.about`` — ``GET /iaas/api/about``

Every op declares the **plane it rides** (``provider`` / ``tenant``).
The declaration is not merely documentation: the plane a request
authenticates on is chosen at transport time by
:func:`~meho_backplane.connectors.vcf_automation._routing.plane_for_path`
applied to the op's path (``/iaas/api/*`` → tenant, everything else →
provider). :func:`validate_typed_ops` asserts at import time that
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

The tenant-bootstrap **provisioning** ops (org / role / user / API token /
project writes, the right + role reads, the tenant login test,
evoila/meho#3890) reuse :class:`VcfaTypedOp`, the group blurbs below and
:func:`validate_typed_ops`, but live in :mod:`.provisioning_ops` so this
module stays the read surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Literal

from meho_backplane.connectors.vcf_automation._routing import Plane, plane_for_path

__all__ = [
    "PROVIDER_ORGS_PATH",
    "PROVIDER_REGIONS_PATH",
    "TENANT_ABOUT_PATH",
    "TENANT_DEPLOYMENTS_PATH",
    "TENANT_DEPLOYMENT_DETAIL_PATH",
    "TENANT_PROJECTS_PATH",
    "VCFA_TYPED_OPS",
    "VCFA_TYPED_WHEN_TO_USE_BY_GROUP",
    "VcfaTypedOp",
    "validate_typed_ops",
]


# Request paths, shared verbatim between the op metadata below and the
# connector handler bodies so the two never drift. plane_for_path()
# reads these to pick the auth plane at transport time.
PROVIDER_ORGS_PATH: Final[str] = "/cloudapi/1.0.0/orgs"
#: VCFA 9.0 serves Region under the ``vcf/`` cloudapi prefix, not the
#: classic ``1.0.0/`` one (#2983 reconcile finding): the SDK the vendor's
#: own terraform provider pins for VCFA 9.0 maps regions as
#: ``OpenApiPathVcf`` (``go-vcloud-director@v3.0.0``
#: ``govcd/openapi_endpoints.go``), and the shelf's live-probe record
#: has ``GET /cloudapi/1.0.0/regions`` → 404 with ``/cloudapi/vcf/regions``
#: serving (consumer kb ``vcf-automation-9.0-provider-object-model.md``,
#: probes 2026-05-16 / 2026-07-21).
PROVIDER_REGIONS_PATH: Final[str] = "/cloudapi/vcf/regions"
TENANT_PROJECTS_PATH: Final[str] = "/iaas/api/projects"
TENANT_DEPLOYMENTS_PATH: Final[str] = "/iaas/api/deployments"
#: Path template for the per-id deployment detail read. The ``{id}``
#: placeholder is substituted (percent-encoded, empty safe set) by the
#: connector handler; ``plane_for_path`` classifies the template itself,
#: so the declared plane is validated before any substitution happens.
TENANT_DEPLOYMENT_DETAIL_PATH: Final[str] = "/iaas/api/deployments/{id}"
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
    at import time by :func:`validate_typed_ops` against
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
    safety_level: Literal["safe", "caution", "dangerous", "destructive"]
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
        "(vcfa.provider.region.list), or run a provider-plane health check "
        "(authenticated reachability + API version) before heavier reads "
        "(vcfa.provider.health), or look up rights (vcfa.provider.right.list) "
        "and global / per-org roles (vcfa.provider.role.list) before building a "
        "custom role or a user. Provider-plane ops authenticate with the "
        "admin@System (or equivalent) Basic-auth session and never "
        "succeed against a tenant token. For per-tenant project / "
        "deployment reads switch to the tenant-plane group."
    ),
    "vcfa-tenant-reads": (
        "Use on the VCFA **tenant plane** to read within one tenant "
        "organization: list projects, the deployment-scoping construct "
        "every deployment belongs to (vcfa.tenant.project.list), list "
        "deployments — which exist, which failed, which are stuck "
        "in-progress, narrowable with a status $filter "
        "(vcfa.tenant.deployment.list), read one deployment's detail by "
        "id (vcfa.tenant.deployment.get), or read "
        "the IaaS API self-describe surface — supported API versions + "
        "latest version — as a tenant-plane reachability/version probe "
        "(vcfa.tenant.about), or test only the tenant login -- does the "
        "target's API token authenticate? -- with no data call "
        "(vcfa.tenant.login.test). Tenant-plane ops authenticate with the "
        "tenant org login (POST /iaas/api/login) and never succeed "
        "against the provider JWT. For the cross-tenant org/region view "
        "switch to the provider-plane group."
    ),
    "vcfa-provider-writes": (
        "Use on the VCFA **provider plane** to bootstrap a tenant: create a "
        "tenant organization (vcfa.provider.org.create), a custom global role "
        "from a base role plus extra rights and publish it to the org "
        "(vcfa.provider.role.create), a local org user holding that role with "
        "its password read from Vault (vcfa.provider.user.create), and mint "
        "that user's API token straight into Vault -- never returned -- "
        "(vcfa.provider.api_token.create) or revoke it "
        "(vcfa.provider.api_token.revoke). All approval-gated; creates are "
        "idempotent on name and answer 'unchanged' when the object exists."
    ),
    "vcfa-tenant-writes": (
        "Use on the VCFA **tenant plane** to create a project in the tenant "
        "organization (vcfa.tenant.project.create), authenticated by the "
        "target's tenant login. Approval-gated; idempotent on name."
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
        "Lists VCFA regions via GET /cloudapi/vcf/regions on the provider "
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
    # The authenticated leg rides the provider session on the org list --
    # a path verified to serve GET on both VCFA 9.0 and 9.1. The former
    # ``GET /cloudapi/1.0.0/site`` answers 405 on 9.1 (evoila/meho#3865).
    path=PROVIDER_ORGS_PATH,
    summary="Check VCFA provider-plane health: authenticated reachability + API version.",
    description=(
        "Provider-plane health check for a VCFA appliance. Establishes (or "
        "reuses) the provider session and reads GET /cloudapi/1.0.0/orgs "
        "with pageSize=1 to prove the provider plane answers an "
        "authenticated request, then reads the unauthenticated "
        "GET /iaas/api/about for the appliance's IaaS API versions. Returns "
        "{provider_plane: {reachable, authenticated, check, org_count}, "
        "api: {reachable, latestApiVersion, supportedApiVersions} or "
        "{reachable: false, error}}. A provider-plane failure (bad "
        "credential, appliance down) surfaces as the op's error; an about "
        "failure is reported inside 'api' without failing the check. "
        "safety_level=safe, read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    response_schema={
        "type": "object",
        "properties": {
            "provider_plane": {"type": "object"},
            "api": {"type": "object"},
        },
        "required": ["provider_plane", "api"],
        "additionalProperties": True,
    },
    group_key="vcfa-provider-reads",
    tags=("read-only", "vcfa", "provider", "health"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call as a pre-flight provider-plane probe: confirm the "
            "appliance answers an authenticated provider request and read "
            "its API version before heavier provider reads, or as a "
            "post-deploy health check."
        ),
        "output_shape": (
            "{provider_plane: {reachable, authenticated, check, org_count}, "
            "api: {reachable, latestApiVersion, supportedApiVersions}}."
        ),
        "next_step": (
            "Proceed to vcfa.provider.org.list or vcfa.provider.region.list; "
            "use vcfa.tenant.about to check the tenant-plane session."
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

_TENANT_DEPLOYMENT_LIST = VcfaTypedOp(
    op_id="vcfa.tenant.deployment.list",
    handler_attr="tenant_deployment_list",
    plane="tenant",
    path=TENANT_DEPLOYMENTS_PATH,
    summary="List deployments within the tenant organization (tenant plane).",
    description=(
        "Lists deployments within the tenant organization via "
        "GET /iaas/api/deployments on the tenant plane — the tenant-plane "
        "inventory answer to 'which deployments exist, which failed, which "
        "are stuck in-progress'; typically the first read when a tenant "
        "reports 'my deployment didn't come up'. Supports OData-style "
        "$filter / $orderby / $top / $skip query params; narrow to failures "
        "with a status filter (e.g. \"status eq 'CREATE_FAILED'\"). Returns a "
        "'content' array; each entry carries id, name, description, status, "
        "projectId, blueprintId, ownedBy, createdAt, lastUpdatedAt, and "
        "resources[], plus totalElements / totalPages page metadata. "
        "safety_level=safe, read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "$filter": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Optional OData filter expression (e.g. \"status eq 'CREATE_FAILED'\")."
                ),
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
            "Call on the tenant plane to list deployments in the tenant org — "
            "the first read when a tenant asks 'which deployments exist' or "
            "reports 'my deployment didn't come up'. Narrow with a status "
            "$filter (e.g. \"status eq 'CREATE_FAILED'\") to isolate failed or "
            "in-progress deployments."
        ),
        "output_shape": (
            "{content: [Deployment, ...], totalElements, totalPages}. Each "
            "Deployment carries id, name, status, projectId, blueprintId, "
            "ownedBy, createdAt, lastUpdatedAt, resources[]."
        ),
        "next_step": (
            "Drill into one deployment's full detail with "
            "vcfa.tenant.deployment.get, or cross-reference projectId "
            "against vcfa.tenant.project.list."
        ),
    },
)

_TENANT_DEPLOYMENT_GET = VcfaTypedOp(
    op_id="vcfa.tenant.deployment.get",
    handler_attr="tenant_deployment_get",
    plane="tenant",
    path=TENANT_DEPLOYMENT_DETAIL_PATH,
    summary="Read one deployment by id within the tenant organization (tenant plane).",
    description=(
        "Reads one deployment's detail via GET /iaas/api/deployments/{id} "
        "on the tenant plane — the drill-down after "
        "vcfa.tenant.deployment.list surfaced a deployment worth "
        "inspecting (a failure to triage, a stuck in-progress create). "
        "Requires the deployment id (from vcfa.tenant.deployment.list). "
        "Returns the single deployment object: id, name, description, "
        "status, projectId, blueprintId, ownedBy, createdAt, "
        "lastUpdatedAt, and resources[] — the same shape as one "
        "'content' entry of the list op. safety_level=safe, read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "id": {
                "type": "string",
                "minLength": 1,
                "description": "The deployment id (from vcfa.tenant.deployment.list).",
            },
        },
        "required": ["id"],
        "additionalProperties": False,
    },
    response_schema={
        "type": "object",
        "properties": {
            "id": {"type": ["string", "null"]},
            "name": {"type": ["string", "null"]},
            "status": {"type": ["string", "null"]},
            "projectId": {"type": ["string", "null"]},
            "resources": {"type": ["array", "null"]},
        },
        "additionalProperties": True,
    },
    group_key="vcfa-tenant-reads",
    tags=("read-only", "vcfa", "tenant"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call on the tenant plane with a deployment id to read one "
            "deployment's full detail — the drill-down after "
            "vcfa.tenant.deployment.list surfaced a failed or stuck "
            "deployment worth inspecting."
        ),
        "output_shape": (
            "{id, name, status, projectId, blueprintId, ownedBy, "
            "createdAt, lastUpdatedAt, resources: [...]}. One deployment "
            "object — the same shape as one 'content' entry of the list op."
        ),
        "next_step": (
            "Inspect status and resources[] to triage the deployment, or "
            "cross-reference projectId against vcfa.tenant.project.list."
        ),
        "parameter_hints": {"id": "The deployment id from vcfa.tenant.deployment.list."},
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


#: The seven typed VCFA read ops the connector registers at lifespan
#: startup — the #2294 audited read set plus the #2839 tenant deployment
#: list and the #2960 tenant deployment detail read. Ordered provider →
#: tenant, probe last within each plane, to match the operator's typical
#: drill path (inventory first, detail next, health as needed).
VCFA_TYPED_OPS: Final[tuple[VcfaTypedOp, ...]] = (
    _PROVIDER_ORG_LIST,
    _PROVIDER_REGION_LIST,
    _PROVIDER_HEALTH,
    _TENANT_PROJECT_LIST,
    _TENANT_DEPLOYMENT_LIST,
    _TENANT_DEPLOYMENT_GET,
    _TENANT_ABOUT,
)


def validate_typed_ops(ops: tuple[VcfaTypedOp, ...]) -> None:
    """Assert every op's declared ``plane`` matches ``plane_for_path(op.path)``.

    Load-bearing: the auth plane a request rides is picked at transport
    time by :func:`~meho_backplane.connectors.vcf_automation._routing.plane_for_path`
    on the op's path, not by the ``plane`` field. The field is the
    op author's declaration of intent; this check keeps it honest so a
    provider op accidentally pointed at ``/iaas/…`` (or vice versa) fails
    the import rather than surfacing as a misrouted HTTP 401 at dispatch.
    Also asserts each op references a group with a curated
    ``when_to_use`` blurb. Run at import for :data:`VCFA_TYPED_OPS` here and
    for the provisioning ops in :mod:`.provisioning_ops`.
    """
    for op in ops:
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


validate_typed_ops(VCFA_TYPED_OPS)
