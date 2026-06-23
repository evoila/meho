# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Curated VCFA core-ops data tables -- the 8 groups + 11 ops + classifier rules.

Split out from :mod:`.core_ops` to keep the public module within the
file-size budget. The contents here are pure data + the classifier
function -- no I/O, no class state -- so they live cleanly outside the
public API surface module. The public module re-exports every symbol
here so callers continue to import from
``meho_backplane.connectors.vcf_automation.core_ops``.

See :mod:`.core_ops` for the design rationale (dual-plane shape,
operator-review semantics, audit-log-driven exclusion). This module
carries the literal data the helper drives.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from meho_backplane.connectors.vcf_automation._routing import Plane

__all__ = [
    "VCFA_CONNECTOR_ID",
    "VCFA_CORE_GROUPS",
    "VCFA_CORE_OPS",
    "VCFA_IMPL_ID",
    "VCFA_PATH_RULES",
    "VCFA_PRODUCT",
    "VCFA_VERSION",
    "VcfaCoreGroup",
    "VcfaCoreOp",
    "classify_vcfa_op",
]


#: Endpoint-descriptor product key -- what
#: :func:`~meho_backplane.operations._lookup.parse_connector_id` extracts
#: from ``VCFA_CONNECTOR_ID = "vcfa-rest-9.0"`` (``head.split("-", 1)[0]``
#: where head is ``"vcfa-rest"``).
#:
#: Since #1814 (Initiative #1810) this equals
#: :attr:`~meho_backplane.connectors.vcf_automation.VcfAutomationConnector.product`
#: (``"vcfa"``) — the v2 connector registry key and target-product
#: resolver. All ``endpoint_descriptor`` and ``operation_group`` rows for
#: this connector triple carry ``product="vcfa"``; operators pass
#: ``--product vcfa`` when driving ``meho connector ingest``. Same short,
#: dispatch-canonical token the SDDC Manager precedent uses (``"sddc"``).
VCFA_PRODUCT: Final[str] = "vcfa"
VCFA_VERSION: Final[str] = "9.0"
VCFA_IMPL_ID: Final[str] = "vcfa-rest"

#: Connector-id slug the G0.6 dispatcher's ``parse_connector_id``
#: round-trips back to the triple above: ``"vcfa-rest-9.0"``.
VCFA_CONNECTOR_ID: Final[str] = f"{VCFA_IMPL_ID}-{VCFA_VERSION}"


@dataclass(frozen=True, slots=True)
class VcfaCoreGroup:
    """One curated operator-review entry for a VCFA operation group.

    ``group_key`` is the slug :func:`classify_vcfa_op` emits.
    ``plane`` records which auth plane the group's ops live on
    (``"provider"`` or ``"tenant"``); the value mirrors
    :func:`~meho_backplane.connectors.vcf_automation._routing.plane_for_path`'s
    return type so cross-checks are static.  ``name`` is the
    operator-readable label ``meho connector review`` renders.
    ``when_to_use`` is the agent-facing hint
    :func:`list_operation_groups` returns verbatim; every entry is a
    single complete sentence **naming its plane** so the agent's
    group-selection step has unambiguous guidance and never collapses
    a tenant question onto a provider-only group (or vice versa).
    """

    group_key: str
    plane: Plane
    name: str
    when_to_use: str


@dataclass(frozen=True, slots=True)
class VcfaCoreOp:
    """One curated operator-review entry for a VCFA operation.

    ``op_id`` follows the ``METHOD:path`` shape every
    ``source_kind='ingested'`` row uses; the path matches an entry in
    ``vcf-automation-9.0/cloudapi.yaml`` (provider plane) or
    ``vcf-automation-9.0/iaas.yaml`` (tenant plane). The ``plane``
    field mirrors the group's plane and is asserted at module-import
    time against
    :func:`~meho_backplane.connectors.vcf_automation._routing.plane_for_path`
    by the public :mod:`.core_ops` module -- a typo that drifts a
    tenant op into a provider group (or vice versa) fails import
    rather than surfacing as a misrouted 401 in production.

    ``llm_instructions`` is the per-op JSON blob the meta-tools
    inline verbatim when the op surfaces. The shape (``when_to_call``
    / ``output_shape`` / ``next_step``) mirrors the typed-connector
    convention from :mod:`meho_backplane.connectors.bind9.ops_zone`
    and the prior core-ops modules.
    """

    op_id: str
    group_key: str
    plane: Plane
    llm_instructions: dict[str, object]


#: Path-prefix -> group_key classifier rules for VCFA.
#:
#: First match wins. The provider and tenant planes never share a
#: path family (``/iaas/api/`` is uniquely tenant), so ordering is
#: not load-bearing in the way Harbor's nested project hierarchy is
#: -- but the tenant rules are listed first defensively so a future
#: provider-side ``/iaas`` (unlikely, but possible) wouldn't shadow
#: them.
VCFA_PATH_RULES: Final[tuple[tuple[str, str], ...]] = (
    # Tenant plane -- /iaas/api/* family.
    ("/iaas/api/about", "tenant-about"),
    ("/iaas/api/projects", "tenant-projects"),
    ("/iaas/api/deployments", "tenant-deployments"),
    ("/iaas/api/blueprints", "tenant-blueprints"),
    # Provider plane -- /cloudapi/1.0.0/* family.
    ("/cloudapi/1.0.0/site", "provider-site"),
    ("/cloudapi/1.0.0/orgs", "provider-orgs"),
    ("/cloudapi/1.0.0/regions", "provider-regions"),
    ("/cloudapi/1.0.0/users", "provider-users"),
)


def classify_vcfa_op(op_id: str) -> str:
    """Return the curated ``group_key`` for a VCFA op_id, or ``"none"``.

    ``op_id`` is the ``METHOD:/path`` form ingested rows carry; the
    helper strips the verb (only ``GET`` is in the read core), then
    matches the path against :data:`VCFA_PATH_RULES` in order. Returns
    ``"none"`` for paths outside the curated families; those rows are
    un-curated and stay ``is_enabled=False`` after curation runs.

    Malformed op_ids (no ``:`` separator, non-``GET`` method) map to
    ``"none"`` defensively -- the v0.5 read core is GET-only.
    """
    try:
        method, path = op_id.split(":", 1)
    except ValueError:
        return "none"
    if method != "GET":
        return "none"
    for prefix, group_key in VCFA_PATH_RULES:
        if path.startswith(prefix):
            return group_key
    return "none"


def _instructions(
    *,
    when_to_call: str,
    output_shape: str,
    next_step: str,
) -> dict[str, object]:
    """Build the per-op ``llm_instructions`` blob with the canonical keys.

    Same three-field shape :mod:`meho_backplane.connectors.nsx.core_ops`
    and :mod:`meho_backplane.connectors.harbor.core_ops` use so an
    agent crossing connector boundaries sees a stable convention.
    """
    return {
        "when_to_call": when_to_call,
        "output_shape": output_shape,
        "next_step": next_step,
    }


#: Operator-reviewed ``when_to_use`` hints for the 8 VCFA groups
#: (4 provider + 4 tenant). Every hint names its plane explicitly
#: so the agent's group-selection step routes correctly across the
#: dual-plane surface.
VCFA_CORE_GROUPS: Final[tuple[VcfaCoreGroup, ...]] = (
    # ----- Provider plane (4 groups, 6 ops) -----
    VcfaCoreGroup(
        group_key="provider-site",
        plane="provider",
        name="VCFA Provider (site)",
        when_to_use=(
            "Use this group on the VCFA **provider plane** to read appliance-level "
            "site identity: site name, configured organization count, and VCFA "
            "appliance version. The probe surface the agent calls before any "
            "heavier provider read or when confirming which VCFA instance the "
            "target points at. Provider-plane ops authenticate with the "
            "``admin@System`` (or equivalent) Basic-auth session and never "
            "succeed against the tenant token."
        ),
    ),
    VcfaCoreGroup(
        group_key="provider-orgs",
        plane="provider",
        name="VCFA Provider Organizations",
        when_to_use=(
            "Use this group on the VCFA **provider plane** to list or inspect "
            "organizations on the appliance. The provider-plane org surface is "
            "the cross-tenant view the system administrator sees -- every tenant "
            "appears here. For per-tenant project / deployment / blueprint "
            "reads, switch to the tenant-plane groups (``tenant-projects``, "
            "``tenant-deployments``, ``tenant-blueprints``)."
        ),
    ),
    VcfaCoreGroup(
        group_key="provider-regions",
        plane="provider",
        name="VCFA Provider Regions",
        when_to_use=(
            "Use this group on the VCFA **provider plane** to list or inspect "
            "regions -- the VCFA 9 evolution of the vCloud-Director provider VDC "
            "concept. Each region groups compute, memory, and networking "
            "resources under a single NSX domain, typically backed by one or "
            "more VCF workload domains. Use to answer 'what compute capacity "
            "does this VCFA appliance offer' or 'which region backs tenant X'."
        ),
    ),
    VcfaCoreGroup(
        group_key="provider-users",
        plane="provider",
        name="VCFA Provider Users",
        when_to_use=(
            "Use this group on the VCFA **provider plane** to list system-scope "
            "users (the system organization's identity entries plus any "
            "cross-org provider-scope users). Use when auditing who has "
            "provider-level access. Tenant-scope users (per-org members) are "
            "exposed through the per-org user endpoints which are not in the "
            "v0.5 read core."
        ),
    ),
    # ----- Tenant plane (4 groups, 5 ops) -----
    VcfaCoreGroup(
        group_key="tenant-about",
        plane="tenant",
        name="VCFA Tenant (about)",
        when_to_use=(
            "Use this group on the VCFA **tenant plane** to read the IaaS API "
            "self-describe surface -- supported API versions and the latest "
            "tenant API version. The probe surface the agent calls before any "
            "tenant catalog read or when confirming tenant-plane reachability. "
            "Tenant-plane ops authenticate with the tenant org login (``POST "
            "/iaas/api/login``) and never succeed against the provider JWT."
        ),
    ),
    VcfaCoreGroup(
        group_key="tenant-projects",
        plane="tenant",
        name="VCFA Tenant Projects",
        when_to_use=(
            "Use this group on the VCFA **tenant plane** to list projects "
            "within the tenant organization. Projects are the deployment-scoping "
            "construct: every deployment belongs to exactly one project, and "
            "blueprint access controls reference project membership. Use to "
            "answer 'what projects exist in this org' or to pick the "
            "project_id filter for a follow-up deployment list."
        ),
    ),
    VcfaCoreGroup(
        group_key="tenant-deployments",
        plane="tenant",
        name="VCFA Tenant Deployments",
        when_to_use=(
            "Use this group on the VCFA **tenant plane** to list or inspect "
            "catalog deployments -- the running instances of blueprints. The "
            "primary tenant-side answer surface for 'what workloads are "
            "deployed', 'what is the status of deployment X', or 'who owns "
            "deployment Y'. Deployment list is the largest payload on the "
            "tenant surface; the dispatcher's JSONFlux seam wraps oversized "
            "responses in a ResultHandle with a bounded inline sample plus a "
            "``fetch_more`` envelope. Re-call with an OData ``$filter`` to "
            "scope down rather than expecting a handle read-back tool."
        ),
    ),
    VcfaCoreGroup(
        group_key="tenant-blueprints",
        plane="tenant",
        name="VCFA Tenant Blueprints",
        when_to_use=(
            "Use this group on the VCFA **tenant plane** to list catalog "
            "blueprints -- the templates deployments instantiate. Use to answer "
            "'what blueprints can this tenant deploy' or to look up a "
            "blueprint id before cross-referencing it on an existing "
            "deployment's blueprintId field."
        ),
    ),
)


#: The 11 curated read-only VCFA core ops (6 provider + 5 tenant).
#: Paths cross-checked against the VCF Automation 9.0 API surface:
#: the cloudapi family at
#: https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-9-0-and-later/9-0/administration-sdks-cli-and-tools/about-the-vcf-automation-api.html
#: and the tenant-plane IaaS family at
#: https://developer.broadcom.com/xapis/vm-apps-org-provisioning-service/latest/.
VCFA_CORE_OPS: Final[tuple[VcfaCoreOp, ...]] = (
    # ----- Provider plane (6 ops) -----
    VcfaCoreOp(
        op_id="GET:/cloudapi/1.0.0/site",
        group_key="provider-site",
        plane="provider",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to read VCFA appliance site identity on the provider "
                "plane: site name, configured organization count, and VCFA "
                "appliance version. Useful as a pre-flight probe before "
                "heavier provider reads or to confirm which VCFA instance the "
                "target points at."
            ),
            output_shape=(
                "Object with id, name, description, restName, and product "
                "version fields plus a links collection. The product version "
                "string identifies the VCFA appliance build."
            ),
            next_step=(
                "Confirm the product version is in the supported range, then "
                "proceed to vcfa.provider.org.list for the org inventory or "
                "vcfa.provider.vdc.list for the region inventory."
            ),
        ),
    ),
    VcfaCoreOp(
        op_id="GET:/cloudapi/1.0.0/orgs",
        group_key="provider-orgs",
        plane="provider",
        llm_instructions=_instructions(
            when_to_call=(
                "Call on the provider plane to list organizations on this "
                "VCFA appliance. Returns the cross-tenant view (every tenant "
                "appears here) -- the system administrator's inventory entry "
                "point. Supports pagination via 'page' and 'pageSize' query "
                "parameters."
            ),
            output_shape=(
                "Object with a 'values' array of organization entries; each "
                "entry carries id, name, displayName, description, isEnabled, "
                "and orgVdcCount. The 'resultTotal' field reports the global "
                "count for pagination."
            ),
            next_step=(
                "Pick an org id for vcfa.provider.org.get to read its full "
                "detail, or switch planes to tenant-side ops for per-org "
                "project / deployment / blueprint reads."
            ),
        ),
    ),
    VcfaCoreOp(
        op_id="GET:/cloudapi/1.0.0/orgs/{id}",
        group_key="provider-orgs",
        plane="provider",
        llm_instructions=_instructions(
            when_to_call=(
                "Call on the provider plane to read the full detail of one "
                "VCFA organization by id. Returns the configuration the "
                "organization-list endpoint summarises, plus quota / policy / "
                "branding fields the list omits. Requires an org id obtained "
                "from vcfa.provider.org.list."
            ),
            output_shape=(
                "Organization object with id, name, displayName, description, "
                "isEnabled, orgVdcCount, catalogCount, vappCount, "
                "runningVMCount, userCount, diskCount, and a settings "
                "sub-object covering quotas + policies."
            ),
            next_step=(
                "If counts indicate the org has active workloads, switch "
                "planes to vcfa.tenant.deployment.list (authenticated against "
                "the same org's tenant token) for the catalog view."
            ),
        ),
    ),
    VcfaCoreOp(
        op_id="GET:/cloudapi/1.0.0/regions",
        group_key="provider-regions",
        plane="provider",
        llm_instructions=_instructions(
            when_to_call=(
                "Call on the provider plane to list VCFA regions -- the VCFA 9 "
                "evolution of the vCloud-Director provider VDC concept. Each "
                "region maps to one NSX domain plus a collection of "
                "supervisors backed by one or more VCF workload domains. Use "
                "to answer 'what compute capacity does this VCFA appliance "
                "offer'."
            ),
            output_shape=(
                "Object with a 'values' array of region entries; each entry "
                "carries id, name, description, nsxManager, supervisors, and "
                "isEnabled. The 'resultTotal' field reports the global count "
                "for pagination."
            ),
            next_step=(
                "Pick a region id for vcfa.provider.vdc.get for the full "
                "detail (capacity counters, configured storage policies)."
            ),
        ),
    ),
    VcfaCoreOp(
        op_id="GET:/cloudapi/1.0.0/regions/{id}",
        group_key="provider-regions",
        plane="provider",
        llm_instructions=_instructions(
            when_to_call=(
                "Call on the provider plane to read the full detail of one "
                "VCFA region by id. Returns capacity counters, configured "
                "storage policies, and the underlying NSX-domain / "
                "workload-domain backing. Requires a region id obtained from "
                "vcfa.provider.vdc.list."
            ),
            output_shape=(
                "Region object with id, name, description, isEnabled, "
                "nsxManager, supervisors[], storagePolicies[], and capacity "
                "fields (totalCpuMhz, totalMemoryMB, allocatedCpuMhz, "
                "allocatedMemoryMB)."
            ),
            next_step=(
                "If allocated/total ratios indicate capacity pressure, "
                "surface the region id and ratio to the operator. Otherwise "
                "cross-reference the region id against tenant-plane "
                "deployment reads to map workloads to regions."
            ),
        ),
    ),
    VcfaCoreOp(
        op_id="GET:/cloudapi/1.0.0/users",
        group_key="provider-users",
        plane="provider",
        llm_instructions=_instructions(
            when_to_call=(
                "Call on the provider plane to list users at the provider "
                "scope: the system organization's identity entries plus any "
                "cross-org provider-scope users. Use when auditing who has "
                "provider-level access. Per-tenant users are exposed through "
                "the per-org user endpoints which are not in the v0.5 read "
                "core."
            ),
            output_shape=(
                "Object with a 'values' array of user entries; each entry "
                "carries id, username, fullName, email, roleEntityRefs, and "
                "isEnabled."
            ),
            next_step=(
                "Surface users with isEnabled=false or unexpected role refs "
                "to the operator. For per-tenant identity audits, the "
                "tenant-side org-user endpoints (out of v0.5 scope) are the "
                "next surface."
            ),
        ),
    ),
    # ----- Tenant plane (5 ops) -----
    VcfaCoreOp(
        op_id="GET:/iaas/api/about",
        group_key="tenant-about",
        plane="tenant",
        llm_instructions=_instructions(
            when_to_call=(
                "Call on the tenant plane to read the IaaS API self-describe "
                "surface: supportedApis list and latestApiVersion. Useful as "
                "a probe before any tenant catalog read or to confirm "
                "tenant-plane reachability and version negotiation."
            ),
            output_shape=(
                "Object with supportedApis[] (each carrying apiVersion + a "
                "documentation URL) and latestApiVersion (string)."
            ),
            next_step=(
                "Confirm latestApiVersion is in the connector's supported "
                "range, then proceed to vcfa.tenant.project.list or "
                "vcfa.tenant.deployment.list for the catalog view."
            ),
        ),
    ),
    VcfaCoreOp(
        op_id="GET:/iaas/api/projects",
        group_key="tenant-projects",
        plane="tenant",
        llm_instructions=_instructions(
            when_to_call=(
                "Call on the tenant plane to list projects within the tenant "
                "organization. Projects are the deployment-scoping construct "
                "-- every deployment belongs to exactly one project. Supports "
                "OData-like filtering via $filter, $orderby, $top, $skip."
            ),
            output_shape=(
                "Object with a 'content' array of project entries; each "
                "entry carries id, name, description, organizationId, "
                "administrators[], members[], and operationTimeout. Page "
                "metadata in totalElements + totalPages."
            ),
            next_step=(
                "Pick a project id as the $filter argument for "
                "vcfa.tenant.deployment.list (filter syntax: "
                "$filter=projectId eq '<id>') to scope the deployment view "
                "to one project."
            ),
        ),
    ),
    VcfaCoreOp(
        op_id="GET:/iaas/api/deployments",
        group_key="tenant-deployments",
        plane="tenant",
        llm_instructions=_instructions(
            when_to_call=(
                "Call on the tenant plane to list catalog deployments (the "
                "running instances of blueprints). The largest payload on "
                "the tenant surface -- large tenants return hundreds of "
                "deployments. The dispatcher's JSONFlux seam wraps oversized "
                "responses in a ResultHandle with a bounded inline sample plus "
                "a ``fetch_more`` envelope; re-call with an OData ``$filter`` "
                "to scope down ($filter=projectId eq '<id>' is the canonical "
                "scope-down) rather than expecting a handle read-back tool."
            ),
            output_shape=(
                "Object with a 'content' array of deployment entries; each "
                "entry carries id, name, description, status, projectId, "
                "blueprintId, ownedBy, createdAt, lastUpdatedAt, and a "
                "resources[] summary. Page metadata in totalElements + "
                "totalPages."
            ),
            next_step=(
                "Pick a deployment id for vcfa.tenant.deployment.get for the "
                "full detail (full resources, expense, request log). Cross-"
                "reference blueprintId against vcfa.tenant.blueprint.list to "
                "name the template."
            ),
        ),
    ),
    VcfaCoreOp(
        op_id="GET:/iaas/api/deployments/{id}",
        group_key="tenant-deployments",
        plane="tenant",
        llm_instructions=_instructions(
            when_to_call=(
                "Call on the tenant plane to read the full detail of one "
                "tenant deployment by id. Returns the full resources[] tree "
                "(each backing VM, network, storage entry), expense, and "
                "request log. Requires a deployment id obtained from "
                "vcfa.tenant.deployment.list."
            ),
            output_shape=(
                "Deployment object with id, name, description, status, "
                "projectId, blueprintId, ownedBy, createdAt, lastUpdatedAt, "
                "resources[] (full backing-resource tree), expense, "
                "lastRequestId, and inputs (the deployment-time parameter "
                "values)."
            ),
            next_step=(
                "Surface the status field to the operator; if status is "
                "FAILED or in-progress, the lastRequestId points at the "
                "request log entry with the failure detail. For a workload "
                "map, walk resources[] for the backing VM ids and "
                "cross-reference against vSphere / NSX inventory."
            ),
        ),
    ),
    VcfaCoreOp(
        op_id="GET:/iaas/api/blueprints",
        group_key="tenant-blueprints",
        plane="tenant",
        llm_instructions=_instructions(
            when_to_call=(
                "Call on the tenant plane to list catalog blueprints -- the "
                "templates deployments instantiate. Use to answer 'what "
                "blueprints can this tenant deploy' or to look up a "
                "blueprint id before cross-referencing it on an existing "
                "deployment's blueprintId field. Supports OData filters."
            ),
            output_shape=(
                "Object with a 'content' array of blueprint entries; each "
                "entry carries id, name, description, projectId, version, "
                "status (DRAFT / VERSIONED / RELEASED), updatedAt, and "
                "content (a summary; the full blueprint YAML lives behind a "
                "separate get-blueprint endpoint not in the v0.5 read core)."
            ),
            next_step=(
                "Cross-reference blueprint id against the blueprintId field "
                "on vcfa.tenant.deployment.list results to identify which "
                "deployments instantiated which blueprint."
            ),
        ),
    ),
)
