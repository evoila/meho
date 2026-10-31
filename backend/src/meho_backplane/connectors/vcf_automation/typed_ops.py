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
* ``vcfa.provider.region.list`` — ``GET /cloudapi/vcf/regions`` on 9.0,
  ``GET /cloudapi/v1/regions`` on 9.1 and newer (:func:`provider_regions_path`)
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

from packaging.version import InvalidVersion, Version

from meho_backplane.connectors.resolver import resolve_target_version
from meho_backplane.connectors.vcf_automation._op_schemas import (
    ABOUT_RESPONSE,
    IAAS_DEPLOYMENT_FIELDS,
    IAAS_PROJECT_FIELDS,
    ORG_FIELDS,
    REGION_FIELDS,
    cloudapi_page,
    field,
    guidance,
    iaas_page,
    obj,
)
from meho_backplane.connectors.vcf_automation._routing import Plane, plane_for_path
from meho_backplane.connectors.vcf_automation.session import VcfAutomationTargetLike
from meho_backplane.connectors.vmware_shared.vcfa_content import CONTENT_GROUP_TEXTS, render_text

__all__ = [
    "PROVIDER_ORGS_PATH",
    "PROVIDER_REGIONS_PATH",
    "PROVIDER_REGIONS_V1_PATH",
    "TENANT_ABOUT_PATH",
    "TENANT_DEPLOYMENTS_PATH",
    "TENANT_DEPLOYMENT_DETAIL_PATH",
    "TENANT_PROJECTS_PATH",
    "VCFA_TYPED_OPS",
    "VCFA_TYPED_WHEN_TO_USE_BY_GROUP",
    "VcfaTypedOp",
    "provider_regions_path",
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
#: VCFA 9.1 and 9.1.1 document Region only under the ``v1/`` cloudapi
#: prefix ("Query Regions"); their API references show no ``vcf/`` path
#: at all (evoila/meho#4604). A 9.1 appliance still answers the 9.0 path,
#: but that path is undocumented there.
PROVIDER_REGIONS_V1_PATH: Final[str] = "/cloudapi/v1/regions"
TENANT_PROJECTS_PATH: Final[str] = "/iaas/api/projects"
TENANT_DEPLOYMENTS_PATH: Final[str] = "/iaas/api/deployments"
#: Path template for the per-id deployment detail read. The ``{id}``
#: placeholder is substituted (percent-encoded, empty safe set) by the
#: connector handler; ``plane_for_path`` classifies the template itself,
#: so the declared plane is validated before any substitution happens.
TENANT_DEPLOYMENT_DETAIL_PATH: Final[str] = "/iaas/api/deployments/{id}"
TENANT_ABOUT_PATH: Final[str] = "/iaas/api/about"

#: First VCF Automation release whose API reference shows
#: :data:`PROVIDER_REGIONS_V1_PATH` instead of :data:`PROVIDER_REGIONS_PATH`.
_REGIONS_V1_MIN_VERSION: Final = Version("9.1")
#: Upper guard so a date-shaped label that happens to parse (``2021.07``)
#: is not taken for a product release (same guard as the 9.1 login pick).
_PRODUCT_VERSION_CEILING: Final = Version("100")


def provider_regions_path(target: VcfAutomationTargetLike) -> str:
    """Return the region list path the API reference shows for *target*'s version.

    9.1 and newer get :data:`PROVIDER_REGIONS_V1_PATH`. Anything else --
    9.0, no version, or a label that is not a product version (the
    fingerprint can record the ``2021-07-15`` API date) -- gets
    :data:`PROVIDER_REGIONS_PATH`, which 9.0 documents and 9.1 still
    answers. Same version rule as the 9.1 login pick in ``._auth``.
    """
    raw = resolve_target_version(target)
    if raw is None:
        return PROVIDER_REGIONS_PATH
    try:
        version = Version(raw)
    except InvalidVersion:
        return PROVIDER_REGIONS_PATH
    if _REGIONS_V1_MIN_VERSION <= version < _PRODUCT_VERSION_CEILING:
        return PROVIDER_REGIONS_V1_PATH
    return PROVIDER_REGIONS_PATH


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


#: Short text per group. The agent reads it through ``list_operation_groups``
#: to pick a group, so it names the plane and the ops each group holds.
#: ``register_typed_operation`` requires a non-empty string whenever
#: ``group_key`` is set. The group keys differ from the ingested browse-group
#: keys (``provider-orgs`` etc.) so the typed group rows never collide with an
#: ingested group's row on ``(product, version, impl_id, group_key)``.
VCFA_TYPED_WHEN_TO_USE_BY_GROUP: Final[dict[str, str]] = {
    "vcfa-provider-reads": (
        "Reads on the VCF Automation provider plane: the view of the system "
        "administrator across all tenants. List orgs (vcfa.provider.org.list) "
        "and regions (vcfa.provider.region.list), check that the provider login "
        "works (vcfa.provider.health), and look up rights "
        "(vcfa.provider.right.list) and roles (vcfa.provider.role.list) before "
        "you build a role or a user, and see which appliance feature flags are on "
        "(vcfa.provider.feature_flag.list). These ops use the provider login; a tenant "
        "token does not work here. All reads; nothing here changes anything."
    ),
    "vcfa-tenant-reads": (
        "Reads on the VCF Automation tenant plane, inside one tenant org: list "
        "projects (vcfa.tenant.project.list) and deployments "
        "(vcfa.tenant.deployment.list), read one deployment "
        "(vcfa.tenant.deployment.get), read the API versions "
        "(vcfa.tenant.about), or test only the tenant login "
        "(vcfa.tenant.login.test). Also list what VCF Automation knows from "
        "vCenter: vSphere networks (vcfa.tenant.fabric_network.list), computes "
        "(vcfa.tenant.fabric_compute.list), regions (vcfa.tenant.region.list) and "
        "cloud zones (vcfa.tenant.zone.list), and read a deploy request until it "
        "ends (vcfa.tenant.blueprint_request.get). These ops use the tenant login; "
        "the provider login does not work here. All reads."
    ),
    "vcfa-provider-writes": (
        "Changes on the VCF Automation provider plane that set up a tenant: "
        "create an org (vcfa.provider.org.create), a custom role "
        "(vcfa.provider.role.create; delete one with vcfa.provider.role.delete) "
        "and a local user "
        "(vcfa.provider.user.create), then create that user's API token and "
        "store it in Vault (vcfa.provider.api_token.create), or revoke it "
        "(vcfa.provider.api_token.revoke). Turn an appliance feature flag on or "
        "off (vcfa.provider.feature_flag.set), for example the one a VM Apps org "
        "needs. A person must approve each call. A create is safe to run twice: "
        "when the object exists, it answers 'unchanged'."
    ),
    "vcfa-tenant-writes": (
        "Changes on the VCF Automation tenant plane, to give a team its own "
        "project, network and first deployment: set the address settings and "
        "tags of a vSphere network (vcfa.tenant.fabric_network.update), create a "
        "network profile (vcfa.tenant.network_profile.create), create a project "
        "with a quota and a network rule (vcfa.tenant.project.create), deploy "
        "from a cloud template or only plan it "
        "(vcfa.tenant.blueprint_request.create), and delete a deployment "
        "(vcfa.tenant.deployment.delete). A person must approve each call. Safe "
        "to run twice: an object that exists answers 'unchanged'."
    ),
    "vcfa-tenant-images": (
        "Image names of the tenant org (image profiles), on the tenant plane. A cloud "
        "template asks for an image by name, for example 'RHEL 9'; the profile of each "
        "region maps the name to a vCenter template. Read the names and their templates "
        "(vcfa.tenant.image_profile.list) and the templates VCF Automation knows "
        "(vcfa.tenant.fabric_image.list), make it read new vCenter templates now "
        "(vcfa.tenant.cloud_account.enumerate_images), and add, re-point or remove one "
        "name (vcfa.tenant.image_profile.mapping.update; a person must approve it)."
    ),
    # Cloud templates and catalog sources: the same ops as on Aria Automation 8.
    "vcfa-tenant-templates": render_text(CONTENT_GROUP_TEXTS["templates"], "vcfa.tenant"),
    "vcfa-tenant-catalog": render_text(CONTENT_GROUP_TEXTS["catalog"], "vcfa.tenant"),
}


# ---------------------------------------------------------------------------
# Shared parameter-schema fragments
# ---------------------------------------------------------------------------

#: The cloudapi provider-plane pagination query params (``page`` /
#: ``pageSize``) shared by org.list + region.list.
_PROVIDER_PAGINATION_PROPERTIES: dict[str, Any] = {
    "page": {
        "type": "integer",
        "minimum": 1,
        "description": "The page number, starting at 1. Omit for page 1.",
    },
    "pageSize": {
        "type": "integer",
        "minimum": 1,
        "maximum": 128,
        "description": "Items per page (at most 128). Omit for the appliance default.",
    },
}

#: The tenant-plane OData query params shared by project.list +
#: deployment.list.
_TENANT_ODATA_PROPERTIES: dict[str, Any] = {
    "$filter": {
        "type": "string",
        "minLength": 1,
        "description": "Optional OData filter, for example \"name eq '<name>'\".",
    },
    "$orderby": {
        "type": "string",
        "minLength": 1,
        "description": "Optional OData sort order, for example 'name asc'.",
    },
    "$top": {
        "type": "integer",
        "minimum": 1,
        "description": "Optional number of items to return (page size).",
    },
    "$skip": {
        "type": "integer",
        "minimum": 0,
        "description": "Optional number of items to skip (offset).",
    },
}

_NO_PARAMS_WITH_EXAMPLE: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
    "examples": [{}],
}


# ---------------------------------------------------------------------------
# Provider plane
# ---------------------------------------------------------------------------

_PROVIDER_ORG_LIST = VcfaTypedOp(
    op_id="vcfa.provider.org.list",
    handler_attr="provider_org_list",
    plane="provider",
    path=PROVIDER_ORGS_PATH,
    summary="List the organizations (tenants) on the VCF Automation appliance.",
    description=(
        "Lists every org on the appliance with GET /cloudapi/1.0.0/orgs on the "
        "provider plane. Each tenant is one org. Supports 'page' and 'pageSize'."
    ),
    parameter_schema={
        "type": "object",
        "properties": dict(_PROVIDER_PAGINATION_PROPERTIES),
        "additionalProperties": False,
        "examples": [{"pageSize": 25}],
    },
    response_schema=cloudapi_page("orgs", ORG_FIELDS),
    group_key="vcfa-provider-reads",
    tags=("read-only", "vcfa", "provider"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=guidance(
        when_to_call=(
            "Call on the provider plane to list the orgs (tenants) on a VCF "
            "Automation appliance. Use 'page' and 'pageSize' on a large appliance."
        ),
        output_shape=(
            "{values: [Org], resultTotal, pageCount, page, pageSize}. Each Org has "
            "id, name, displayName, isEnabled, isClassicTenant and counts such as "
            "orgVdcCount and userCount."
        ),
        next_step=(
            "Use vcfa.tenant.project.list for reads inside one org, or "
            "vcfa.provider.region.list for the regions behind the orgs."
        ),
    ),
)

_PROVIDER_REGION_LIST = VcfaTypedOp(
    op_id="vcfa.provider.region.list",
    handler_attr="provider_region_list",
    plane="provider",
    path=PROVIDER_REGIONS_PATH,
    summary="List the regions on the VCF Automation appliance (provider plane).",
    description=(
        "Lists the regions on the provider plane: GET /cloudapi/vcf/regions on "
        "VCF Automation 9.0, GET /cloudapi/v1/regions on 9.1 and newer. "
        "A region groups compute, memory and networking under one NSX Manager, "
        "with its vSphere Supervisors. Supports 'page' and 'pageSize'."
    ),
    parameter_schema={
        "type": "object",
        "properties": dict(_PROVIDER_PAGINATION_PROPERTIES),
        "additionalProperties": False,
        "examples": [{"page": 1, "pageSize": 25}],
    },
    response_schema=cloudapi_page("regions", REGION_FIELDS),
    group_key="vcfa-provider-reads",
    tags=("read-only", "vcfa", "provider"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=guidance(
        when_to_call=(
            "Call on the provider plane to list the regions and the capacity each "
            "one offers, for example 'what compute does this appliance have'."
        ),
        output_shape=(
            "{values: [Region], resultTotal, pageCount, page, pageSize}. Each Region "
            "has id, name, status (READY, NOT_READY, ERROR, FAILED), nsxManager "
            "{name, id}, supervisors [{name, id}], cpuCapacityMHz, "
            "memoryCapacityMiB and storagePolicies."
        ),
        next_step=(
            "Check that the region you need has status READY, then read the "
            "tenant side with vcfa.tenant.project.list."
        ),
    ),
)

_PROVIDER_HEALTH = VcfaTypedOp(
    op_id="vcfa.provider.health",
    handler_attr="provider_health",
    plane="provider",
    # The authenticated leg rides the provider session on the org list --
    # a path verified to serve GET on both VCFA 9.0 and 9.1. The former
    # ``GET /cloudapi/1.0.0/site`` answers 405 on 9.1 (evoila/meho#3865).
    path=PROVIDER_ORGS_PATH,
    summary="Check that the VCF Automation provider login works, and read the API version.",
    description=(
        "Logs in on the provider plane and reads GET /cloudapi/1.0.0/orgs with "
        "pageSize=1, then reads GET /iaas/api/about without a login for the API "
        "versions. A failed provider login fails the op; a failed about read is "
        "reported in 'api'."
    ),
    parameter_schema=dict(_NO_PARAMS_WITH_EXAMPLE),
    response_schema={
        "type": "object",
        "properties": {
            "provider_plane": obj(
                "The result of the logged-in provider read.",
                {
                    "reachable": field("boolean", "Always true in a returned result."),
                    "authenticated": field("boolean", "Always true in a returned result."),
                    "check": field("string", "The request that was made."),
                    "org_count": field(["integer", "null"], "The number of orgs on the appliance."),
                },
            ),
            "api": obj(
                "The result of the about read, made without a login.",
                {
                    "reachable": field("boolean", "False when the about read failed."),
                    "latestApiVersion": field(
                        ["string", "null"], "The newest API version, as a date."
                    ),
                    "supportedApiVersions": {
                        "type": "array",
                        "description": "Every supported API version, as dates.",
                        "items": {"type": "string"},
                    },
                    "error": field("string", "Only when reachable is false: the error kind."),
                },
            ),
        },
        "required": ["provider_plane", "api"],
        "additionalProperties": True,
    },
    group_key="vcfa-provider-reads",
    tags=("read-only", "vcfa", "provider", "health"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=guidance(
        when_to_call=(
            "Call first, to check that the appliance answers a logged-in provider "
            "request and to read its API version, or as a health check after a "
            "deploy."
        ),
        output_shape=(
            "{provider_plane: {reachable, authenticated, check, org_count}, api: "
            "{reachable, latestApiVersion, supportedApiVersions} or {reachable: "
            "false, error}}. A bad credential or a down appliance is an op error, "
            "not a result."
        ),
        next_step=(
            "Go on with vcfa.provider.org.list or vcfa.provider.region.list; use "
            "vcfa.tenant.about to check the tenant plane."
        ),
    ),
)


# ---------------------------------------------------------------------------
# Tenant plane
# ---------------------------------------------------------------------------

_TENANT_PROJECT_LIST = VcfaTypedOp(
    op_id="vcfa.tenant.project.list",
    handler_attr="tenant_project_list",
    plane="tenant",
    path=TENANT_PROJECTS_PATH,
    summary="List the projects in the tenant organization (tenant plane).",
    description=(
        "Lists the projects of the tenant org with GET /iaas/api/projects. Every "
        "deployment belongs to exactly one project. Supports the OData params "
        "$filter, $orderby, $top and $skip."
    ),
    parameter_schema={
        "type": "object",
        "properties": dict(_TENANT_ODATA_PROPERTIES),
        "additionalProperties": False,
        "examples": [{"$filter": "name eq '<project-name>'", "$top": 25}],
    },
    response_schema=iaas_page("projects", IAAS_PROJECT_FIELDS),
    group_key="vcfa-tenant-reads",
    tags=("read-only", "vcfa", "tenant"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=guidance(
        when_to_call=(
            "Call on the tenant plane to list the projects of the tenant org. "
            "Narrow with $filter when the operator named a project."
        ),
        output_shape=(
            "{content: [Project], numberOfElements, totalElements}. Each Project "
            "has id, name, description, orgId, administrators / members / viewers "
            "[{email, type}], zones [{zoneId, priority}] and operationTimeout."
        ),
        next_step="Use a project id to find its deployments with vcfa.tenant.deployment.list.",
    ),
)

_TENANT_DEPLOYMENT_LIST = VcfaTypedOp(
    op_id="vcfa.tenant.deployment.list",
    handler_attr="tenant_deployment_list",
    plane="tenant",
    path=TENANT_DEPLOYMENTS_PATH,
    summary="List the deployments in the tenant organization (tenant plane).",
    description=(
        "Lists the deployments of the tenant org with GET /iaas/api/deployments: "
        "which deployments exist and in which project. Usually the first read "
        "when a tenant says 'my deployment did not come up'. Supports $filter, "
        "$orderby, $top and $skip."
    ),
    parameter_schema={
        "type": "object",
        "properties": dict(_TENANT_ODATA_PROPERTIES),
        "additionalProperties": False,
        "examples": [{"$filter": "name eq '<deployment-name>'", "$top": 25}],
    },
    response_schema=iaas_page("deployments", IAAS_DEPLOYMENT_FIELDS),
    group_key="vcfa-tenant-reads",
    tags=("read-only", "vcfa", "tenant"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=guidance(
        when_to_call=(
            "Call on the tenant plane to list the deployments of the tenant org, "
            "for example when a tenant asks which deployments exist or says one "
            "did not come up. Narrow with $filter, for example on the name."
        ),
        output_shape=(
            "{content: [Deployment], numberOfElements, totalElements}. The vendor "
            "IaaS spec lists id, name, description, projectId, orgId, owner, "
            "ownerType, createdAt and updatedAt for each Deployment; an appliance "
            "may add more fields, such as status."
        ),
        next_step=(
            "Read one deployment with vcfa.tenant.deployment.get, or match "
            "projectId against vcfa.tenant.project.list."
        ),
    ),
)

_TENANT_DEPLOYMENT_GET = VcfaTypedOp(
    op_id="vcfa.tenant.deployment.get",
    handler_attr="tenant_deployment_get",
    plane="tenant",
    path=TENANT_DEPLOYMENT_DETAIL_PATH,
    summary="Read one deployment by id in the tenant organization (tenant plane).",
    description=(
        "Reads one deployment with GET /iaas/api/deployments/{id} on the tenant "
        "plane: the next step after vcfa.tenant.deployment.list found a "
        "deployment to look at. Needs the deployment id."
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
        "examples": [{"id": "<deployment-id>"}],
    },
    response_schema=obj("One deployment.", dict(IAAS_DEPLOYMENT_FIELDS)),
    group_key="vcfa-tenant-reads",
    tags=("read-only", "vcfa", "tenant"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=guidance(
        when_to_call=(
            "Call on the tenant plane with a deployment id to read that one "
            "deployment, after vcfa.tenant.deployment.list found one to look at."
        ),
        output_shape=(
            "One Deployment, the same shape as one 'content' entry of the list op: "
            "id, name, description, projectId, orgId, owner, ownerType, createdAt, "
            "updatedAt, plus any fields the appliance adds."
        ),
        next_step="Match projectId against vcfa.tenant.project.list to find the project.",
        parameter_hints={"id": "The deployment id from vcfa.tenant.deployment.list."},
    ),
)

_TENANT_ABOUT = VcfaTypedOp(
    op_id="vcfa.tenant.about",
    handler_attr="tenant_about",
    plane="tenant",
    path=TENANT_ABOUT_PATH,
    summary="Read the API versions of the tenant plane (tenant plane).",
    description=(
        "Reads GET /iaas/api/about on the tenant plane: the supported API versions "
        "and the newest one. An answer shows that the tenant plane works; it is "
        "the tenant-side partner of vcfa.provider.health."
    ),
    parameter_schema=dict(_NO_PARAMS_WITH_EXAMPLE),
    response_schema=ABOUT_RESPONSE,
    group_key="vcfa-tenant-reads",
    tags=("read-only", "vcfa", "tenant", "health"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=guidance(
        when_to_call=(
            "Call to check that the tenant plane answers and to read its newest API "
            "version, before tenant reads or as a health check after a deploy."
        ),
        output_shape=(
            "{latestApiVersion, supportedApis: [{apiVersion, documentationLink, "
            "deprecationPolicy}]}. The versions are dates, not product versions."
        ),
        next_step="Go on with vcfa.tenant.project.list.",
    ),
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
