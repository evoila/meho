# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Per-op ``llm_instructions`` blobs for the ``vra8`` typed read core.

Split out of :mod:`~meho_backplane.connectors.vra8.typed_reads` (which holds the
op bodies) to keep each module focused, the ``fleet-lcm`` #3047 / ``vcd`` #3057
layout. Consumed by :mod:`meho_backplane.connectors.vra8.typed_ops`, which keys
each op's metadata row to :data:`VRA8_READ_LLM_INSTRUCTIONS`.

The canonical three-key shape (``when_to_call`` / ``output_shape`` / ``next_step``)
is the same convention the harbor / fleet-lcm / vcd read cores use, so an agent
crossing connector boundaries sees a stable structure. The framing is migration
inventory: these reads enumerate a *source* vRA 8 estate an operator is bringing
onto the platform.

**Pagination.** The vRA 8 list surfaces are server-paged (a ``{content: [],
totalElements, numberOfElements, ...}`` wrapper). Each list op's ``output_shape``
tells the agent to check ``totalElements`` and page via ``$top`` / ``$skip`` — the
default call returns the first page only, but the wrapper self-describes the total,
so it is never a silent truncation.
"""

from __future__ import annotations

__all__ = ["VRA8_READ_LLM_INSTRUCTIONS"]


_PROJECT_LIST_INSTRUCTIONS: dict[str, object] = {
    "when_to_call": (
        "Call first when inventorying a vRealize Automation 8 source estate — "
        "projects are vRA 8's tenancy/quota boundary (members, zones, resource "
        "constraints). Use to answer 'which projects/teams live on this vRA' before "
        "drilling into their deployments and blueprints."
    ),
    "output_shape": (
        "IaaS paged wrapper {content: [Project, ...], totalElements, "
        "numberOfElements}. Each Project carries id, name, description, orgId, "
        "administrators/members/viewers, zones (cloud zones), constraints, and "
        "customProperties. Paged — check totalElements and page with $top / $skip."
    ),
    "next_step": (
        "Filter vcfa.vra8.deployment.list by projectId to enumerate a project's "
        "running workloads, or vcfa.vra8.blueprint.list by the project to see its "
        "IaC definitions."
    ),
}

_DEPLOYMENT_LIST_INSTRUCTIONS: dict[str, object] = {
    "when_to_call": (
        "Call to list the deployments — vRA 8's running workloads (the Service "
        "Broker view). This is usually the highest-value migration read: what is "
        "actually deployed, in which project, from which blueprint/catalog item. "
        "Pass $top to raise the page size for a whole-estate count."
    ),
    "output_shape": (
        "Service Broker paged wrapper {content: [Deployment, ...], totalElements, "
        "totalPages, number, size}. Each Deployment carries id, name, projectId, "
        "blueprintId/blueprintVersion, catalogItemId, status, createdAt/By, "
        "ownedBy, leaseExpireAt, and (with expand) resources / lastRequest / "
        "inprogressRequests. A large list is reduced to a JSONFlux handle — narrow "
        "with result_query / result_aggregate (e.g. count by projectId or status)."
    ),
    "next_step": (
        "Drill into one deployment via vcfa.vra8.deployment.get for its resources "
        "and request state (lastRequest / inprogressRequests — the pre-cutover "
        "quiescence signal). Map blueprintId back to vcfa.vra8.blueprint.list for "
        "the definition to recreate."
    ),
}

_DEPLOYMENT_GET_INSTRUCTIONS: dict[str, object] = {
    "when_to_call": (
        "Call with a deployment_id (from vcfa.vra8.deployment.list) to read one "
        "deployment's full detail — its resources and its request state. vRA 8 has "
        "no top-level list-all-requests endpoint, so this is how you read a "
        "deployment's lastRequest / inprogressRequests: the signal for whether an "
        "operation is in flight before a migration cutover."
    ),
    "output_shape": (
        "A single Deployment object: id, name, projectId, blueprintId, "
        "catalogItemId, status, expense, inputs, resources[] (the provisioned "
        "machines/networks/etc.), lastRequest (the most recent action + its "
        "status), and inprogressRequests[] (empty when quiescent)."
    ),
    "next_step": (
        "A non-empty inprogressRequests means the deployment is not quiescent — "
        "hold the cutover. Correlate resources[] against the target-side capacity "
        "plan, and blueprintId with vcfa.vra8.blueprint.list."
    ),
}

_BLUEPRINT_LIST_INSTRUCTIONS: dict[str, object] = {
    "when_to_call": (
        "Call to list the Cloud Assembly blueprints — the infrastructure-as-code "
        "definitions (YAML) a migration must recreate on the target. Use to "
        "inventory what templates exist, which are released, and which project owns "
        "each."
    ),
    "output_shape": (
        "Blueprint paged wrapper {content: [Blueprint, ...], totalElements}. Each "
        "Blueprint carries id, name, projectId/projectName, status, content (the "
        "blueprint YAML), totalVersions / totalReleasedVersions, valid, and "
        "contentSourceId/Type (a git-backed source, if any). Paged — page with "
        "$top / $skip."
    ),
    "next_step": (
        "Flag git-backed blueprints (contentSourceType) as external dependencies "
        "the migration must re-point. Correlate a blueprint's id with "
        "vcfa.vra8.deployment.list (blueprintId) to see which are in active use."
    ),
}

_CATALOG_ITEM_LIST_INSTRUCTIONS: dict[str, object] = {
    "when_to_call": (
        "Call to list the Service Broker catalog items (admin view — every project, "
        "not just the caller's) — the self-service offerings a migration must "
        "republish on the target. Use to inventory what is offered and which "
        "projects each item is shared to."
    ),
    "output_shape": (
        "Service Broker paged wrapper {content: [CatalogItem, ...], totalElements}. "
        "Each CatalogItem carries id, name, type (the source kind — blueprint / "
        "workflow / etc.), sourceId/sourceName, projectIds/projects (entitlement "
        "scope), iconId, and createdAt/By. Paged — page with $top / $skip."
    ),
    "next_step": (
        "Map an item's sourceId back to its backing definition "
        "(vcfa.vra8.blueprint.list for blueprint-typed items), and its projectIds "
        "to vcfa.vra8.project.list to see who can request it."
    ),
}

_ABOUT_INSTRUCTIONS: dict[str, object] = {
    "when_to_call": (
        "Call to read the vRA 8 appliance's IaaS API capabilities — the supported "
        "API versions. Use to confirm the source is a vRA 8.x surface and gauge its "
        "generation before the deeper inventory reads (the connector's fingerprint "
        "reads the same endpoint for reachability)."
    ),
    "output_shape": (
        "{latestApiVersion, supportedApis: [{apiVersion, deprecationPolicy, "
        "documentationLink}, ...]}. Note: latestApiVersion is an API *date* label "
        "(e.g. 2021-07-15), not the product build (8.11 vs 8.18) — the product "
        "version is carried on the target, not this response."
    ),
    "next_step": (
        "Proceed to vcfa.vra8.project.list and vcfa.vra8.deployment.list for the "
        "actual estate inventory."
    ),
}


#: Per-op ``llm_instructions`` keyed by the dot-form op id, consumed by
#: :mod:`meho_backplane.connectors.vra8.typed_ops`.
VRA8_READ_LLM_INSTRUCTIONS: dict[str, dict[str, object]] = {
    "vcfa.vra8.project.list": _PROJECT_LIST_INSTRUCTIONS,
    "vcfa.vra8.deployment.list": _DEPLOYMENT_LIST_INSTRUCTIONS,
    "vcfa.vra8.deployment.get": _DEPLOYMENT_GET_INSTRUCTIONS,
    "vcfa.vra8.blueprint.list": _BLUEPRINT_LIST_INSTRUCTIONS,
    "vcfa.vra8.catalog-item.list": _CATALOG_ITEM_LIST_INSTRUCTIONS,
    "vcfa.vra8.about": _ABOUT_INSTRUCTIONS,
}
