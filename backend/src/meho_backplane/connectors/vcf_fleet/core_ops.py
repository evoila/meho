# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""VCF Fleet 9.0 (vRSLCM-derived) read-only v0.5 core — curated subset.

This module names the **8 read-only Fleet operations** the G3.6 v0.5 ship
enables out of the much larger vRSLCM LCM REST corpus the G0.7
spec-ingestion pipeline lands under ``connector_id="fleet-rest-9.0"``.
The curation is two-layered:

* :data:`FLEET_CORE_GROUPS` — the operator-reviewed ``when_to_use``
  hint per LLM-grouping pass output group. Each entry's ``group_key``
  is the deterministic slug :func:`classify_fleet_op` assigns to Fleet
  ops; the ``when_to_use`` is what the agent reads verbatim through
  :func:`~meho_backplane.operations.meta_tools.list_operation_groups`
  to pick a group to search within.
* :data:`FLEET_CORE_OPS` — the 8 ``EndpointDescriptor.op_id`` strings
  that flip to ``is_enabled=True`` at operator-review time, paired
  with the per-op ``llm_instructions`` blob the agent inlines into
  the reasoning context when it sees the op in
  :func:`~meho_backplane.operations.meta_tools.search_operations`
  hits. Every other op under the same connector triple stays
  ``is_enabled=False`` (the G0.7 ingestion default for
  ``source_kind='ingested'`` rows).

Per Initiative #369 and CLAUDE.md postulates 1-2, Fleet is **fully
generic-ingested**: the underlying ops are not registered in code,
they live in the ``endpoint_descriptor`` table. This module only
carries the **operator-review metadata** the substrate uses at the
review step — the actual curation is applied through
:func:`apply_fleet_core_curation` against an existing ingested
connector.

The 8 ops (paths cross-checked against vRSLCM REST API 1.3.0 at
https://developer.broadcom.com/xapis/vrealize-suite-lifecycle-manager/latest/):

1. ``GET:/lcm/lcops/api/v2/about`` — ``fleet.about`` — vRSLCM
   appliance identity. **Note:** Fleet's first-party ``/about``
   endpoint returns HTTP 500 in VCF 9.0 builds (see the
   :class:`~meho_backplane.connectors.vcf_fleet.connector.VcfFleetConnector`
   docstring for the known-issue inventory). The op is curated for
   parity with the spec; the ``llm_instructions.next_step`` tells
   the agent to fall back to ``fleet.datacenter.list`` as the
   reachability probe in 9.0.
2. ``GET:/lcm/lcops/api/v2/datacenters`` — ``fleet.datacenter.list``
   — datacenter inventory. The wrapper-verified probe endpoint;
   guaranteed to work in 9.0 because the connector's own probe uses
   it.
3. ``GET:/lcm/lcops/api/v2/datacenters/{dataCenterVmid}/vcenters``
   — ``fleet.vcenter.list`` — vCenters registered under a datacenter.
4. ``GET:/lcm/lcops/api/v2/environments`` — ``fleet.environment.list``
   — environment inventory (the primary Fleet entry point — every
   product deploy lives under an environment).
5. ``GET:/lcm/lcops/api/v2/environments/{environmentId}`` —
   ``fleet.environment.get`` — per-environment detail including
   products, status, and metadata.
6. ``GET:/lcm/lcops/api/v2/environments/{environmentId}/products`` —
   ``fleet.product.list`` — products deployed under an environment.
7. ``GET:/lcm/request/api/v2/requests`` — ``fleet.request.list`` —
   lifecycle requests (deploy / patch / upgrade) the appliance has
   processed; the LCM workflow-status surface.
8. ``GET:/lcm/request/api/v2/requests/{requestId}`` —
   ``fleet.request.get`` — per-request detail including state
   (``INPROGRESS`` / ``COMPLETED`` / ``FAILED``), output map, and
   error cause.

Path families and group_keys
-----------------------------

vRSLCM's LCM REST surface splits into two top-level path families:

* ``/lcm/lcops/...`` — the "LCM operations" surface covering
  identity (``/about``), datacenters, vCenters, environments, and
  products. Carries six of the eight curated ops.
* ``/lcm/request/...`` — the "request" surface covering lifecycle
  workflow status. Carries two of the eight curated ops.

The classifier in :data:`FLEET_PATH_RULES` reflects that split.
Order is load-bearing: deeper nested paths must precede their
parent prefixes (vcenters before datacenters, products before
environments) so the ``startswith`` loop terminates at the right
group.

Curation application
--------------------

:func:`apply_fleet_core_curation` is the operator-review-time
substrate call that makes exactly the 8 curated ops dispatchable.
Mirrors :func:`~meho_backplane.connectors.harbor.core_ops.apply_harbor_core_curation`
verbatim, threading the "enable group but pin non-core ops
disabled" needle via the audit-log-driven operator-override
exclusion.

JSONFlux handle expectation
---------------------------

``fleet.environment.list`` and ``fleet.request.list`` are the
candidates for JSONFlux handle returns on busy appliances — a
single LCM appliance commonly manages dozens of environments and
thousands of historical requests. The acceptance criterion "the
longest list op returns a JSONFlux handle" is verified in the
canary's substrate-level dispatch tests rather than against a
live appliance; real JSONFlux reduction is out of scope per
Goal #214.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final
from uuid import UUID

import structlog

from meho_backplane.operations.ingest.service import ReviewService

__all__ = [
    "FLEET_CONNECTOR_ID",
    "FLEET_CORE_GROUPS",
    "FLEET_CORE_OPS",
    "FLEET_IMPL_ID",
    "FLEET_PATH_RULES",
    "FLEET_PRODUCT",
    "FLEET_VERSION",
    "FleetCoreGroup",
    "FleetCoreOp",
    "apply_fleet_core_curation",
    "classify_fleet_op",
]

_log = structlog.get_logger(__name__)

#: Endpoint-descriptor product key — what
#: :func:`~meho_backplane.operations._lookup.parse_connector_id` extracts
#: from ``FLEET_CONNECTOR_ID = "fleet-rest-9.0"``
#: (``head.split("-", 1)[0]`` where head is ``"fleet-rest"``). This is
#: the value the G0.7 ingestion substrate writes onto every persisted
#: ``endpoint_descriptor.product`` and ``operation_group.product``
#: row, and the value :class:`ReviewService._resolve_scope` queries
#: against at review time.
#:
#: It differs from :attr:`VcfFleetConnector.product` (``"vcf-fleet"``)
#: — same shape as the SDDC Manager case
#: (``SddcManagerConnector.product="sddc-manager"`` but rows carry
#: ``product="sddc"``). The discrepancy is harmless: the registry's
#: dispatch path looks up the connector class by the v2 triple
#: (``"vcf-fleet", "9.0", "fleet-rest"``) before any descriptor lookup,
#: and the descriptor lookup itself only consults the natural key
#: derived from ``parse_connector_id``. The two keys never collide
#: because they live in different lookup paths.
FLEET_PRODUCT: Final[str] = "fleet"
FLEET_VERSION: Final[str] = "9.0"
FLEET_IMPL_ID: Final[str] = "fleet-rest"

#: Connector-id slug the G0.6 dispatcher's ``parse_connector_id``
#: round-trips back to the triple above: ``"fleet-rest-9.0"``.
FLEET_CONNECTOR_ID: Final[str] = f"{FLEET_IMPL_ID}-{FLEET_VERSION}"


@dataclass(frozen=True, slots=True)
class FleetCoreGroup:
    """One curated operator-review entry for a Fleet operation group.

    ``group_key`` is the slug :func:`classify_fleet_op` emits.
    ``name`` is the operator-readable label ``meho connector review``
    renders. ``when_to_use`` is the agent-facing hint
    :func:`list_operation_groups` returns verbatim; every entry is a
    single complete sentence so the agent's group-selection step has
    unambiguous guidance.
    """

    group_key: str
    name: str
    when_to_use: str


@dataclass(frozen=True, slots=True)
class FleetCoreOp:
    """One curated operator-review entry for a Fleet operation.

    ``op_id`` follows the ``METHOD:path`` shape every
    ``source_kind='ingested'`` row uses; the path matches an entry in
    the vRSLCM LCM REST OpenAPI spec.

    ``llm_instructions`` is the per-op JSON blob the meta-tools inline
    verbatim when the op surfaces. The shape (``when_to_call`` /
    ``output_shape`` / ``next_step``) mirrors the typed-connector
    convention from :mod:`meho_backplane.connectors.bind9.ops_zone`
    and the ingested-connector convention from
    :mod:`meho_backplane.connectors.nsx.core_ops` /
    :mod:`meho_backplane.connectors.harbor.core_ops` — the same agent
    reads all surfaces, so the structure stays uniform.
    """

    op_id: str
    group_key: str
    llm_instructions: dict[str, object]


#: Path-prefix → group_key classifier rules for Fleet.
#:
#: **Order is load-bearing.** Each rule is checked via
#: ``path.startswith(prefix)``. More-specific nested prefixes must
#: precede less-specific ones to avoid a shorter prefix consuming a
#: path that belongs to a deeper group:
#:
#: * ``…/datacenters/{dataCenterVmid}/vcenters`` before
#:   ``…/datacenters`` — vcenter paths also start with the datacenter
#:   prefix.
#: * ``…/environments/{environmentId}/products`` before
#:   ``…/environments`` — product paths also start with the
#:   environment prefix.
#: * ``…/about`` first — its prefix doesn't overlap others, but
#:   listing it first keeps the rule ordering intent-readable
#:   (identity / probe before inventory).
#:
#: The template variable names (``{environmentId}``, etc.) are literal
#: substrings of the rule strings so ``startswith`` comparisons against
#: ingested op_ids (which also carry the literal template var names)
#: resolve correctly.
FLEET_PATH_RULES: Final[tuple[tuple[str, str], ...]] = (
    ("/lcm/lcops/api/v2/about", "fleet-about"),
    # Deeper paths under datacenters must precede the datacenters root.
    (
        "/lcm/lcops/api/v2/datacenters/{dataCenterVmid}/vcenters",
        "fleet-vcenter",
    ),
    ("/lcm/lcops/api/v2/datacenters", "fleet-datacenter"),
    # Deeper paths under environments must precede the environments root.
    (
        "/lcm/lcops/api/v2/environments/{environmentId}/products",
        "fleet-product",
    ),
    ("/lcm/lcops/api/v2/environments", "fleet-environment"),
    # Request paths live under /lcm/request/, not /lcm/lcops/.
    ("/lcm/request/api/v2/requests", "fleet-request"),
)


def classify_fleet_op(op_id: str) -> str:
    """Return the curated ``group_key`` for a Fleet op_id, or ``"none"``.

    ``op_id`` is the ``METHOD:/path`` form ingested rows carry; the
    helper strips the verb and matches the path against
    :data:`FLEET_PATH_RULES` in order. Only ``GET`` verbs classify
    — the v0.5 core is read-only.

    Rule ordering guarantees that the most-specific prefix wins:
    a path like
    ``/lcm/lcops/api/v2/datacenters/{dataCenterVmid}/vcenters``
    matches the ``fleet-vcenter`` rule before the broader
    ``/lcm/lcops/api/v2/datacenters`` rule can fire.

    Returns ``"none"`` for paths outside the curated families (e.g.
    ``/lcm/locker/api/v2/passwords``, ``/lcm/authzn/...``); those
    rows are un-curated and stay ``is_enabled=False`` after
    :func:`apply_fleet_core_curation` runs.
    """
    try:
        method, path = op_id.split(":", 1)
    except ValueError:
        return "none"
    if method != "GET":
        return "none"
    for prefix, group_key in FLEET_PATH_RULES:
        if path.startswith(prefix):
            return group_key
    return "none"


#: Operator-reviewed ``when_to_use`` hints for the 6 Fleet groups
#: the read-only v0.5 core spans. Every hint is one complete sentence
#: the agent reads verbatim — vague hints poison
#: ``search_operations`` ranking.
FLEET_CORE_GROUPS: Final[tuple[FleetCoreGroup, ...]] = (
    FleetCoreGroup(
        group_key="fleet-about",
        name="VCF Fleet (about)",
        when_to_use=(
            "Use this group to read the VCF Fleet (vRSLCM) appliance's "
            "identity: API version, build, product version. WARNING: in "
            "VCF 9.0 builds the /about endpoint returns HTTP 500 — use "
            "fleet.datacenter.list as the reachability probe instead, "
            "and cross-source the product version from SDDC Manager's "
            "/v1/vcf-services entry until the 9.0 regression is fixed."
        ),
    ),
    FleetCoreGroup(
        group_key="fleet-datacenter",
        name="VCF Fleet Datacenters",
        when_to_use=(
            "Use this group to list the datacenters (logical groupings of "
            "vCenters / regions) the Fleet appliance is aware of. The "
            "datacenters call is also the wrapper-verified reachability "
            "probe — guaranteed to respond in 9.0 even when /about is "
            "broken — and is the entry point for navigating into "
            "registered vCenters via fleet.vcenter.list."
        ),
    ),
    FleetCoreGroup(
        group_key="fleet-vcenter",
        name="VCF Fleet vCenters",
        when_to_use=(
            "Use this group to enumerate the vCenters registered under a "
            "Fleet datacenter. Each vCenter entry carries hostname, build, "
            "and the data-collection status; the agent uses these to map "
            "Fleet-managed inventory back to vSphere targets. Requires a "
            "dataCenterVmid from fleet.datacenter.list."
        ),
    ),
    FleetCoreGroup(
        group_key="fleet-environment",
        name="VCF Fleet Environments",
        when_to_use=(
            "Use this group to list or inspect Fleet environments — the "
            "primary unit of Fleet-managed lifecycle. Every product "
            "deployment (vRA, vROps, vRLI, vIDM, …) lives under an "
            "environment. Use to answer 'what environments has Fleet "
            "provisioned' or 'what is the status of environment X' before "
            "drilling into its products."
        ),
    ),
    FleetCoreGroup(
        group_key="fleet-product",
        name="VCF Fleet Products",
        when_to_use=(
            "Use this group to list products deployed under a Fleet "
            "environment. Each product entry carries name, version, "
            "deployment status, and node breakdown. Requires an "
            "environmentId from fleet.environment.list. Use to answer "
            "'what version of vRA is deployed in environment X' or "
            "'which nodes back this product'."
        ),
    ),
    FleetCoreGroup(
        group_key="fleet-request",
        name="VCF Fleet Lifecycle Requests",
        when_to_use=(
            "Use this group to list or inspect lifecycle requests "
            "(deploy / patch / upgrade / scale workflows) Fleet has "
            "processed. Each request carries state (INPROGRESS / "
            "COMPLETED / FAILED), execution path, and error cause. The "
            "primary workflow-status surface — read these to answer "
            "'did the upgrade finish' or 'why did the deploy fail'."
        ),
    ),
)


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


#: The 8 curated read-only Fleet core ops. Each entry carries the
#: op_id (``GET:/path`` form), the curated group assignment, and the
#: operator-reviewed ``llm_instructions`` blob.
#:
#: Paths cross-checked against vRSLCM REST API 1.3.0 at
#: https://developer.broadcom.com/xapis/vrealize-suite-lifecycle-manager/latest/.
FLEET_CORE_OPS: Final[tuple[FleetCoreOp, ...]] = (
    FleetCoreOp(
        op_id="GET:/lcm/lcops/api/v2/about",
        group_key="fleet-about",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to read the vRSLCM appliance's identity — API "
                "version, build, product version — when confirming "
                "which Fleet instance the target points at. KNOWN "
                "REGRESSION: in VCF 9.0 builds this endpoint returns "
                "HTTP 500. When the call fails with a 500, do not "
                "retry; fall back to fleet.datacenter.list as the "
                "appliance reachability probe."
            ),
            output_shape=(
                "On success: object with apiVersion, productVersion, "
                "buildNumber, releaseDate keys. On 500 (the 9.0 "
                "regression): a connector_error OperationResult with "
                "the response body's `errorCode` and `errorMessage` "
                "fields surfaced in extras."
            ),
            next_step=(
                "If the call returned 200, proceed with the intended "
                "Fleet operation. If it returned 500 (VCF 9.0), call "
                "fleet.datacenter.list — it doubles as a reachability "
                "probe and exposes the Lcm-API-Version response header "
                "(read via fingerprint, not call_operation, in the "
                "current substrate)."
            ),
        ),
    ),
    FleetCoreOp(
        op_id="GET:/lcm/lcops/api/v2/datacenters",
        group_key="fleet-datacenter",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to list Fleet-managed datacenters — logical "
                "groupings of vCenters and environments. The primary "
                "inventory entry point for any Fleet workflow, and the "
                "wrapper-verified reachability probe (guaranteed to "
                "respond in VCF 9.0 even when /about is broken). "
                "Supports no query parameters; small lists return "
                "inline, large ones are reduced to a JSONFlux handle "
                "carrying a bounded inline sample plus a ``fetch_more`` "
                "envelope (no handle read-back tool exists in this version)."
            ),
            output_shape=(
                "Array of Datacenter objects; each carries vmid "
                "(opaque datacenter identifier), name, type (PRIVATE_CLOUD "
                "/ PUBLIC_CLOUD), city, country, and a vCenters[] "
                "sub-array with brief vCenter metadata. The vmid is the "
                "load-bearing identifier for follow-up calls."
            ),
            next_step=(
                "Pick a datacenter vmid and pass it as dataCenterVmid "
                "to fleet.vcenter.list to enumerate registered vCenters, "
                "or follow up with fleet.environment.list to enumerate "
                "Fleet-managed product deployments (environments are not "
                "scoped to a datacenter at the API surface)."
            ),
        ),
    ),
    FleetCoreOp(
        op_id="GET:/lcm/lcops/api/v2/datacenters/{dataCenterVmid}/vcenters",
        group_key="fleet-vcenter",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to enumerate vCenters registered under a Fleet "
                "datacenter. Requires a dataCenterVmid path parameter "
                "obtained from fleet.datacenter.list. Returns "
                "Fleet-registered vCenters only — vCenters reachable "
                "to the operator but not registered with Fleet are not "
                "returned here (consult SDDC Manager or the vCenter "
                "API directly for that)."
            ),
            output_shape=(
                "Array of VCenter objects; each carries vmid, name, "
                "hostname (FQDN), buildNumber, version, and "
                "dataCollectionStatus (with timestamp + last-error). "
                "The hostname is the load-bearing identifier for "
                "cross-referencing against vSphere targets."
            ),
            next_step=(
                "Cross-reference the hostname against vSphere targets "
                "configured under the same operator scope; if a Fleet "
                "vCenter has no matching vSphere target, the operator "
                "may need to add it. Use the vCenter's vmid for any "
                "follow-up data-collection trigger (POST surface — "
                "out of v0.5 scope)."
            ),
        ),
    ),
    FleetCoreOp(
        op_id="GET:/lcm/lcops/api/v2/environments",
        group_key="fleet-environment",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to list all Fleet-managed environments. An "
                "environment is the unit Fleet uses to group one or "
                "more product deployments (e.g. a vRA environment with "
                "vRA + vIDM + Postgres nodes). The primary entry point "
                "for any Fleet inventory workflow. Large appliances "
                "managing many environments return a JSONFlux handle "
                "carrying a bounded inline sample plus a ``fetch_more`` "
                "envelope (no handle read-back tool exists in this version)."
            ),
            output_shape=(
                "Array of Environment objects; each carries "
                "environmentId, environmentName, environmentDescription, "
                "environmentStatus (DEPLOY_SUCCESSFUL / DEPLOY_FAILED / "
                "...), and a products[] sub-array with product summaries."
            ),
            next_step=(
                "Pick an environmentId and pass it to "
                "fleet.environment.get for the full per-environment "
                "detail, or to fleet.product.list to enumerate its "
                "products and their versions. Cross-reference status "
                "against the fleet-request surface when an environment "
                "looks stuck (open requests against it may explain why)."
            ),
        ),
    ),
    FleetCoreOp(
        op_id="GET:/lcm/lcops/api/v2/environments/{environmentId}",
        group_key="fleet-environment",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to read the full detail of one Fleet environment "
                "by environmentId. Returns the products[] array with "
                "complete deployment metadata (nodes, IPs, versions, "
                "FQDN), the configuration history, and the status "
                "transitions. Requires an environmentId path parameter "
                "from fleet.environment.list."
            ),
            output_shape=(
                "Environment object with environmentId, "
                "environmentName, environmentStatus, products[] "
                "(each carrying productId, version, nodes[] with "
                "hostname/IP/role), createdOn, modifiedOn, and "
                "transactionId for the most recent operation."
            ),
            next_step=(
                "Surface the products[] versions to the operator for an "
                "inventory snapshot. If the status is DEPLOY_FAILED, "
                "cross-reference transactionId against "
                "fleet.request.get to find the failing workflow's "
                "error cause."
            ),
        ),
    ),
    FleetCoreOp(
        op_id="GET:/lcm/lcops/api/v2/environments/{environmentId}/products",
        group_key="fleet-product",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to list products deployed under one Fleet "
                "environment. Returns one entry per product (vRA, vROps, "
                "vRLI, vIDM, Postgres, …) with deployment status, "
                "version, and node breakdown. Requires an environmentId "
                "from fleet.environment.list. Use when answering 'what "
                "is deployed in environment X' or 'what version of "
                "product Y runs there'."
            ),
            output_shape=(
                "Array of Product objects; each carries productId "
                "(e.g. 'vra', 'vrops', 'vidm'), version, status, "
                "nodes[] (with hostname, ipAddress, role, "
                "vmStatus), and snapshot/backup metadata where Fleet "
                "manages it."
            ),
            next_step=(
                "Surface productId + version pairs to the operator. If "
                "a node's vmStatus is anything other than POWERED_ON, "
                "flag it; if a product version is older than the "
                "supported floor, suggest cross-checking the Fleet "
                "lifecycle-request history for an in-flight upgrade."
            ),
        ),
    ),
    FleetCoreOp(
        op_id="GET:/lcm/request/api/v2/requests",
        group_key="fleet-request",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to list lifecycle requests (deploy / patch / "
                "upgrade / scale workflows) Fleet has processed. "
                "Returns the most recent requests by createdOn; "
                "operators on busy appliances commonly see thousands "
                "of historical entries, so the call returns a JSONFlux "
                "handle carrying a bounded inline sample plus a "
                "``fetch_more`` envelope. Re-call with a narrower filter "
                "(state or requestType) to scope the set down rather than "
                "expecting a handle read-back tool. Use to answer 'what "
                "workflows is Fleet "
                "currently running' or 'what was the last upgrade'."
            ),
            output_shape=(
                "Array of Request objects; each carries vmid, "
                "requestName, requestType (e.g. ENVIRONMENT_CREATE, "
                "PRODUCT_UPGRADE), state (INPROGRESS / COMPLETED / "
                "FAILED), createdOn, lastUpdatedOn, and a brief "
                "executionStatus summary. The full output map + error "
                "cause come from fleet.request.get."
            ),
            next_step=(
                "Filter on state=INPROGRESS to surface in-flight work; "
                "for any FAILED request, drill into fleet.request.get "
                "with the vmid to read the error cause + execution "
                "path."
            ),
        ),
    ),
    FleetCoreOp(
        op_id="GET:/lcm/request/api/v2/requests/{requestId}",
        group_key="fleet-request",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to read the full detail of one Fleet lifecycle "
                "request by vmid (the request's id). Returns the input "
                "map (parameters the request was created with), the "
                "output map (results / generated identifiers), the "
                "execution path (per-stage status), and the error "
                "cause on FAILED. Requires a requestId path parameter "
                "from fleet.request.list."
            ),
            output_shape=(
                "Request object with vmid, transactionId, requestName, "
                "requestReason, requestType, requestSource, "
                "requestSourceType, inputMap (object), outputMap "
                "(object), state, executionId, executionPath[] (stage "
                "list with per-stage status + timestamps), "
                "executionStatus, errorCause (string, only on FAILED), "
                "resultSet, isCancelEnabled, lastUpdatedOn, and "
                "createdBy."
            ),
            next_step=(
                "Surface state + the most recent executionPath[] entry "
                "to the operator. On FAILED, surface errorCause "
                "verbatim — the Fleet appliance writes operator-readable "
                "diagnostic text there. On INPROGRESS, suggest polling "
                "this op rather than re-listing fleet.request.list "
                "(point-read is far cheaper)."
            ),
        ),
    ),
)


async def apply_fleet_core_curation(
    review_service: ReviewService,
    *,
    tenant_id: UUID | None,
) -> None:
    """Apply the curated 8-op read core against an ingested Fleet connector.

    Drives the substrate so that, after this call returns, exactly
    the 8 ops in :data:`FLEET_CORE_OPS` are dispatchable
    (``is_enabled=True``) and every other ingested op stays
    ``is_enabled=False``. The 6 curated groups land
    ``review_status='enabled'`` so the agent's
    :func:`~meho_backplane.operations.meta_tools.search_operations`
    surfaces the core ops; non-curated groups are left untouched
    (``review_status='staged'`` from the G0.7 ingest default).

    The substrate doesn't expose "enable only ops X, Y, Z under
    group G": :meth:`ReviewService.enable_group`'s cascade flips
    ``is_enabled=True`` on every child op in the group. The helper
    works around this via the audit-log-driven operator-override
    exclusion — the same mechanism
    :func:`~meho_backplane.connectors.harbor.core_ops.apply_harbor_core_curation`
    and :func:`~meho_backplane.connectors.nsx.core_ops.apply_nsx_core_curation`
    established:

    1. :meth:`ReviewService.get_review_payload` loads the current
       state of every curated group and its child ops.
    2. For each child op in a curated group that **isn't** in the
       :data:`FLEET_CORE_OPS` allow-list,
       :meth:`ReviewService.edit_op` with ``is_enabled=False``
       writes the operator-override audit row. The follow-on
       :meth:`enable_group` cascade detects these rows and skips
       them.
    3. :meth:`ReviewService.edit_group` lands the operator-reviewed
       ``name`` + ``when_to_use`` on each curated group.
    4. :meth:`ReviewService.enable_group` flips
       ``review_status='enabled'`` and cascades ``is_enabled=True``
       to the curated child ops (operator-overridden non-core ops
       are skipped).
    5. :meth:`ReviewService.edit_op` lands the curated
       ``llm_instructions`` blob per entry in :data:`FLEET_CORE_OPS`.

    Re-running is safe but not idempotent at the audit layer:
    :meth:`enable_group` short-circuits on groups already in
    ``review_status='enabled'`` (no audit row), but
    :meth:`edit_group` and :meth:`edit_op` always emit one audit
    row per call. The intended posture is a one-shot curation step
    after ingest; re-runs produce redundant ``meho.connector.edit_*``
    audit rows but never corrupt state.

    Raises :class:`~meho_backplane.operations.ingest.ConnectorNotFoundError`
    if no groups exist for ``fleet-rest-9.0`` under *tenant_id* (the
    operator must run ``meho connector ingest`` against the Fleet
    LCM spec before this helper applies).
    """
    payload = await review_service.get_review_payload(
        FLEET_CONNECTOR_ID,
        tenant_id,
    )

    core_op_ids_by_group: dict[str, set[str]] = {}
    for op in FLEET_CORE_OPS:
        core_op_ids_by_group.setdefault(op.group_key, set()).add(op.op_id)

    for group_payload in payload.groups:
        allow_list = core_op_ids_by_group.get(group_payload.group_key)
        if allow_list is None:
            continue
        for review_op in group_payload.ops:
            if review_op.op_id in allow_list:
                continue
            await review_service.edit_op(
                FLEET_CONNECTOR_ID,
                review_op.op_id,
                tenant_id=tenant_id,
                is_enabled=False,
            )
            _log.info(
                "fleet_non_core_op_disabled",
                connector_id=FLEET_CONNECTOR_ID,
                op_id=review_op.op_id,
                group_key=group_payload.group_key,
            )

    for group in FLEET_CORE_GROUPS:
        await review_service.edit_group(
            FLEET_CONNECTOR_ID,
            group.group_key,
            tenant_id=tenant_id,
            name=group.name,
            when_to_use=group.when_to_use,
        )
        await review_service.enable_group(
            FLEET_CONNECTOR_ID,
            group.group_key,
            tenant_id=tenant_id,
        )
        _log.info(
            "fleet_core_group_enabled",
            connector_id=FLEET_CONNECTOR_ID,
            group_key=group.group_key,
        )

    for op in FLEET_CORE_OPS:
        await review_service.edit_op(
            FLEET_CONNECTOR_ID,
            op.op_id,
            tenant_id=tenant_id,
            llm_instructions=op.llm_instructions,
        )
        _log.info(
            "fleet_core_op_curated",
            connector_id=FLEET_CONNECTOR_ID,
            op_id=op.op_id,
        )
