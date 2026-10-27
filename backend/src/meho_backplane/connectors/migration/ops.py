# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ``migration.inventory.csv`` operation and the package registrar.

The registrar also registers ``migration.assess.vm`` (:mod:`.assess_vm_op`).

Natural key ``(product="migration", version="1.x", impl_id="migration-assess")``,
so the wire ``connector_id`` is ``migration-assess-1.x``. No connector class
backs it (the ``audit-integrity-1.x`` mould): the handler is a module-level
function the dispatcher calls with ``target=None``.

The operation only reads stored results of the caller and builds text, so
it is ``safe`` and sets ``requires_approval=False``. It is a typed
operation, not a multi-step one: it runs no other operation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.migration.assess_vm_op import register_assess_vm_operation
from meho_backplane.connectors.migration.azure_migrate_csv import (
    MAX_SERVERS_PER_FILE,
    CsvExport,
    inventory_to_csv,
)
from meho_backplane.connectors.migration.inventory_load import (
    MAX_INVENTORY_HANDLES,
    load_inventories,
)
from meho_backplane.operations.typed_register import register_typed_operation

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "MIGRATION_CONNECTOR_ID",
    "MIGRATION_INVENTORY_CSV_OP_ID",
    "MIGRATION_INVENTORY_CSV_PARAMETER_SCHEMA",
    "MIGRATION_INVENTORY_CSV_RESPONSE_SCHEMA",
    "migration_inventory_csv",
    "register_migration_operations",
]

MIGRATION_CONNECTOR_ID: Final[str] = "migration-assess-1.x"
MIGRATION_INVENTORY_CSV_OP_ID: Final[str] = "migration.inventory.csv"

_EXAMPLE_HANDLE: Final[str] = "3f2b8c1e-6a4d-4e2f-9b7a-1c5d8e9f0a21"

MIGRATION_INVENTORY_CSV_PARAMETER_SCHEMA: dict[str, Any] = {
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
    },
    "required": ["inventory_handles"],
    "additionalProperties": False,
    "examples": [{"inventory_handles": [_EXAMPLE_HANDLE]}],
}


def _response_schema() -> dict[str, Any]:
    schema = CsvExport.model_json_schema()
    properties = schema["properties"]
    properties["sources"] = {
        "type": "array",
        "description": "The inventories read: handle id, connector, target and time.",
        "items": {
            "type": "object",
            "properties": {
                "handle_id": {"type": "string"},
                "connector_id": {"type": "string"},
                "target": {"type": "string"},
                "collected_at": {"type": "string", "format": "date-time"},
            },
        },
    }
    schema["required"] = [*schema.get("required", []), "sources"]
    return schema


MIGRATION_INVENTORY_CSV_RESPONSE_SCHEMA: dict[str, Any] = _response_schema()

_GROUP_KEY: Final[str] = "inventory"
_GROUP_WHEN_TO_USE: Final[str] = (
    "Read-only work on estate inventories that other reads collected and MEHO "
    "stored: merge them and turn them into files other tools import. Nothing "
    "here calls a source system or changes anything."
)

_SUMMARY: Final[str] = "Turn stored inventories into an Azure Migrate CSV import file."

_DESCRIPTION: Final[str] = (
    "Reads one or more stored inventories, merges them and returns the CSV text in "
    "the column format of Azure Migrate's import template, the row count, and the "
    "servers left out or renamed. Read-only."
)

_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "You have inventory handles and need a file for Azure Migrate's CSV "
        "import, or want to see which servers lack the values Azure Migrate "
        "needs (name, cores, memory, OS name)."
    ),
    "parameter_hints": {
        "inventory_handles": (
            f"Required. 1 to {MAX_INVENTORY_HANDLES} handle ids. A stored result "
            "lasts one hour; an expired handle fails with a clear message."
        ),
    },
    "output_shape": (
        "{'csv_lines': [<header line>, <one line per server>, ...], 'row_count', "
        "'columns', 'left_out': [{source_id, name, reason, missing, same_as}], "
        "'renamed': [{source_id, original_name, csv_name, reasons}], "
        "'skipped_records': {kind: count}, 'notes', 'sources'}. At most "
        f"{MAX_SERVERS_PER_FILE} servers per file."
    ),
    "next_step": (
        "Show the left-out and renamed servers to the user before the file is "
        "imported. Join csv_lines with CRLF to get the file. When the result came "
        "back as a handle, read the lines in pages with result_query, path "
        "'/csv_lines' and offset/limit; each page returns rows of {'value': <line>}."
    ),
}


async def migration_inventory_csv(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Build the Azure Migrate CSV file from the caller's stored inventories.

    Targetless; *target* is always ``None``. The tenant and the caller come
    from *operator*, never from *params*.
    """
    del target
    loaded = await load_inventories(operator, list(params["inventory_handles"]))
    export = inventory_to_csv(loaded.records, loaded.duplicates)
    result = export.model_dump(mode="json")
    result["sources"] = [
        {
            "handle_id": handle_id,
            "connector_id": envelope.source.connector_id,
            "target": envelope.source.target,
            "collected_at": envelope.source.collected_at.isoformat(),
        }
        for handle_id, envelope in zip(loaded.handle_ids, loaded.envelopes, strict=True)
    ]
    return result


async def register_migration_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert the migration operations into ``endpoint_descriptor``. Safe to run twice."""
    await register_typed_operation(
        product="migration",
        version="1.x",
        impl_id="migration-assess",
        op_id=MIGRATION_INVENTORY_CSV_OP_ID,
        handler=migration_inventory_csv,
        group_key=_GROUP_KEY,
        when_to_use=_GROUP_WHEN_TO_USE,
        summary=_SUMMARY,
        description=_DESCRIPTION,
        parameter_schema=MIGRATION_INVENTORY_CSV_PARAMETER_SCHEMA,
        response_schema=MIGRATION_INVENTORY_CSV_RESPONSE_SCHEMA,
        tags=["migration", "inventory", "azure-migrate", "csv", "read"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions=_LLM_INSTRUCTIONS,
        embedding_service=embedding_service,
    )
    await register_assess_vm_operation(embedding_service=embedding_service)
