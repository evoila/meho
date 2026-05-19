# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""SDDC Manager 9.0 read-only v0.2 core — curated operator-enabled subset.

This module names the **9 read-only SDDC Manager operations** the G3.5
SDDC Manager v0.2 ship enables out of the much larger VCF API corpus the
G0.7 spec-ingestion pipeline lands under
``connector_id="sddc-rest-9.0"``. The curation is two-layered:

* :data:`SDDC_CORE_GROUPS` — the operator-reviewed ``when_to_use``
  hint per LLM-grouping pass output group. Each entry's ``group_key``
  is the deterministic slug the path-prefix classifier assigns to SDDC
  Manager ops (see :func:`classify_sddc_op` below); the ``when_to_use``
  is what the agent reads verbatim through
  :func:`~meho_backplane.operations.meta_tools.list_operation_groups`
  to pick a group to search within.
* :data:`SDDC_CORE_OPS` — the 9 ``EndpointDescriptor.op_id`` strings
  that flip to ``is_enabled=True`` at operator-review time, paired
  with the per-op ``llm_instructions`` blob the agent inlines into
  the reasoning context when it sees the op in
  :func:`~meho_backplane.operations.meta_tools.search_operations`
  hits. Every other op under the same connector triple stays
  ``is_enabled=False`` (the G0.7 ingestion default for
  ``source_kind='ingested'`` rows).

Per Initiative #368 and CLAUDE.md postulates 1-2, SDDC Manager is **fully
generic-ingested**: the underlying ops are not registered in code, they
live in the ``endpoint_descriptor`` table. This module only carries the
**operator-review metadata** the substrate uses at the review step — the
actual curation is applied through :func:`apply_sddc_core_curation`
against an existing ingested connector.

``SDDC_PRODUCT`` / ``SDDC_VERSION`` / ``SDDC_IMPL_ID`` note
------------------------------------------------------------

``SDDC_PRODUCT = "sddc"`` is the value
:func:`~meho_backplane.operations._lookup.parse_connector_id` extracts
from ``SDDC_CONNECTOR_ID = "sddc-rest-9.0"`` (``head.split("-", 1)[0]``).
It is **not** the same as :attr:`SddcManagerConnector.product`, which is
``"sddc-manager"`` and is used only for the v2 connector registry and the
target-product resolver. All ``endpoint_descriptor`` and
``operation_group`` rows for this connector triple carry
``product="sddc"``; operators pass ``--product sddc`` when driving
``meho connector ingest``. Review-time helpers (including
:class:`~meho_backplane.operations.ingest.service.ReviewService`) derive
the product via ``parse_connector_id`` so the same constant is consistent
across the ingestion, review, and dispatch legs.

The 9 ops (paths cross-checked against VCF / SDDC Manager API 9.0 at
https://developer.broadcom.com/xapis/vmware-cloud-foundation-api/latest/):

1. ``GET:/v1/releases/system`` — ``sddc.about`` — SDDC Manager software
   release (version, build date, component BOM). The same surface the
   operator reads to confirm the VCF build under management; exposing it
   as an agent-callable op lets the agent run a sanity probe before heavier
   inventory reads.
2. ``GET:/v1/sddc-managers`` — ``sddc.manager.list`` — list of SDDC Manager
   appliances (FQDN, IP, version, management domain). The primary read for
   "which SDDC Manager appliance manages this VCF stack."
3. ``GET:/v1/domains`` — ``sddc.domain.list`` — management + workload
   domains under this SDDC Manager.
4. ``GET:/v1/domains/{id}`` — ``sddc.domain.info`` — full domain detail
   including associated vCenter(s), NSX-T cluster, clusters, and hosts.
5. ``GET:/v1/clusters`` — ``sddc.cluster.list`` — cluster inventory across
   all or one domain (``?domainId=`` filter).
6. ``GET:/v1/hosts`` — ``sddc.host.list`` — ESXi host inventory across all
   or one domain or cluster (``?domainId=`` / ``?clusterId=`` filter).
7. ``GET:/v1/network-pools`` — ``sddc.network_pool.list`` — network pools
   defining the IP ranges and VLANs provisioned to new hosts at commission
   time.
8. ``GET:/v1/bundles`` — ``sddc.bundle.list`` — LCM bundle inventory (VCF
   update packages, async patches). Read-only; lifecycle writes stay
   ``staged`` per Initiative #368.
9. ``GET:/v1/tasks`` — ``sddc.workflow.list`` — in-flight and recently
   completed VCF workflow tasks. The SDDC Manager API surface uses the term
   ``tasks``; the issue body calls this surface ``sddc.workflow.list`` for
   operator clarity.

Path families and group_keys
-----------------------------

Every SDDC Manager REST path begins with ``/v1/``. The 9 curated ops
span 8 distinct top-level resource families:
``releases``, ``sddc-managers``, ``domains``, ``clusters``, ``hosts``,
``network-pools``, ``bundles``, ``tasks``. :data:`SDDC_PATH_RULES`
mirrors that taxonomy so an operator reviewing the connector sees the
read-core ops fall into the expected 8 groups. The ``sddc-domains``
group carries two ops (list + detail).

Curation application
--------------------

:func:`apply_sddc_core_curation` is the operator-review-time substrate
call that makes exactly the 9 curated ops dispatchable and leaves every
other ingested op disabled. The substrate has no "enable only ops X, Y, Z
under group G" verb — :meth:`ReviewService.enable_group` cascades
``is_enabled=True`` to every child op. The helper threads this needle via
the audit-log-driven operator-override exclusion (see
:func:`~meho_backplane.operations.ingest._internals.operator_disabled_op_ids`),
exactly matching the pattern :func:`apply_nsx_core_curation` in
:mod:`meho_backplane.connectors.nsx.core_ops` established.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final
from uuid import UUID

import structlog

from meho_backplane.operations.ingest.service import ReviewService

__all__ = [
    "SDDC_CONNECTOR_ID",
    "SDDC_CORE_GROUPS",
    "SDDC_CORE_OPS",
    "SDDC_IMPL_ID",
    "SDDC_PATH_RULES",
    "SDDC_PRODUCT",
    "SDDC_VERSION",
    "SddcCoreGroup",
    "SddcCoreOp",
    "apply_sddc_core_curation",
    "classify_sddc_op",
]

_log = structlog.get_logger(__name__)

#: Endpoint-descriptor product key — what
#: :func:`~meho_backplane.operations._lookup.parse_connector_id`
#: extracts from ``"sddc-rest-9.0"`` (first hyphen-segment of
#: impl_id ``"sddc-rest"``).
#:
#: Distinct from :attr:`SddcManagerConnector.product` (``"sddc-manager"``)
#: which is the v2 registry key and resolver target. All
#: ``endpoint_descriptor`` + ``operation_group`` rows carry
#: ``product="sddc"``; see the module docstring for details.
SDDC_PRODUCT: Final[str] = "sddc"
SDDC_VERSION: Final[str] = "9.0"
SDDC_IMPL_ID: Final[str] = "sddc-rest"

#: Connector-id slug the G0.6 dispatcher's ``parse_connector_id``
#: round-trips back to the triple above: ``"sddc-rest-9.0"``.
SDDC_CONNECTOR_ID: Final[str] = f"{SDDC_IMPL_ID}-{SDDC_VERSION}"


@dataclass(frozen=True, slots=True)
class SddcCoreGroup:
    """One curated operator-review entry for an SDDC Manager operation group.

    ``group_key`` is the slug the path-prefix classifier emits (see
    :data:`SDDC_PATH_RULES`). ``name`` is the operator-readable label
    ``meho connector review`` renders. ``when_to_use`` is the agent-facing
    hint :func:`list_operation_groups` returns verbatim; every entry is a
    single complete sentence so the agent's group-selection step has
    unambiguous guidance.
    """

    group_key: str
    name: str
    when_to_use: str


@dataclass(frozen=True, slots=True)
class SddcCoreOp:
    """One curated operator-review entry for an SDDC Manager operation.

    ``op_id`` follows the ``METHOD:path`` shape every
    ``source_kind='ingested'`` row uses; the path matches an entry
    in the VCF / SDDC Manager 9.0 API spec.

    ``llm_instructions`` is the per-op JSON blob the meta-tools inline
    verbatim when the op surfaces. The shape (``when_to_call`` /
    ``output_shape`` / ``next_step``) mirrors the typed-connector convention
    from :mod:`meho_backplane.connectors.bind9.ops_zone` and
    :mod:`meho_backplane.connectors.nsx.core_ops` — same agent reads both
    surfaces, so the structure stays uniform.
    """

    op_id: str
    group_key: str
    llm_instructions: dict[str, object]


#: Path-prefix → group_key classifier rules for SDDC Manager.
#:
#: First match wins. Order: more specific prefixes first (``/v1/sddc-managers``
#: before any hypothetical ``/v1/sddc-*`` catch-all). Every SDDC Manager
#: path begins with ``/v1/`` so there is no manager-vs-policy split like
#: NSX; the top-level resource name is the natural grouping key.
SDDC_PATH_RULES: Final[tuple[tuple[str, str], ...]] = (
    ("/v1/releases", "sddc-releases"),
    ("/v1/sddc-managers", "sddc-managers"),
    ("/v1/domains", "sddc-domains"),
    ("/v1/clusters", "sddc-clusters"),
    ("/v1/hosts", "sddc-hosts"),
    ("/v1/network-pools", "sddc-network-pools"),
    ("/v1/bundles", "sddc-bundles"),
    ("/v1/tasks", "sddc-tasks"),
)


def classify_sddc_op(op_id: str) -> str:
    """Return the curated ``group_key`` for an SDDC Manager op_id, or ``"none"``.

    ``op_id`` is the ``METHOD:/path`` form ingested rows carry; the
    helper strips the verb and matches the path against
    :data:`SDDC_PATH_RULES` in order.

    Returns ``"none"`` for paths outside the curated families (e.g.
    ``/v1/vcenters``, ``/v1/nsxt-clusters``); operators reviewing
    the ingested connector see those rows as unassigned and can either
    tighten the rules in this module on the next release or accept them
    as un-curated. The G0.7 canary's ``operations_unassigned < 50%`` bar
    tolerates a long tail of un-curated paths.
    """
    try:
        _, path = op_id.split(":", 1)
    except ValueError:
        return "none"
    for prefix, group_key in SDDC_PATH_RULES:
        if path.startswith(prefix):
            return group_key
    return "none"


#: Operator-reviewed ``when_to_use`` hints for the 8 SDDC Manager groups
#: the read-only v0.2 core spans. Two ops share the ``sddc-domains`` group
#: (list + detail); every other group carries exactly one op. Every hint is
#: one complete sentence the agent reads verbatim — vague hints poison
#: ``search_operations`` ranking, per the ai_engineering pack.
SDDC_CORE_GROUPS: Final[tuple[SddcCoreGroup, ...]] = (
    SddcCoreGroup(
        group_key="sddc-releases",
        name="SDDC Manager (release)",
        when_to_use=(
            "Use this group to read the SDDC Manager's own software release — "
            "VCF version, build date, and component BOM. The probe surface the "
            "agent calls before any heavier inventory read or when confirming "
            "the VCF build under management."
        ),
    ),
    SddcCoreGroup(
        group_key="sddc-managers",
        name="SDDC Manager (appliance)",
        when_to_use=(
            "Use this group to list SDDC Manager appliances — their FQDN, IP "
            "address, version, and the management domain they belong to. The "
            "primary read for 'which SDDC Manager appliance manages this VCF "
            "stack' questions."
        ),
    ),
    SddcCoreGroup(
        group_key="sddc-domains",
        name="VCF Domains",
        when_to_use=(
            "Use this group to list or inspect VCF domains (management and "
            "workload). The entry point for any domain-scoped cluster, host, or "
            "network-pool query, and for mapping which vCenter or NSX-T cluster "
            "belongs to a given domain."
        ),
    ),
    SddcCoreGroup(
        group_key="sddc-clusters",
        name="VCF Clusters",
        when_to_use=(
            "Use this group to list vSphere clusters across all or one VCF "
            "domain. The primary inventory read for 'how many clusters exist', "
            "'what datastore type does a cluster use', or 'which hosts are in "
            "cluster X' questions."
        ),
    ),
    SddcCoreGroup(
        group_key="sddc-hosts",
        name="VCF Hosts",
        when_to_use=(
            "Use this group to enumerate ESXi hosts across all VCF domains, or "
            "filter to a specific domain or cluster. The primary read for host "
            "count, FQDN, ESXi version, and assignment status questions."
        ),
    ),
    SddcCoreGroup(
        group_key="sddc-network-pools",
        name="VCF Network Pools",
        when_to_use=(
            "Use this group to list network pools configured in SDDC Manager. "
            "Network pools define the IP ranges and VLANs provisioned to new "
            "ESXi hosts during domain expansion or host commission operations."
        ),
    ),
    SddcCoreGroup(
        group_key="sddc-bundles",
        name="VCF LCM Bundles",
        when_to_use=(
            "Use this group to list LCM bundles available on this SDDC Manager — "
            "VCF update packages, component updates, and async patches. Read-only; "
            "use when answering 'which updates are available' or 'is the VCF stack "
            "compliant with the latest release'."
        ),
    ),
    SddcCoreGroup(
        group_key="sddc-tasks",
        name="VCF Workflows (Tasks)",
        when_to_use=(
            "Use this group to list in-flight or recently completed VCF workflow "
            "tasks. Use when answering 'what operations are running against this "
            "VCF stack' or monitoring a domain-expand, host-commission, or "
            "update-apply workflow."
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

    Same three-field shape :mod:`meho_backplane.connectors.nsx.core_ops` and
    :mod:`meho_backplane.connectors.bind9.ops_zone` use so an agent crossing
    connector boundaries sees a stable convention.
    """
    return {
        "when_to_call": when_to_call,
        "output_shape": output_shape,
        "next_step": next_step,
    }


#: The 9 curated read-only SDDC Manager core ops. Each entry carries the
#: op_id (``GET:/path`` form), the curated group assignment, and the
#: operator-reviewed ``llm_instructions`` blob.
#:
#: Paths sourced against VCF / SDDC Manager API 9.0 at
#: https://developer.broadcom.com/xapis/vmware-cloud-foundation-api/latest/.
SDDC_CORE_OPS: Final[tuple[SddcCoreOp, ...]] = (
    SddcCoreOp(
        op_id="GET:/v1/releases/system",
        group_key="sddc-releases",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to read the SDDC Manager's own software release — VCF "
                "version string, release date, and the component BOM listing "
                "every bundled product's version. Useful as a pre-flight probe "
                "before heavier inventory reads or to confirm the VCF build "
                "under management."
            ),
            output_shape=(
                "Object with version, releaseDate, description, and bom[] "
                "(each entry carries componentType and componentVersion)."
            ),
            next_step=(
                "If the release looks current, proceed to domain or cluster "
                "reads; if behind, surface the version to the operator for an "
                "LCM update check via sddc.bundle.list."
            ),
        ),
    ),
    SddcCoreOp(
        op_id="GET:/v1/sddc-managers",
        group_key="sddc-managers",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to list SDDC Manager appliances — their FQDN, IP address, "
                "version, and the management domain they belong to. The primary "
                "read for 'which SDDC Manager manages this VCF stack' questions, "
                "or when you need the appliance FQDN before making further "
                "targeted calls."
            ),
            output_shape=(
                "Paginated envelope with elements[] and pageMetadata; each "
                "element carries id, fqdn, ipAddress, version, and a domain "
                "object with id and name."
            ),
            next_step=(
                "Cross-reference the management domain name against sddc.domain.list "
                "for domain-scoped queries, or use the FQDN when targeting a "
                "specific appliance."
            ),
        ),
    ),
    SddcCoreOp(
        op_id="GET:/v1/domains",
        group_key="sddc-domains",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to list all VCF domains managed by this SDDC Manager — "
                "the management domain and any workload domains. The entry point "
                "for domain-scoped cluster, host, or network-pool queries and for "
                "mapping which vCenter or NSX-T cluster belongs to a given domain."
            ),
            output_shape=(
                "Paginated envelope with elements[] and pageMetadata; each domain "
                "carries id, name, type (MANAGEMENT or WORKLOAD), and references "
                "to associated vcenters and nsxtCluster."
            ),
            next_step=(
                "Pick a domain id for sddc.domain.info to get the full detail "
                "including vCenter FQDN and NSX-T cluster, or pass domainId as a "
                "filter to sddc.cluster.list / sddc.host.list."
            ),
        ),
    ),
    SddcCoreOp(
        op_id="GET:/v1/domains/{id}",
        group_key="sddc-domains",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to read the full detail of one VCF domain — its associated "
                "vCenter(s), NSX-T cluster (with VIP FQDN), cluster list, and host "
                "references. Requires a domain id from sddc.domain.list; use after "
                "listing domains to answer 'which vCenter manages domain X' or "
                "'what NSX-T cluster backs this workload domain'."
            ),
            output_shape=(
                "Domain object with vcenters[] (id, fqdn), nsxtCluster (id, "
                "vipFqdn), clusters[] (id, name), and ssoId/ssoName fields."
            ),
            next_step=(
                "Cross-reference vcenters[].fqdn against the vSphere connector "
                "for VM reads, or use cluster ids for targeted sddc.cluster.list "
                "or sddc.host.list queries."
            ),
        ),
    ),
    SddcCoreOp(
        op_id="GET:/v1/clusters",
        group_key="sddc-clusters",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to list vSphere clusters across all VCF domains, or filter "
                "to a specific domain by passing domainId as a query parameter. "
                "The primary inventory read for cluster count, datastore type, "
                "and host membership questions."
            ),
            output_shape=(
                "Paginated envelope with elements[] and pageMetadata; each cluster "
                "carries id, name, primaryDatastoreType, domainId, and a hosts[] "
                "array of host references."
            ),
            next_step=(
                "Cross-reference domainId against the domain listing for "
                "context, or use cluster ids to filter sddc.host.list to the "
                "specific cluster's hosts."
            ),
        ),
    ),
    SddcCoreOp(
        op_id="GET:/v1/hosts",
        group_key="sddc-hosts",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to enumerate ESXi hosts across all VCF domains, or filter "
                "by domainId or clusterId query parameters. The primary inventory "
                "read for host count, FQDN, ESXi version, IP addresses, and "
                "assignment status. Large VCF deployments may return dozens or "
                "hundreds of hosts — use the JSONFlux handle path for big sets."
            ),
            output_shape=(
                "Paginated envelope with elements[] and pageMetadata; each host "
                "carries id, fqdn, esxiVersion, ipAddresses[], domain.id, "
                "cluster.id, networkPool.id, and status "
                "(ASSIGNED or UNASSIGNED_USEABLE)."
            ),
            next_step=(
                "Cross-reference cluster.id against sddc.cluster.list, or use "
                "fqdn when targeting a specific host for vSphere reads. For "
                "hosts in UNASSIGNED_USEABLE status, surface them to the operator "
                "as available for domain expansion."
            ),
        ),
    ),
    SddcCoreOp(
        op_id="GET:/v1/network-pools",
        group_key="sddc-network-pools",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to list network pools configured in SDDC Manager. Network "
                "pools define the IP ranges, VLANs, and subnets provisioned to "
                "new ESXi hosts during domain expansion or host commission. Use "
                "when answering 'what network pool will be used for the next "
                "host commission' or verifying VLAN assignments."
            ),
            output_shape=(
                "Paginated envelope with elements[] and pageMetadata; each pool "
                "carries id, name, and networks[] (each network has type, vlanId, "
                "subnet, mask, gateway, and ipPools[] with start/end ranges)."
            ),
            next_step=(
                "Cross-reference a pool's id against cluster or host records when "
                "answering 'which network pool covers this host' or confirming "
                "pre-commission networking requirements."
            ),
        ),
    ),
    SddcCoreOp(
        op_id="GET:/v1/bundles",
        group_key="sddc-bundles",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to list LCM bundles available on this SDDC Manager — VCF "
                "update packages, component patches, and async updates. Read-only; "
                "use when answering 'which updates are available', 'is the VCF "
                "stack compliant with the latest release', or 'what version does "
                "bundle X contain'."
            ),
            output_shape=(
                "Array of bundle objects; each carries id, type "
                "(VMWARE_SOFTWARE), version, description, sizeMB, "
                "downloadStatus, isCumulative, isCompliant, "
                "applicabilityStatus, and components[] (component name + "
                "version pairs)."
            ),
            next_step=(
                "Surface bundle isCompliant and applicabilityStatus to the "
                "operator; cross-reference the VCF release version from "
                "sddc.about to identify applicable update bundles."
            ),
        ),
    ),
    SddcCoreOp(
        op_id="GET:/v1/tasks",
        group_key="sddc-tasks",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to list in-flight or recently completed VCF workflow tasks "
                "(SDDC Manager calls these 'tasks'; the operator-facing name is "
                "sddc.workflow.list). Use when answering 'what operations are "
                "running against this VCF stack', monitoring a domain-expand or "
                "host-commission operation, or triaging a workflow that failed. "
                "Supports status filtering via the status query parameter "
                "(Successful, Failed, In_Progress, Pending, Cancelled)."
            ),
            output_shape=(
                "Task objects with id, name, status, type, creationTimestamp, "
                "completionTimestamp, subtasks[] (nested task tree), and "
                "errors[] (message + remediation hints for failed tasks)."
            ),
            next_step=(
                "For a failed task, surface errors[].message and "
                "errors[].remediationMessage to the operator. For an "
                "in-progress task, poll via this op or wait for the status "
                "to transition before proceeding with the next workflow step."
            ),
        ),
    ),
)


async def apply_sddc_core_curation(
    review_service: ReviewService,
    *,
    tenant_id: UUID | None,
) -> None:
    """Apply the curated 9-op read core against an ingested SDDC Manager connector.

    Drives the substrate so that, after this call returns, exactly
    the 9 ops in :data:`SDDC_CORE_OPS` are dispatchable
    (``is_enabled=True``) and every other ingested op stays
    ``is_enabled=False``. The 8 curated groups land
    ``review_status='enabled'`` so the agent's
    :func:`~meho_backplane.operations.meta_tools.search_operations`
    surfaces the core ops; non-curated groups are left untouched
    (``review_status='staged'`` from the G0.7 ingest default).

    The substrate doesn't expose "enable only ops X, Y, Z under
    group G": :meth:`ReviewService.enable_group`'s cascade flips
    ``is_enabled=True`` on every child op in the group. The helper
    works around this via the audit-log-driven operator-override
    exclusion:

    1. :meth:`ReviewService.get_review_payload` loads the current
       state of every curated group and its child ops.
    2. For each child op in a curated group that **isn't** in the
       :data:`SDDC_CORE_OPS` allow-list,
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
       ``llm_instructions`` blob per entry in :data:`SDDC_CORE_OPS`.

    Re-running is safe but not idempotent at the audit layer.
    :meth:`enable_group` short-circuits on groups already in
    ``review_status='enabled'`` (no audit row), but
    :meth:`edit_group` and :meth:`edit_op` always emit one audit
    row per call — even when the incoming value equals the
    persisted one.

    Raises :class:`~meho_backplane.operations.ingest.ConnectorNotFoundError`
    if no groups exist for ``sddc-rest-9.0`` under *tenant_id* (the
    operator must run ``meho connector ingest`` against the VCF spec
    before this helper applies).
    """
    payload = await review_service.get_review_payload(
        SDDC_CONNECTOR_ID,
        tenant_id,
    )

    core_op_ids_by_group: dict[str, set[str]] = {}
    for op in SDDC_CORE_OPS:
        core_op_ids_by_group.setdefault(op.group_key, set()).add(op.op_id)

    for group_payload in payload.groups:
        allow_list = core_op_ids_by_group.get(group_payload.group_key)
        if allow_list is None:
            continue
        for review_op in group_payload.ops:
            if review_op.op_id in allow_list:
                continue
            await review_service.edit_op(
                SDDC_CONNECTOR_ID,
                review_op.op_id,
                tenant_id=tenant_id,
                is_enabled=False,
            )
            _log.info(
                "sddc_non_core_op_disabled",
                connector_id=SDDC_CONNECTOR_ID,
                op_id=review_op.op_id,
                group_key=group_payload.group_key,
            )

    for group in SDDC_CORE_GROUPS:
        await review_service.edit_group(
            SDDC_CONNECTOR_ID,
            group.group_key,
            tenant_id=tenant_id,
            name=group.name,
            when_to_use=group.when_to_use,
        )
        await review_service.enable_group(
            SDDC_CONNECTOR_ID,
            group.group_key,
            tenant_id=tenant_id,
        )
        _log.info(
            "sddc_core_group_enabled",
            connector_id=SDDC_CONNECTOR_ID,
            group_key=group.group_key,
        )

    for op in SDDC_CORE_OPS:
        await review_service.edit_op(
            SDDC_CONNECTOR_ID,
            op.op_id,
            tenant_id=tenant_id,
            llm_instructions=op.llm_instructions,
        )
        _log.info(
            "sddc_core_op_curated",
            connector_id=SDDC_CONNECTOR_ID,
            op_id=op.op_id,
        )
