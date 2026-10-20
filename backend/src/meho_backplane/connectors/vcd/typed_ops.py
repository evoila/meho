# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed-op metadata + registrar for :class:`VcloudDirectorConnector` (#3057).

The VMware Cloud Director migration-inventory read core is registered as typed
ops (``source_kind="typed"``) so it dispatches on a fresh boot with **zero
catalog ingest** — the read core is live the moment the connector loads (part of
initiative `evoila/meho#3056 <https://github.com/evoila/meho/issues/3056>`_,
legacy migration-source connector coverage).

The op bodies live in :mod:`meho_backplane.connectors.vcd.typed_reads`; the
per-op ``llm_instructions`` in
:mod:`meho_backplane.connectors.vcd._llm_instructions`; thin bound-method shims
on :class:`~meho_backplane.connectors.vcd.connector.VcloudDirectorConnector`
expose them under the ``handler_attr`` names below so the dispatcher's
:func:`~meho_backplane.operations._handler_resolve.import_handler` walk recovers
each callable from its persisted ``module.ClassName.method`` path.

The dataclass + tuple + module-level registrar shape mirrors
:mod:`meho_backplane.connectors.fleet_lcm.typed_ops` (#3047) and
:mod:`meho_backplane.connectors.harbor.typed_ops` (#2856) — the Task #2358
convention: typed ops own the curated read core. A write surface (create org /
vApp / deploy) is deliberately out of scope — this is a read core for
inventorying a migration source.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

import structlog

from meho_backplane.connectors.vcd._llm_instructions import VCD_READ_LLM_INSTRUCTIONS

if TYPE_CHECKING:
    from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "VCD_TYPED_OPS",
    "VCD_TYPED_WHEN_TO_USE_BY_GROUP",
    "VcloudDirectorTypedOp",
    "register_vcd_typed_operations",
]

_log = structlog.get_logger(__name__)

# Typed-op group keys — the three curated groups the vCD read core spans,
# ordered inventory -> networking -> tasks to match the operator's migration
# drill path ("what workloads are here, how do they network, what's happening").
_GROUP_INVENTORY = "vcd-inventory"
_GROUP_NETWORKING = "vcd-networking"
_GROUP_TASKS = "vcd-tasks"


@dataclass(frozen=True)
class VcloudDirectorTypedOp:
    """Metadata for one vCD typed op registered at lifespan startup.

    Fields mirror the keyword arguments
    :func:`~meho_backplane.operations.typed_register.register_typed_operation`
    accepts so :func:`register_vcd_typed_operations` can splat the dataclass into
    the helper without per-op boilerplate. ``handler_attr`` is the attribute name
    on :class:`VcloudDirectorConnector` exposing the async handler; the registrar
    resolves the bound method against the class at registration time. Mirrors
    :class:`~meho_backplane.connectors.fleet_lcm.typed_ops.FleetLcmTypedOp`.
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


#: Curated ``when_to_use`` blurb per typed-op group. Every entry is a complete
#: sentence the agent reads verbatim through ``list_operation_groups`` to pick a
#: group to search within; ``register_typed_operation`` requires a non-empty
#: string whenever ``group_key`` is set.
VCD_TYPED_WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    _GROUP_INVENTORY: (
        "Use this group to inventory the VMware Cloud Director estate an operator "
        "is migrating off: organizations, their org VDCs (compute/storage "
        "allocations), vApps, virtual machines, and catalogs. A provider session "
        "sees every org, so this is the primary 'what workloads exist and how "
        "big are they' surface for migration sizing and wave planning."
    ),
    _GROUP_NETWORKING: (
        "Use this group to read the vCD networking edges — the edge gateways that "
        "provide each org VDC's egress and network services (NAT, firewall, VPN). "
        "Use when mapping the source network topology a migration must re-home "
        "onto the target."
    ),
    _GROUP_TASKS: (
        "Use this group to read the vCD instance's recent asynchronous tasks — "
        "what it is doing or just did. Use to confirm the estate is quiescent "
        "before a migration cutover, or to find the task behind a resource in an "
        "unexpected state."
    ),
}


# ---------------------------------------------------------------------------
# Shared schema fragments
# ---------------------------------------------------------------------------

#: Empty parameter object shared by every read (all are no-argument list ops —
#: the provider session is estate-wide, so no per-call selector is needed). The
#: one example is the empty call.
_NO_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
    "examples": [{}],
}


def _field(type_: str, description: str) -> dict[str, Any]:
    """Return one described response field."""
    return {"type": type_, "description": description}


def _record_envelope(record_type: str, record_fields: dict[str, Any]) -> dict[str, Any]:
    """Return the classic query-service ``{record: [...], total, page, pageSize}`` schema."""
    return {
        "type": "object",
        "properties": {
            "record": {
                "type": "array",
                "description": f"One {record_type} per row.",
                "items": {
                    "type": "object",
                    "properties": record_fields,
                    "additionalProperties": True,
                },
            },
            "total": _field("integer", "How many rows match in total, across all pages."),
            "page": _field("integer", "The page number of this answer."),
            "pageSize": _field("integer", "How many rows one page holds."),
        },
        "additionalProperties": True,
    }


_NAME = _field("string", "The name of the object.")
_ORG_NAME = _field("string", "The name of the organization that owns it.")
_ORG_REF = _field(
    "string", "The organization that owns it, as a reference or id. It is not the name."
)

_READ_TAGS = ("read-only", "vcd", "vcloud-director")


#: The typed ops :class:`VcloudDirectorConnector` registers at lifespan startup —
#: the 7-op migration-inventory read core (#3057). Every op is safe, read-only,
#: and ungated; the wire path/type lives in the handler (see ``.typed_reads`` /
#: ``._paths``), so the descriptor rows carry ``method=None`` / ``path=None``
#: (typed convention).
VCD_TYPED_OPS: tuple[VcloudDirectorTypedOp, ...] = (
    # ---- Inventory ----
    VcloudDirectorTypedOp(
        op_id="vcd.org.list",
        handler_attr="org_list",
        summary="List the organizations in the vCD instance.",
        description=(
            "Lists the organizations (GET /cloudapi/1.0.0/orgs), the top of the vCD tenant tree. A"
            " provider session sees every org. Each org has a urn:vcloud:org: id, name, "
            "displayName and isEnabled. Read-only."
        ),
        parameter_schema=_NO_PARAMS,
        response_schema={
            "type": "object",
            "properties": {
                "values": {
                    "type": "array",
                    "description": "One organization per entry.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": _field("string", "The org id, a urn:vcloud:org: URN."),
                            "name": _NAME,
                            "displayName": _field("string", "The name shown in the UI."),
                            "isEnabled": _field("boolean", "Whether the org is enabled."),
                        },
                        "additionalProperties": True,
                    },
                },
                "resultTotal": _field("integer", "How many orgs exist in total."),
                "pageCount": _field("integer", "How many pages the result has."),
                "page": _field("integer", "The page number of this answer."),
                "pageSize": _field("integer", "How many entries one page holds."),
            },
            "additionalProperties": True,
        },
        group_key=_GROUP_INVENTORY,
        tags=(*_READ_TAGS, "org"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=VCD_READ_LLM_INSTRUCTIONS["vcd.org.list"],
    ),
    VcloudDirectorTypedOp(
        op_id="vcd.vdc.list",
        handler_attr="vdc_list",
        summary="List the organization VDCs (compute/storage allocations).",
        description=(
            "Lists the organization VDCs with the query service (type=adminOrgVdc): the compute "
            "and storage each org has. Each row has name, orgName, providerVdcName and the vApp "
            "and VM counts. Read-only."
        ),
        parameter_schema=_NO_PARAMS,
        response_schema=_record_envelope(
            "AdminOrgVdcRecord",
            {
                "name": _NAME,
                "orgName": _ORG_NAME,
                "providerVdcName": _field("string", "The provider VDC that backs this VDC."),
                "numberOfVApps": _field("integer", "How many vApps the VDC holds."),
                "numberOfVMs": _field("integer", "How many VMs the VDC holds."),
            },
        ),
        group_key=_GROUP_INVENTORY,
        tags=(*_READ_TAGS, "vdc"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=VCD_READ_LLM_INSTRUCTIONS["vcd.vdc.list"],
    ),
    VcloudDirectorTypedOp(
        op_id="vcd.vapp.list",
        handler_attr="vapp_list",
        summary="List the vApps (deployable multi-VM units).",
        description=(
            "Lists the vApps with the query service (type=adminVApp): groups of VMs that move "
            "together in a migration. Each row has name, org (a reference or id, not the name), "
            "vdcName, status, numberOfVMs and isDeployed. Read-only."
        ),
        parameter_schema=_NO_PARAMS,
        response_schema=_record_envelope(
            "AdminVAppRecord",
            {
                "name": _NAME,
                "org": _ORG_REF,
                "vdcName": _field("string", "The VDC the vApp lives in."),
                "status": _field("string", "The vApp state, for example POWERED_ON."),
                "numberOfVMs": _field("integer", "How many VMs the vApp holds."),
                "isDeployed": _field("boolean", "Whether the vApp is deployed."),
            },
        ),
        group_key=_GROUP_INVENTORY,
        tags=(*_READ_TAGS, "vapp"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=VCD_READ_LLM_INSTRUCTIONS["vcd.vapp.list"],
    ),
    VcloudDirectorTypedOp(
        op_id="vcd.vm.list",
        handler_attr="vm_list",
        summary="List the virtual machines across the estate.",
        description=(
            "Lists every VM with the query service (type=adminVM): the main migration inventory. "
            "Each row has name, containerName (the vApp), vdc, org, status, numberOfCpus, memoryMB"
            " and guestOs. Read-only."
        ),
        parameter_schema=_NO_PARAMS,
        response_schema=_record_envelope(
            "AdminVMRecord",
            {
                "name": _NAME,
                "containerName": _field("string", "The vApp the VM belongs to."),
                "vdc": _field("string", "The VDC the VM lives in."),
                "org": _field("string", "The organization that owns the VM."),
                "status": _field("string", "The VM state, for example POWERED_ON."),
                "numberOfCpus": _field("integer", "How many virtual CPUs the VM has."),
                "memoryMB": _field("integer", "The VM memory in MB."),
                "guestOs": _field("string", "The guest operating system."),
            },
        ),
        group_key=_GROUP_INVENTORY,
        tags=(*_READ_TAGS, "vm"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=VCD_READ_LLM_INSTRUCTIONS["vcd.vm.list"],
    ),
    VcloudDirectorTypedOp(
        op_id="vcd.catalog.list",
        handler_attr="catalog_list",
        summary="List the catalogs (vApp-template + media libraries).",
        description=(
            "Lists the catalogs with the query service (type=adminCatalog): the vApp template and "
            "media libraries. Each row has name, orgName, isShared, isPublished and the template "
            "and media counts. Read-only."
        ),
        parameter_schema=_NO_PARAMS,
        response_schema=_record_envelope(
            "AdminCatalogRecord",
            {
                "name": _NAME,
                "orgName": _ORG_NAME,
                "isShared": _field("boolean", "Whether the catalog is shared."),
                "isPublished": _field("boolean", "Whether the catalog is published to other orgs."),
                "numberOfTemplates": _field("integer", "How many vApp templates it holds."),
                "numberOfMedia": _field("integer", "How many media files (ISO) it holds."),
            },
        ),
        group_key=_GROUP_INVENTORY,
        tags=(*_READ_TAGS, "catalog"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=VCD_READ_LLM_INSTRUCTIONS["vcd.catalog.list"],
    ),
    # ---- Networking ----
    VcloudDirectorTypedOp(
        op_id="vcd.edge-gateway.list",
        handler_attr="edge_gateway_list",
        summary="List the edge gateways (org-VDC network edges).",
        description=(
            "Lists the edge gateways with the query service (type=edgeGateway): the network edge "
            "(NAT, firewall, VPN) of each org VDC. Each row has name, vdc, orgName and the network"
            " counts. Read-only."
        ),
        parameter_schema=_NO_PARAMS,
        response_schema=_record_envelope(
            "EdgeGatewayRecord",
            {
                "name": _NAME,
                "vdc": _field("string", "The VDC the edge gateway serves."),
                "orgName": _ORG_NAME,
                "numberOfExtNetworks": _field("integer", "How many external networks it uses."),
                "numberOfOrgNetworks": _field("integer", "How many org networks it serves."),
            },
        ),
        group_key=_GROUP_NETWORKING,
        tags=(*_READ_TAGS, "edge-gateway", "network"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=VCD_READ_LLM_INSTRUCTIONS["vcd.edge-gateway.list"],
    ),
    # ---- Tasks ----
    VcloudDirectorTypedOp(
        op_id="vcd.task.list",
        handler_attr="task_list",
        summary="List recent asynchronous tasks.",
        description=(
            "Lists recent tasks with the query service (type=adminTask): what vCD does or just "
            "did. Each row has name, status, objectName, orgName, startDate, endDate and "
            "ownerName. Read-only."
        ),
        parameter_schema=_NO_PARAMS,
        response_schema=_record_envelope(
            "AdminTaskRecord",
            {
                "name": _field("string", "The operation the task runs."),
                "status": _field("string", "The task state, for example running, success, error."),
                "objectName": _field("string", "The object the task works on."),
                "orgName": _ORG_NAME,
                "startDate": _field("string", "When the task started."),
                "endDate": _field("string", "When the task ended."),
                "ownerName": _field("string", "Who started the task."),
            },
        ),
        group_key=_GROUP_TASKS,
        tags=(*_READ_TAGS, "task"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=VCD_READ_LLM_INSTRUCTIONS["vcd.task.list"],
    ),
)


async def register_vcd_typed_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert every op in :data:`VCD_TYPED_OPS` into ``endpoint_descriptor``.

    Queued onto the lifespan-driven registrar list via
    :func:`~meho_backplane.operations.typed_register.register_typed_op_registrar`
    in this package's ``__init__``; the runner
    (:func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`)
    invokes it after every connector subpackage has been imported, so the
    descriptor rows land before the first dispatch — the vCD read core is live on
    a fresh boot. Idempotent across pod restarts (the helper skips the embedding
    recompute on unchanged summary / description / tags). Mirrors
    :func:`register_fleet_lcm_typed_operations`.

    The ``embedding_service`` keyword-only parameter is the runner contract:
    :func:`run_typed_op_registrars` passes the process-wide
    :class:`EmbeddingService` (or a chassis-test stub) to every registrar, so
    each registrar must accept the kwarg. It is forwarded to
    :func:`register_typed_operation` (which falls back to the process-wide
    singleton when ``None``).
    """
    # Lazy imports: the operations package pulls in the embedding pipeline
    # (ONNX runtime + model), which pure connector/handler unit tests should not
    # pay. Lifespan callers have it warmed by the time this runs.
    from meho_backplane.connectors.vcd.connector import VcloudDirectorConnector
    from meho_backplane.operations.typed_register import register_typed_operation

    for op in VCD_TYPED_OPS:
        handler = getattr(VcloudDirectorConnector, op.handler_attr, None)
        if handler is None:
            raise AttributeError(
                f"VcloudDirectorConnector typed op {op.op_id!r} declares "
                f"handler_attr={op.handler_attr!r} but the class has no such attribute"
            )
        when_to_use = VCD_TYPED_WHEN_TO_USE_BY_GROUP.get(op.group_key)
        if when_to_use is None:
            raise ValueError(
                f"VcloudDirectorConnector typed op {op.op_id!r} declares "
                f"group_key={op.group_key!r} but no curated when_to_use exists for that "
                f"key. Add an entry to VCD_TYPED_WHEN_TO_USE_BY_GROUP."
            )
        await register_typed_operation(
            product=VcloudDirectorConnector.product,
            version=VcloudDirectorConnector.version,
            impl_id=VcloudDirectorConnector.impl_id,
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
        "vcd_typed_operations_registered",
        count=len(VCD_TYPED_OPS),
        product=VcloudDirectorConnector.product,
        version=VcloudDirectorConnector.version,
        impl_id=VcloudDirectorConnector.impl_id,
    )
