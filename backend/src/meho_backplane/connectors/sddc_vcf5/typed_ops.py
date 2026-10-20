# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed-op metadata + registrar for :class:`SddcVcf5Connector` (#3059).

The VCF 5.x SDDC Manager migration-inventory read set is registered as typed ops
(``source_kind="typed"``) so it dispatches on a fresh boot with **zero catalog
ingest** — the read core is live the moment the connector loads (part of initiative
`evoila/meho#3056 <https://github.com/evoila/meho/issues/3056>`_, legacy
migration-source connector coverage; this is the **legacy dual-impl** of
``product="sddc"``, sibling to the modern 9.x ``sddc-rest``).

The op bodies live in :mod:`meho_backplane.connectors.sddc_vcf5.typed_reads`; thin
bound-method shims on
:class:`~meho_backplane.connectors.sddc_vcf5.connector.SddcVcf5Connector` expose
them under the ``handler_attr`` names below so the dispatcher's
:func:`~meho_backplane.operations._handler_resolve.import_handler` walk recovers
each callable from its persisted ``module.ClassName.method`` path. The dataclass +
tuple + module-level registrar shape mirrors
:mod:`meho_backplane.connectors.sddc_manager.typed_ops`.

Op ids are namespaced ``sddc.vcf5.*`` (distinct from the modern impl's ``sddc.*``):
the two impls publish read surfaces for different VCF generations and the resolver
picks one per target by fingerprint, so an agent only ever sees the set matching the
target's version. A tight **migration-inventory** subset ships — domains / clusters
/ hosts / vCenters / NSX-T clusters / SDDC Manager + tasks. The modern connector's
credential-read (secret-bearing) and network-pool host-commissioning reads are
deliberately out of scope: this core inventories a migration *source*, it does not
operate it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

import structlog

from meho_backplane.connectors.sddc_vcf5._response_schemas import SDDC_VCF5_RESPONSE_SCHEMAS

if TYPE_CHECKING:
    from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "SDDC_VCF5_TYPED_OPS",
    "SDDC_VCF5_TYPED_WHEN_TO_USE_BY_GROUP",
    "SddcVcf5TypedOp",
    "register_sddc_vcf5_typed_operations",
]

_log = structlog.get_logger(__name__)

# Typed-op group keys — distinct from the modern ``sddc-*`` keys so the
# (product, impl_id, group_key) rows never collide across the two impls of product
# ``sddc``. Ordered by the migration operator's drill: what exists (inventory) ->
# what is running (tasks).
_GROUP_INVENTORY = "sddc-vcf5-inventory"
_GROUP_TASKS = "sddc-vcf5-tasks"


@dataclass(frozen=True)
class SddcVcf5TypedOp:
    """Metadata for one VCF 5.x SDDC Manager typed op registered at lifespan startup.

    Fields mirror the keyword arguments
    :func:`~meho_backplane.operations.typed_register.register_typed_operation`
    accepts so :func:`register_sddc_vcf5_typed_operations` can splat the dataclass
    into the helper without per-op boilerplate. ``handler_attr`` is the attribute
    name on :class:`SddcVcf5Connector` exposing the async handler; the registrar
    resolves the bound method against the class at registration time. Mirrors
    :class:`~meho_backplane.connectors.sddc_manager.typed_ops.SddcTypedOp`.
    """

    op_id: str
    handler_attr: str
    summary: str
    description: str
    parameter_schema: dict[str, Any]
    response_schema: dict[str, Any] | None
    group_key: str
    tags: tuple[str, ...]
    safety_level: Literal["safe", "caution", "dangerous", "destructive"]
    requires_approval: bool
    llm_instructions: dict[str, Any] | None


#: Short text per typed-op group. The agent reads it through
#: ``list_operation_groups`` to pick a group, so it says which reads each
#: group holds. ``register_typed_operation`` requires a non-empty string
#: whenever ``group_key`` is set.
SDDC_VCF5_TYPED_WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    _GROUP_INVENTORY: (
        "Reads of a VCF 5.x stack: domains, clusters, ESXi hosts, vCenters, NSX "
        "clusters, and the SDDC Manager appliance with its version. Use it to see "
        "what the stack holds and how big it is, for example before a migration. "
        "All reads; nothing here changes anything."
    ),
    _GROUP_TASKS: (
        "One read: the VCF 5.x workflow tasks, running or recently ended. Use it to "
        "check that nothing runs before a cutover, or to find a failed task. All "
        "reads."
    ),
}


# ---------------------------------------------------------------------------
# Shared schema helpers
# ---------------------------------------------------------------------------

#: The empty parameter object shared by the no-argument reads.
_NO_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
    "examples": [{}],
}


def _instructions(
    *,
    when_to_call: str,
    output_shape: str,
    next_step: str,
    parameter_hints: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build the per-op ``llm_instructions`` blob with the canonical keys."""
    blob: dict[str, Any] = {
        "when_to_call": when_to_call,
        "output_shape": output_shape,
        "next_step": next_step,
    }
    if parameter_hints is not None:
        blob["parameter_hints"] = parameter_hints
    return blob


def _optional_id(description: str) -> dict[str, Any]:
    """An optional id filter."""
    return {"type": "string", "minLength": 1, "description": description}


#: The ``{domainId?}`` parameter schema of the reads that filter by domain.
_DOMAIN_FILTER_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "domainId": _optional_id(
            "Optional VCF domain id (from sddc.vcf5.domain.list). Omit for all domains."
        ),
    },
    "additionalProperties": False,
    "examples": [{"domainId": "<domain-id>"}],
}


# ---------------------------------------------------------------------------
# Inventory group
# ---------------------------------------------------------------------------

_DOMAIN_LIST = SddcVcf5TypedOp(
    op_id="sddc.vcf5.domain.list",
    handler_attr="domain_list",
    summary="List the VCF 5.x domains (management and workload) SDDC Manager manages.",
    description=(
        "Lists every VCF domain via GET /v1/domains: the management domain and the "
        "workload domains, with their vCenters, clusters and NSX cluster. The top of "
        "the VCF 5.x inventory. Read-only."
    ),
    parameter_schema=_NO_PARAMS,
    response_schema=SDDC_VCF5_RESPONSE_SCHEMAS["sddc.vcf5.domain.list"],
    group_key=_GROUP_INVENTORY,
    tags=("read-only", "sddc", "vcf", "domain", "inventory"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=_instructions(
        when_to_call=(
            "Call first when you take stock of a VCF 5.x stack. The domains are the "
            "top of the tree; their ids scope the cluster, host and vCenter reads."
        ),
        output_shape=(
            "{elements: [{id, name, type, status, vcenters: [{id, fqdn}], clusters: "
            "[{id}], nsxtCluster: {id, vipFqdn}, ssoName}], pageMetadata}."
        ),
        next_step=(
            "Pass a domain id as domainId to sddc.vcf5.host.list or "
            "sddc.vcf5.vcenter.list to read one domain."
        ),
    ),
)

_CLUSTER_LIST = SddcVcf5TypedOp(
    op_id="sddc.vcf5.cluster.list",
    handler_attr="cluster_list",
    summary="List the vSphere clusters of all VCF 5.x domains or of one domain.",
    description=(
        "Lists vSphere clusters via GET /v1/clusters, for all domains or for one "
        "domain with domainId. Shows each cluster's main datastore and type, its "
        "hosts and its distributed switches. Read-only."
    ),
    parameter_schema=_DOMAIN_FILTER_PARAMS,
    response_schema=SDDC_VCF5_RESPONSE_SCHEMAS["sddc.vcf5.cluster.list"],
    group_key=_GROUP_INVENTORY,
    tags=("read-only", "sddc", "vcf", "cluster", "inventory"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=_instructions(
        when_to_call=(
            "Call to list vSphere clusters; pass domainId to read one domain. The "
            "vendor reference lists the domainId filter from 5.2 on; on older 5.x "
            "builds check the domain of each row yourself."
        ),
        output_shape=(
            "{elements: [{id, name, primaryDatastoreName, primaryDatastoreType, "
            "isStretched, hosts: [{id, fqdn, ipAddress}], vdsSpecs: [{name, mtu}]}], "
            "pageMetadata}."
        ),
        next_step=(
            "Pass a cluster id as clusterId to sddc.vcf5.host.list for the full host "
            "detail of that cluster."
        ),
        parameter_hints={"domainId": "VCF domain id from sddc.vcf5.domain.list; omit for all."},
    ),
)

_HOST_LIST = SddcVcf5TypedOp(
    op_id="sddc.vcf5.host.list",
    handler_attr="host_list",
    summary="List the ESXi hosts of the VCF 5.x stack, with optional filters.",
    description=(
        "Lists ESXi hosts via GET /v1/hosts, for all domains or filtered by domain, "
        "cluster or status. Shows each host's FQDN, ESXi version, IPs, hardware, "
        "domain, cluster and status. The list can hold hundreds of hosts. Read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "domainId": _optional_id("Optional VCF domain id filter. Omit for all domains."),
            "clusterId": _optional_id("Optional cluster id filter. Omit for all clusters."),
            "status": {
                "type": "string",
                "enum": ["ASSIGNED", "UNASSIGNED_USEABLE", "UNASSIGNED_UNUSEABLE"],
                "description": (
                    "Optional host status filter: ASSIGNED, UNASSIGNED_USEABLE or "
                    "UNASSIGNED_UNUSEABLE. Omit for all statuses."
                ),
            },
        },
        "additionalProperties": False,
        "examples": [{"domainId": "<domain-id>", "status": "ASSIGNED"}],
    },
    response_schema=SDDC_VCF5_RESPONSE_SCHEMAS["sddc.vcf5.host.list"],
    group_key=_GROUP_INVENTORY,
    tags=("read-only", "sddc", "vcf", "host", "inventory"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=_instructions(
        when_to_call=(
            "Call to list ESXi hosts, for example to size a migration. Narrow with "
            "domainId, clusterId or status when the operator named one. "
            "status=UNASSIGNED_USEABLE lists the free hosts."
        ),
        output_shape=(
            "{elements: [{id, fqdn, esxiVersion, status, ipAddresses: [{ipAddress, "
            "type}], domain: {id, name}, cluster: {id}, networkpool: {id, name}, "
            "hardwareVendor, hardwareModel}], pageMetadata}. Large lists come back as a "
            "result handle."
        ),
        next_step=(
            "Use result_query on a handle to filter or page a large list, for example "
            "by esxiVersion."
        ),
        parameter_hints={
            "domainId": "VCF domain id; omit for all.",
            "clusterId": "cluster id; omit for all.",
            "status": "ASSIGNED / UNASSIGNED_USEABLE / UNASSIGNED_UNUSEABLE; omit for all.",
        },
    ),
)

_VCENTER_LIST = SddcVcf5TypedOp(
    op_id="sddc.vcf5.vcenter.list",
    handler_attr="vcenter_list",
    summary="List the vCenter Server instances the VCF 5.x SDDC Manager manages.",
    description=(
        "Lists the vCenters via GET /v1/vcenters, for all domains or for one domain "
        "with domainId. Shows each vCenter's FQDN, IP, version and domain. Read-only."
    ),
    parameter_schema=_DOMAIN_FILTER_PARAMS,
    response_schema=SDDC_VCF5_RESPONSE_SCHEMAS["sddc.vcf5.vcenter.list"],
    group_key=_GROUP_INVENTORY,
    tags=("read-only", "sddc", "vcf", "vcenter", "inventory"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=_instructions(
        when_to_call=("Call to list managed vCenters, or to find the vCenter of one domain."),
        output_shape=(
            "{elements: [{id, fqdn, ipAddress, version, domain: {id, name}}], pageMetadata}."
        ),
        next_step=(
            "Find the vSphere target with this fqdn (list_targets) to read its VMs, "
            "for example for a migration inventory."
        ),
        parameter_hints={"domainId": "VCF domain id; omit for all."},
    ),
)

_NSXT_CLUSTER_LIST = SddcVcf5TypedOp(
    op_id="sddc.vcf5.nsxt_cluster.list",
    handler_attr="nsxt_cluster_list",
    summary="List the NSX manager clusters the VCF 5.x SDDC Manager manages.",
    description=(
        "Lists the NSX manager clusters via GET /v1/nsxt-clusters, with the domains "
        "each one serves and its manager nodes. Use it to find which NSX cluster a "
        "workload domain uses. Read-only."
    ),
    parameter_schema=_NO_PARAMS,
    response_schema=SDDC_VCF5_RESPONSE_SCHEMAS["sddc.vcf5.nsxt_cluster.list"],
    group_key=_GROUP_INVENTORY,
    tags=("read-only", "sddc", "vcf", "nsxt", "inventory"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=_instructions(
        when_to_call=(
            "Call to list NSX clusters and the domains they serve, for example to map "
            "each workload domain to its NSX cluster."
        ),
        output_shape=(
            "{elements: [{id, vipFqdn, version, status, isShared, domains: [{id, "
            "name}], nodes: [{id, fqdn, name}]}], pageMetadata}."
        ),
        next_step="Find the NSX target with this vipFqdn (list_targets) for NSX reads.",
    ),
)

_MANAGER_LIST = SddcVcf5TypedOp(
    op_id="sddc.vcf5.manager.list",
    handler_attr="manager_list",
    summary="List the SDDC Manager appliances with their version.",
    description=(
        "Lists the SDDC Manager appliances via GET /v1/sddc-managers: FQDN, IP, "
        "version and domain. Use it to confirm which SDDC Manager runs the stack and "
        "its exact 5.x build. Read-only."
    ),
    parameter_schema=_NO_PARAMS,
    response_schema=SDDC_VCF5_RESPONSE_SCHEMAS["sddc.vcf5.manager.list"],
    group_key=_GROUP_INVENTORY,
    tags=("read-only", "sddc", "vcf", "manager", "inventory"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=_instructions(
        when_to_call=(
            "Call to find the SDDC Manager appliance and read the exact VCF version "
            "and build, for example to record a migration source."
        ),
        output_shape=(
            "{elements: [{id, fqdn, ipAddress, version, domain: {id, name}}], pageMetadata}."
        ),
        next_step=("Go on with sddc.vcf5.domain.list to read what this SDDC Manager manages."),
    ),
)

# ---------------------------------------------------------------------------
# Tasks group
# ---------------------------------------------------------------------------

_TASK_LIST = SddcVcf5TypedOp(
    op_id="sddc.vcf5.task.list",
    handler_attr="task_list",
    summary="List the VCF 5.x workflow tasks, running or recently ended.",
    description=(
        "Lists VCF workflow tasks via GET /v1/tasks, optionally filtered by status. "
        "Shows each task's name, type, state, times, errors and the objects it works "
        "on. Use it to check that nothing runs before a cutover. Read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["Successful", "Failed", "In_Progress", "Pending", "Cancelled"],
                "description": "Optional task status filter. Omit for all statuses.",
            },
        },
        "additionalProperties": False,
        "examples": [{"status": "Failed"}],
    },
    response_schema=SDDC_VCF5_RESPONSE_SCHEMAS["sddc.vcf5.task.list"],
    group_key=_GROUP_TASKS,
    tags=("read-only", "sddc", "vcf", "task", "workflow"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=_instructions(
        when_to_call=(
            "Call to check that no VCF workflow runs before a cutover, or with "
            "status='Failed' to find a failed task. Each row has its own status; "
            "check it, and drop rows in other states yourself."
        ),
        output_shape=(
            "{elements: [{id, name, type, status, creationTimestamp, "
            "completionTimestamp, subTasks: [{name, status}], errors: [{errorCode, "
            "message, remediationMessage}], resources: [{resourceId, fqdn, type}]}], "
            "pageMetadata}."
        ),
        next_step=(
            "For a failed task, report errors[].message and remediationMessage. If any "
            "task is PENDING or IN_PROGRESS, the stack is not idle yet."
        ),
        parameter_hints={
            "status": "Successful / Failed / In_Progress / Pending / Cancelled; omit for all.",
        },
    ),
)


#: The typed ops :class:`SddcVcf5Connector` registers at lifespan startup — the 7-op
#: VCF 5.x migration-inventory read core (#3059). Ordered domains -> compute ->
#: network -> appliance -> tasks to match the operator's inventory path.
SDDC_VCF5_TYPED_OPS: tuple[SddcVcf5TypedOp, ...] = (
    _DOMAIN_LIST,
    _CLUSTER_LIST,
    _HOST_LIST,
    _VCENTER_LIST,
    _NSXT_CLUSTER_LIST,
    _MANAGER_LIST,
    _TASK_LIST,
)


async def register_sddc_vcf5_typed_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert every op in :data:`SDDC_VCF5_TYPED_OPS` into ``endpoint_descriptor``.

    Queued onto the lifespan-driven registrar list via
    :func:`~meho_backplane.operations.typed_register.register_typed_op_registrar` in
    this package's ``__init__``; the runner
    (:func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`)
    invokes it after every connector subpackage has been imported, so the descriptor
    rows land before the first dispatch — the VCF 5.x read core is live on a fresh
    boot. Idempotent across pod restarts. Mirrors
    :func:`~meho_backplane.connectors.sddc_manager.typed_ops.register_sddc_typed_operations`.

    The ``embedding_service`` keyword-only parameter is the runner contract:
    :func:`run_typed_op_registrars` passes the process-wide
    :class:`EmbeddingService` (or a chassis-test stub) to every registrar, so each
    registrar must accept the kwarg. It is forwarded to
    :func:`register_typed_operation` (which falls back to the process-wide singleton
    when ``None``).
    """
    # Lazy imports: the operations package pulls in the embedding pipeline (ONNX
    # runtime + model), which pure connector/handler unit tests should not pay.
    from meho_backplane.connectors.sddc_vcf5.connector import SddcVcf5Connector
    from meho_backplane.operations.typed_register import register_typed_operation

    for op in SDDC_VCF5_TYPED_OPS:
        handler = getattr(SddcVcf5Connector, op.handler_attr, None)
        if handler is None:
            raise AttributeError(
                f"SddcVcf5Connector typed op {op.op_id!r} declares "
                f"handler_attr={op.handler_attr!r} but the class has no such attribute"
            )
        when_to_use = SDDC_VCF5_TYPED_WHEN_TO_USE_BY_GROUP.get(op.group_key)
        if when_to_use is None:
            raise ValueError(
                f"SddcVcf5Connector typed op {op.op_id!r} declares group_key="
                f"{op.group_key!r} but no curated when_to_use exists for that key. Add "
                f"an entry to SDDC_VCF5_TYPED_WHEN_TO_USE_BY_GROUP."
            )
        await register_typed_operation(
            product=SddcVcf5Connector.product,
            version=SddcVcf5Connector.version,
            impl_id=SddcVcf5Connector.impl_id,
            op_id=op.op_id,
            handler=handler,
            summary=op.summary,
            description=op.description,
            parameter_schema=op.parameter_schema,
            response_schema=op.response_schema,
            group_key=op.group_key,
            when_to_use=when_to_use,
            tags=list(op.tags),
            safety_level=op.safety_level,
            requires_approval=op.requires_approval,
            llm_instructions=op.llm_instructions,
            embedding_service=embedding_service,
        )
    _log.info(
        "sddc_vcf5_typed_operations_registered",
        count=len(SDDC_VCF5_TYPED_OPS),
        product=SddcVcf5Connector.product,
        version=SddcVcf5Connector.version,
        impl_id=SddcVcf5Connector.impl_id,
    )
