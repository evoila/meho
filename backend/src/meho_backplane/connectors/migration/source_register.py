# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Registrar of the source adapters: one ``<product>.migration.inventory`` per connector.

Each adapter is a multi-step operation registered on its own connector's
natural key, in the group ``migration``. All of them only read, so they are
``safe`` and set ``requires_approval=False`` on purpose
(:func:`register_composite_operation` defaults to ``dangerous`` with
approval). They all answer in one shape (:data:`INVENTORY_ANSWER_SCHEMA`):
the handle of the stored inventory plus the counts and omissions.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Final, NamedTuple

from meho_backplane.connectors.migration.source_hosts import (
    linux_migration_inventory,
    winsrv_migration_inventory,
)
from meho_backplane.connectors.migration.source_hyperv import hyperv_migration_inventory
from meho_backplane.connectors.migration.source_k8s import k8s_migration_inventory
from meho_backplane.connectors.migration.source_proxmox import proxmox_migration_inventory
from meho_backplane.operations.typed_register import register_composite_operation

if TYPE_CHECKING:
    from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "INVENTORY_ANSWER_SCHEMA",
    "SOURCE_ADAPTERS",
    "SourceAdapter",
    "register_source_adapters",
]

_GROUP_KEY: Final[str] = "migration"
_GROUP_WHEN_TO_USE: Final[str] = (
    "Use to collect this source's machines or workloads for a migration "
    "assessment, in MEHO's common inventory shape. Read-only. The answer is "
    "the handle of a stored inventory, which migration.inventory.csv and the "
    "assessment steps read."
)

_COUNTS: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": {"type": "integer"},
    "description": "Number per record kind: server, application, kubernetes_workload.",
}

INVENTORY_ANSWER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "inventory_handle": {
            "type": "string",
            "description": "Handle id of the stored inventory. Pass it to migration.inventory.csv.",
        },
        "expires_at": {"type": "string", "description": "When the stored inventory expires."},
        "connector_id": {"type": "string", "description": "Connector that read the source."},
        "target": {"type": "string", "description": "Target name."},
        "collected_at": {"type": "string", "description": "When the collection finished."},
        "record_counts": {**_COUNTS, "description": "Records in the inventory, per kind."},
        "source_totals": {**_COUNTS, "description": "The source's own total, per kind."},
        "left_out_items": {**_COUNTS, "description": "Whole items left out, per kind."},
        "counts_match": {
            "type": "boolean",
            "description": (
                "True only when the source's own total was read in full for every "
                "record kind and records plus left-out items equal it. False when a "
                "total could not be read, or when the counts differ."
            ),
        },
        "omission_count": {"type": "integer", "description": "Number of omissions."},
        "omissions": {
            "type": "array",
            "description": "What was not collected, and why.",
            "items": {
                "type": "object",
                "properties": {
                    "what": {"type": "string"},
                    "reason": {"type": "string"},
                    "detail": {"type": ["string", "null"]},
                },
            },
        },
    },
    "required": ["inventory_handle", "counts_match", "omissions"],
}

_PARAMETER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
    "examples": [{}],
}

_RESULT_SCALARS: Final[list[str]] = [
    "inventory_handle",
    "expires_at",
    "connector_id",
    "target",
    "collected_at",
    "counts_match",
    "omission_count",
]

#: The record kinds of the counts dicts, so the reducer keeps the three count
#: dicts inline and with the rows when a long ``omissions`` list makes the
#: answer larger than the inline limit (they are objects, not scalars).
_COUNT_KINDS: Final[list[str]] = ["server", "application", "kubernetes_workload"]
_RESULT_OBJECTS: Final[dict[str, Any]] = {
    "objects": {
        "record_counts": list(_COUNT_KINDS),
        "source_totals": list(_COUNT_KINDS),
        "left_out_items": list(_COUNT_KINDS),
    }
}

_NEXT_STEP: Final[str] = (
    "Pass inventory_handle to migration.inventory.csv for an Azure Migrate "
    "import file, or read the records with result_query (path '/records'). "
    "The handle lasts one hour. Show the omissions to the user."
)


class SourceAdapter(NamedTuple):
    """One adapter: where it is registered and what it tells the AI."""

    product: str
    version: str
    impl_id: str
    op_id: str
    handler: Callable[..., Awaitable[dict[str, Any]]]
    summary: str
    description: str
    when_to_call: str


SOURCE_ADAPTERS: Final[tuple[SourceAdapter, ...]] = (
    SourceAdapter(
        product="hyperv",
        version="2022.x",
        impl_id="hyperv-ssh",
        op_id="hyperv.migration.inventory",
        handler=hyperv_migration_inventory,
        summary="Collect every VM of this Hyper-V host as a migration inventory.",
        description=(
            "Reads the host's VMs, their configuration, disks, checkpoints and guest "
            "OS, and stores one server record per VM in MEHO's common inventory "
            "shape. Returns the inventory handle, counts that are checked against "
            "Hyper-V's own VM count, and omissions. Read-only."
        ),
        when_to_call=(
            "Call to collect a Hyper-V host's VMs for a migration assessment. For a "
            "cluster, call it once per node; the VM id keeps each VM to one record "
            "when the inventories are merged."
        ),
    ),
    SourceAdapter(
        product="proxmox",
        version="8.x",
        impl_id="proxmox-api",
        op_id="proxmox.migration.inventory",
        handler=proxmox_migration_inventory,
        summary="Collect every QEMU VM of this Proxmox VE target as a migration inventory.",
        description=(
            "Reads the nodes, their QEMU VMs, each VM's configuration and snapshots, "
            "and the guest agent's OS info, and stores one server record per VM in "
            "MEHO's common inventory shape. Returns the inventory handle, checked "
            "counts and omissions. Read-only."
        ),
        when_to_call=(
            "Call to collect a Proxmox VE cluster's virtual machines for a "
            "migration assessment. Templates are counted and left out."
        ),
    ),
    SourceAdapter(
        product="linux",
        version="1.x",
        impl_id="linux-ssh",
        op_id="linux.migration.inventory",
        handler=linux_migration_inventory,
        summary="Collect this Linux host as one server record of a migration inventory.",
        description=(
            "Reads the host's OS, CPUs, memory, whole disks, boot type and BIOS "
            "UUID with read-only file reads, and stores one server record in "
            "MEHO's common inventory shape. Returns the inventory handle, counts "
            "and omissions. Read-only."
        ),
        when_to_call=(
            "Call for a physical Linux server, or a Linux VM on a platform MEHO "
            "does not read, that belongs in a migration assessment."
        ),
    ),
    SourceAdapter(
        product="winsrv",
        version="2022.x",
        impl_id="winsrv-ssh",
        op_id="winsrv.migration.inventory",
        handler=winsrv_migration_inventory,
        summary="Collect this Windows Server host as one server record of a migration inventory.",
        description=(
            "Reads the host's OS, processors, memory, disks, firmware type and "
            "product UUID, and stores one server record in MEHO's common inventory "
            "shape. Returns the inventory handle, counts and omissions. Read-only."
        ),
        when_to_call=(
            "Call for a physical Windows server, or a Windows VM on a platform MEHO "
            "does not read, that belongs in a migration assessment."
        ),
    ),
    SourceAdapter(
        product="k8s",
        version="1.x",
        impl_id="k8s",
        op_id="k8s.migration.inventory",
        handler=k8s_migration_inventory,
        summary="Collect this cluster's workloads and nodes as a migration inventory.",
        description=(
            "Reads nodes, Deployments, StatefulSets, DaemonSets, volume claims and "
            "pod placement, and stores one workload record per workload and one "
            "server record per node, linked by node. Returns the inventory handle, "
            "checked counts and omissions. Read-only."
        ),
        when_to_call=(
            "Call to collect a Kubernetes cluster's workloads for a migration "
            "assessment. Nodes carry their BIOS UUID, so a node that is a VM "
            "matches the hypervisor's record and is not counted twice."
        ),
    ),
)


def _llm_instructions(adapter: SourceAdapter) -> dict[str, Any]:
    return {
        "when_to_call": adapter.when_to_call,
        "parameter_hints": {},
        "output_shape": (
            "{'inventory_handle', 'expires_at', 'connector_id', 'target', "
            "'collected_at', 'record_counts': {kind: n}, 'source_totals': {kind: n}, "
            "'left_out_items': {kind: n}, 'counts_match': <bool>, 'omission_count', "
            "'omissions': [{what, reason, detail}]}. Unknown values in the records "
            "are null with a reason, never 0. counts_match is false when a source "
            "total could not be read in full."
        ),
        "next_step": _NEXT_STEP,
        "result_scalars": {"keys": list(_RESULT_SCALARS)},
        "result_objects": _RESULT_OBJECTS,
    }


async def register_source_adapters(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert the five source adapters into ``endpoint_descriptor``. Safe to run twice."""
    for adapter in SOURCE_ADAPTERS:
        await register_composite_operation(
            product=adapter.product,
            version=adapter.version,
            impl_id=adapter.impl_id,
            op_id=adapter.op_id,
            handler=adapter.handler,
            summary=adapter.summary,
            description=adapter.description,
            parameter_schema=_PARAMETER_SCHEMA,
            response_schema=INVENTORY_ANSWER_SCHEMA,
            group_key=_GROUP_KEY,
            when_to_use=_GROUP_WHEN_TO_USE,
            tags=["migration", "inventory", "read-only", adapter.product],
            safety_level="safe",
            requires_approval=False,
            llm_instructions=_llm_instructions(adapter),
            embedding_service=embedding_service,
        )
