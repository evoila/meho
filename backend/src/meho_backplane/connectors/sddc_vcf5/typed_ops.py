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
    safety_level: Literal["safe", "caution", "dangerous"]
    requires_approval: bool
    llm_instructions: dict[str, Any] | None


#: Curated ``when_to_use`` blurb per typed-op group. ``register_typed_operation``
#: requires a non-empty string whenever ``group_key`` is set.
SDDC_VCF5_TYPED_WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    _GROUP_INVENTORY: (
        "Use to inventory the VCF 5.x infrastructure a migration is moving off: the "
        "workload + management domains (sddc.vcf5.domain.list), their vSphere "
        "clusters (sddc.vcf5.cluster.list), ESXi hosts (sddc.vcf5.host.list), the "
        "managed vCenters (sddc.vcf5.vcenter.list), the NSX-T manager clusters that "
        "front them (sddc.vcf5.nsxt_cluster.list), and the SDDC Manager appliance "
        "itself with its version (sddc.vcf5.manager.list). The right group for 'what "
        "does this VCF 5.x stack contain and how big is it' — the physical + logical "
        "footprint a migration must re-home. Read-only."
    ),
    _GROUP_TASKS: (
        "Use to read VCF workflow tasks (sddc.vcf5.task.list) — in-flight or recently "
        "completed operations, optionally filtered by status. The right group for "
        "confirming the source estate is quiescent before a migration cutover, or "
        "triaging a workflow that failed. Read-only."
    ),
}


# ---------------------------------------------------------------------------
# Shared parameter-schema fragments
# ---------------------------------------------------------------------------

#: The empty parameter object shared by the no-argument reads.
_NO_PARAMS: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}


def _instructions(
    *,
    when_to_use: str,
    output_shape: str,
    parameter_hints: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build the per-op ``llm_instructions`` blob with the canonical SDDC keys."""
    blob: dict[str, Any] = {"when_to_use": when_to_use, "output_shape": output_shape}
    if parameter_hints is not None:
        blob["parameter_hints"] = parameter_hints
    return blob


# ---------------------------------------------------------------------------
# Inventory group
# ---------------------------------------------------------------------------

_DOMAIN_LIST = SddcVcf5TypedOp(
    op_id="sddc.vcf5.domain.list",
    handler_attr="domain_list",
    summary="VCF 5.x domains (management + workload) SDDC Manager governs.",
    description=(
        "Lists every VCF domain the SDDC Manager governs via GET /v1/domains — the "
        "management domain plus any workload domains — the top of the VCF tenancy "
        "tree and the entry point for a migration inventory. Works with zero catalog "
        "ingest. safety_level=safe, read-only."
    ),
    parameter_schema=_NO_PARAMS,
    response_schema={"type": "object", "additionalProperties": True},
    group_key=_GROUP_INVENTORY,
    tags=("read-only", "sddc", "vcf", "domain", "inventory"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=_instructions(
        when_to_use=(
            "Call first when inventorying a VCF 5.x source estate — the management "
            "domain and any workload domains are the top of the tree and the anchor "
            "for domain-scoped cluster / host / vCenter reads."
        ),
        output_shape=(
            "{elements: [{id, name, type (MANAGEMENT/WORKLOAD), vcenters, "
            "nsxtCluster}, ...], pageMetadata}."
        ),
    ),
)

_CLUSTER_LIST = SddcVcf5TypedOp(
    op_id="sddc.vcf5.cluster.list",
    handler_attr="cluster_list",
    summary="vSphere clusters across all or one VCF 5.x domain.",
    description=(
        "Lists vSphere clusters across all VCF domains via GET /v1/clusters, or "
        "narrowed to one domain via the optional domainId filter — cluster count, "
        "datastore type, and host membership a migration must re-home. Works with "
        "zero catalog ingest. safety_level=safe, read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "domainId": {
                "type": "string",
                "minLength": 1,
                "description": "Optional VCF domain id filter. Omit for all domains.",
            },
        },
        "additionalProperties": False,
    },
    response_schema={"type": "object", "additionalProperties": True},
    group_key=_GROUP_INVENTORY,
    tags=("read-only", "sddc", "vcf", "cluster", "inventory"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=_instructions(
        when_to_use="Call to list vSphere clusters; pass domainId to scope to one domain.",
        output_shape=(
            "{elements: [{id, name, primaryDatastoreType, domainId, hosts}, ...], pageMetadata}."
        ),
        parameter_hints={"domainId": "VCF domain id from sddc.vcf5.domain.list; omit for all."},
    ),
)

_HOST_LIST = SddcVcf5TypedOp(
    op_id="sddc.vcf5.host.list",
    handler_attr="host_list",
    summary="ESXi hosts across the VCF 5.x estate, optionally filtered.",
    description=(
        "Enumerates ESXi hosts across all VCF domains via GET /v1/hosts, or narrowed "
        "by the optional domainId / clusterId / status filters — the physical "
        "footprint (host count, FQDN, ESXi version, assignment status) a migration "
        "must size. Large estates return hundreds of hosts, reduced to a JSONFlux "
        "handle. Works with zero catalog ingest. safety_level=safe, read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "domainId": {
                "type": "string",
                "minLength": 1,
                "description": "Optional VCF domain id filter. Omit for all domains.",
            },
            "clusterId": {
                "type": "string",
                "minLength": 1,
                "description": "Optional cluster id filter. Omit for all clusters.",
            },
            "status": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Optional host status filter (e.g. ASSIGNED, UNASSIGNED_USEABLE). "
                    "Omit for all statuses."
                ),
            },
        },
        "additionalProperties": False,
    },
    response_schema={"type": "object", "additionalProperties": True},
    group_key=_GROUP_INVENTORY,
    tags=("read-only", "sddc", "vcf", "host", "inventory"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=_instructions(
        when_to_use=(
            "Call to enumerate ESXi hosts — the migration's physical footprint. "
            "Narrow with domainId / clusterId / status when the operator named one; "
            "aggregate over the handle for host/version counts."
        ),
        output_shape=(
            "{elements: [{id, fqdn, esxiVersion, ipAddresses, domain, cluster, "
            "status}, ...], pageMetadata}."
        ),
        parameter_hints={
            "domainId": "VCF domain id; omit for all.",
            "clusterId": "cluster id; omit for all.",
            "status": "ASSIGNED / UNASSIGNED_USEABLE; omit for all.",
        },
    ),
)

_VCENTER_LIST = SddcVcf5TypedOp(
    op_id="sddc.vcf5.vcenter.list",
    handler_attr="vcenter_list",
    summary="vCenter Server instances the VCF 5.x SDDC Manager manages.",
    description=(
        "Lists the vCenter Server instances SDDC Manager manages via GET /v1/vcenters, "
        "or narrowed to one domain via the optional domainId filter. Cross the "
        "returned fqdn against the vSphere connector for VM-level migration inventory. "
        "Works with zero catalog ingest. safety_level=safe, read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "domainId": {
                "type": "string",
                "minLength": 1,
                "description": "Optional VCF domain id filter. Omit for all domains.",
            },
        },
        "additionalProperties": False,
    },
    response_schema={"type": "object", "additionalProperties": True},
    group_key=_GROUP_INVENTORY,
    tags=("read-only", "sddc", "vcf", "vcenter", "inventory"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=_instructions(
        when_to_use=(
            "Call to list managed vCenters, then cross each fqdn against the vSphere "
            "connector to inventory the VMs a migration will move."
        ),
        output_shape="{elements: [{id, fqdn, domain}, ...], pageMetadata}.",
        parameter_hints={"domainId": "VCF domain id; omit for all."},
    ),
)

_NSXT_CLUSTER_LIST = SddcVcf5TypedOp(
    op_id="sddc.vcf5.nsxt_cluster.list",
    handler_attr="nsxt_cluster_list",
    summary="NSX-T manager clusters the VCF 5.x SDDC Manager manages.",
    description=(
        "Lists the NSX-T manager clusters SDDC Manager manages via GET /v1/nsxt-clusters "
        "and the domains each one backs — the network fabric a migration must re-home. "
        "Works with zero catalog ingest. safety_level=safe, read-only."
    ),
    parameter_schema=_NO_PARAMS,
    response_schema={"type": "object", "additionalProperties": True},
    group_key=_GROUP_INVENTORY,
    tags=("read-only", "sddc", "vcf", "nsxt", "inventory"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=_instructions(
        when_to_use=(
            "Call to map the NSX-T fabric — which NSX-T cluster fronts each workload "
            "domain — so the target-side network design covers every domain."
        ),
        output_shape="{elements: [{id, vipFqdn, domainIds}, ...], pageMetadata}.",
    ),
)

_MANAGER_LIST = SddcVcf5TypedOp(
    op_id="sddc.vcf5.manager.list",
    handler_attr="manager_list",
    summary="SDDC Manager appliance inventory + version.",
    description=(
        "Lists the SDDC Manager appliances via GET /v1/sddc-managers — their FQDN, "
        "IP, version, and management domain. Confirms which SDDC Manager fronts the "
        "VCF stack and its exact 5.x build; the same surface the connector's "
        "fingerprint reads, exposed as an operator-callable typed op. Works with zero "
        "catalog ingest. safety_level=safe, read-only."
    ),
    parameter_schema=_NO_PARAMS,
    response_schema={"type": "object", "additionalProperties": True},
    group_key=_GROUP_INVENTORY,
    tags=("read-only", "sddc", "vcf", "manager", "inventory"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=_instructions(
        when_to_use=(
            "Call to identify the SDDC Manager appliance(s) and read the exact VCF "
            "version/build — the anchor for the migration source record."
        ),
        output_shape="{elements: [{id, fqdn, ipAddress, version, domain}, ...], pageMetadata}.",
    ),
)

# ---------------------------------------------------------------------------
# Tasks group
# ---------------------------------------------------------------------------

_TASK_LIST = SddcVcf5TypedOp(
    op_id="sddc.vcf5.task.list",
    handler_attr="task_list",
    summary="In-flight or recent VCF 5.x workflow tasks, optionally filtered.",
    description=(
        "Lists in-flight or recently completed VCF workflow tasks via GET /v1/tasks, "
        "optionally narrowed by the status filter (Successful / Failed / In_Progress "
        "/ Pending / Cancelled). The read for confirming the source estate is "
        "quiescent before a migration cutover, or triaging a workflow that failed. "
        "Works with zero catalog ingest. safety_level=safe, read-only."
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
    },
    response_schema={"type": "object", "additionalProperties": True},
    group_key=_GROUP_TASKS,
    tags=("read-only", "sddc", "vcf", "task", "workflow"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=_instructions(
        when_to_use=(
            "Call to confirm no VCF workflow is in flight before a migration cutover "
            "(quiescence), or narrow with status='Failed' when triaging."
        ),
        output_shape=(
            "{elements: [{id, name, status, type, subtasks, errors}, ...], "
            "pageMetadata}. For a failed task surface errors[].message."
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
