# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ``migration.assess.readiness`` operation.

It reads the caller's stored inventories and checks every server against the
rules of :mod:`.readiness_rules`. It runs no other operation and calls no
source system, so it is a typed operation, ``safe``, with
``requires_approval=False`` on purpose.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.migration.contracts import (
    AssessmentInputs,
    AssessmentItem,
    AssessmentResult,
    Assumptions,
    InventoryInput,
    ServerRecord,
    Unknown,
    assessment_result_json_schema,
)
from meho_backplane.connectors.migration.inventory_load import (
    MAX_INVENTORY_HANDLES,
    LoadedInventory,
    load_inventories,
)
from meho_backplane.connectors.migration.readiness_checks import assess_server
from meho_backplane.connectors.migration.readiness_rules import CALCULATION_VERSION
from meho_backplane.operations.typed_register import register_typed_operation

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "MIGRATION_ASSESS_READINESS_OP_ID",
    "MIGRATION_ASSESS_READINESS_PARAMETER_SCHEMA",
    "MIGRATION_ASSESS_READINESS_RESPONSE_SCHEMA",
    "migration_assess_readiness",
    "register_readiness_operation",
]

MIGRATION_ASSESS_READINESS_OP_ID: Final[str] = "migration.assess.readiness"

_STATES: Final[tuple[str, ...]] = ("ready", "ready_with_conditions", "not_ready", "unknown")

_EXAMPLE_HANDLE: Final[str] = "3f2b8c1e-6a4d-4e2f-9b7a-1c5d8e9f0a21"

MIGRATION_ASSESS_READINESS_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "inventory_handles": {
            "type": "array",
            "items": {"type": "string", "format": "uuid"},
            "minItems": 1,
            "maxItems": MAX_INVENTORY_HANDLES,
            "description": (
                "Handle ids of stored inventories, as returned by the inventory reads. "
                "Several handles are merged in the given order."
            ),
        },
        "region": {
            "type": "string",
            "pattern": "^[a-z0-9]+$",
            "description": (
                "Azure region name, for example 'westeurope'. Only recorded in the "
                "assumptions; the rules do not depend on the region."
            ),
        },
    },
    "required": ["inventory_handles"],
    "additionalProperties": False,
    "examples": [{"inventory_handles": [_EXAMPLE_HANDLE], "region": "westeurope"}],
}


def _response_schema() -> dict[str, Any]:
    schema = assessment_result_json_schema()
    properties = schema["properties"]
    properties["counts"] = {
        "type": "object",
        "description": "Servers per readiness state.",
        "properties": {state: {"type": "integer"} for state in _STATES},
    }
    properties["skipped_records"] = {
        "type": "object",
        "description": "Records that are not servers, per kind. They are not checked.",
        "additionalProperties": {"type": "integer"},
    }
    properties["duplicates"] = {
        "type": "array",
        "description": "Records of a server already checked through another record.",
        "items": {
            "type": "object",
            "properties": {
                "source_id": {"type": "string"},
                "same_as": {"type": "string"},
                "match": {"type": "string", "enum": ["source_id", "bios_uuid"]},
            },
        },
    }
    schema["required"] = [*schema.get("required", []), "counts", "skipped_records", "duplicates"]
    return schema


MIGRATION_ASSESS_READINESS_RESPONSE_SCHEMA: dict[str, Any] = _response_schema()

# The same group text as the other assessment operations: the group row is shared.
_GROUP_KEY: Final[str] = "assessment"
_GROUP_WHEN_TO_USE: Final[str] = (
    "Turn stored estate inventories into an Azure migration assessment: VM sizes, "
    "readiness and monthly cost estimates. Every number shows its inputs and "
    "assumptions. These operations only read."
)

_SUMMARY: Final[str] = "Check if each server can move to an Azure VM, and on what conditions."

_DESCRIPTION: Final[str] = (
    "Reads stored inventories and gives every server a readiness state: ready, ready "
    "with conditions, not ready or unknown. Checks the OS, the boot type, disk, core "
    "and memory limits, and shared, raw-mapped and independent disks and snapshots. "
    "Read-only."
)

_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "You have inventory handles and need to know which servers can move to Azure "
        "VMs as they are, which need work first, and which cannot move."
    ),
    "parameter_hints": {
        "inventory_handles": (
            f"Required. 1 to {MAX_INVENTORY_HANDLES} handle ids from the inventory reads. "
            "A stored result lasts one hour."
        ),
        "region": "Optional Azure region name; it is only recorded.",
    },
    "output_shape": (
        "The assessment result: 'items' (per server 'readiness': {state, findings: "
        "[{flag, state, reason, inputs, source_title, source_url, source_checked_on}]} "
        "and 'flags', the flags that are not ready), 'unknowns', 'omissions', "
        "'assumptions', 'inputs', 'counts' per state, 'skipped_records', 'duplicates'."
    ),
    "next_step": (
        "Show the counts first, then the not-ready and unknown servers with their "
        "reasons and sources. Unknown means data is missing: run the inventory read "
        "with more access, not a guess. A large result comes back as a handle; read "
        "'items' with result_query."
    ),
}

_ASSUMPTIONS: Final[dict[str, str]] = {
    "os_disk": (
        "The inventory does not say which disk is the OS disk. The OS disk limit is "
        "checked only when every disk or no disk is over it; otherwise the server is unknown."
    ),
    "disk_units": "Disk sizes in GB are compared with Microsoft's limits; 1 TB = 1,024 GB.",
    "memory": "Configured memory is checked, not assigned memory.",
}


def _inputs(loaded: LoadedInventory, generated_at: datetime) -> AssessmentInputs:
    return AssessmentInputs(
        inventories=[
            InventoryInput(
                handle_id=handle_id,
                connector_id=envelope.source.connector_id,
                target=envelope.source.target,
                collected_at=envelope.source.collected_at,
            )
            for handle_id, envelope in zip(loaded.handle_ids, loaded.envelopes, strict=True)
        ],
        generated_at=generated_at,
    )


def _first_records(loaded: LoadedInventory) -> tuple[list[ServerRecord], Counter[str]]:
    """Servers to check, once per machine, and the count of other records per kind.

    A record whose ``source_id`` or BIOS UUID was seen before is left out; the
    loader lists it in ``duplicates``.
    """
    servers: list[ServerRecord] = []
    skipped: Counter[str] = Counter()
    seen_ids: set[str] = set()
    seen_bios: set[str] = set()
    for record in loaded.records:
        if not isinstance(record, ServerRecord):
            skipped[record.kind] += 1
            continue
        bios = record.identity_hints.bios_uuid
        repeated = record.source_id in seen_ids or (bios is not None and bios in seen_bios)
        seen_ids.add(record.source_id)
        if bios is not None:
            seen_bios.add(bios)
        if not repeated:
            servers.append(record)
    return servers, skipped


async def migration_assess_readiness(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Check every server of the caller's stored inventories. *target* is always ``None``."""
    del target
    loaded = await load_inventories(operator, list(params["inventory_handles"]))
    generated_at = datetime.now(UTC)
    servers, skipped = _first_records(loaded)
    items: list[AssessmentItem] = []
    unknowns: list[Unknown] = []
    for record in servers:
        readiness = assess_server(record, as_of=generated_at.date())
        items.append(
            AssessmentItem(
                source_id=record.source_id,
                name=record.name or "(name not reported)",
                flags=[f.flag for f in readiness.findings if f.state != "ready"],
                readiness=readiness,
            )
        )
        unknowns += [
            Unknown(source_id=record.source_id, what=f.flag, reason=f.reason)
            for f in readiness.findings
            if f.state == "unknown"
        ]
    other: dict[str, str | float | int | bool] = {
        **_ASSUMPTIONS,
        "support_dates_as_of": generated_at.date().isoformat(),
    }
    result = AssessmentResult(
        schema_version="1",
        calculation_version=CALCULATION_VERSION,
        inputs=_inputs(loaded, generated_at),
        assumptions=Assumptions(region=params.get("region"), other=other),
        items=items,
        unknowns=unknowns,
        omissions=[omission for envelope in loaded.envelopes for omission in envelope.omissions],
    ).model_dump(mode="json")
    states: Counter[str] = Counter(
        item.readiness.state for item in items if item.readiness is not None
    )
    result["counts"] = {state: states.get(state, 0) for state in _STATES}
    result["skipped_records"] = dict(skipped)
    result["duplicates"] = [
        {"source_id": d.source_id, "same_as": d.same_as, "match": d.match}
        for d in loaded.duplicates
    ]
    return result


async def register_readiness_operation(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert ``migration.assess.readiness`` into ``endpoint_descriptor``. Safe to run twice."""
    await register_typed_operation(
        product="migration",
        version="1.x",
        impl_id="migration-assess",
        op_id=MIGRATION_ASSESS_READINESS_OP_ID,
        handler=migration_assess_readiness,
        group_key=_GROUP_KEY,
        when_to_use=_GROUP_WHEN_TO_USE,
        summary=_SUMMARY,
        description=_DESCRIPTION,
        parameter_schema=MIGRATION_ASSESS_READINESS_PARAMETER_SCHEMA,
        response_schema=MIGRATION_ASSESS_READINESS_RESPONSE_SCHEMA,
        tags=["migration", "assessment", "azure", "readiness", "read"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions=_LLM_INSTRUCTIONS,
        embedding_service=embedding_service,
    )
