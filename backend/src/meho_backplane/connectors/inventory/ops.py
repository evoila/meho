# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ``inventory.export`` operation and the package registrar.

Natural key ``(product="inventory", version="1.x", impl_id="inventory")``,
so the wire ``connector_id`` is ``inventory-1.x``. No connector class
backs it (the ``audit-integrity-1.x`` mould): the handler is a module-level
function the dispatcher calls with ``target=None``.

The operation only reads stored results of the caller (or inventories given
in the call) and builds text, so it is ``safe`` and sets
``requires_approval=False``. It is a typed operation, not a multi-step one:
it runs no other operation. It holds no rule of one target: the file comes
from the format named in the call (:mod:`.formats`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.inventory.export import ExportResult
from meho_backplane.connectors.inventory.formats import EXPORT_FORMATS, get_export_format
from meho_backplane.connectors.inventory.inventory_load import (
    MAX_INVENTORY_HANDLES,
    load_inventories,
)
from meho_backplane.operations.typed_register import register_typed_operation

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "INVENTORY_CONNECTOR_ID",
    "INVENTORY_EXPORT_OP_ID",
    "INVENTORY_EXPORT_PARAMETER_SCHEMA",
    "INVENTORY_EXPORT_RESPONSE_SCHEMA",
    "inventory_export",
    "register_inventory_operations",
]

INVENTORY_CONNECTOR_ID: Final[str] = "inventory-1.x"
INVENTORY_EXPORT_OP_ID: Final[str] = "inventory.export"

_EXAMPLE_HANDLE: Final[str] = "3f2b8c1e-6a4d-4e2f-9b7a-1c5d8e9f0a21"


def _format_description() -> str:
    known = "; ".join(f"'{name}': {fmt.description}" for name, fmt in EXPORT_FORMATS.items())
    return f"The file format to build. Known formats: {known}"


INVENTORY_EXPORT_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "format": {
            "type": "string",
            "enum": sorted(EXPORT_FORMATS),
            "description": _format_description(),
        },
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
        "inventories": {
            "type": "array",
            "items": {"type": "object"},
            "minItems": 1,
            "maxItems": MAX_INVENTORY_HANDLES,
            "description": (
                "Inventories given inline: each one is a whole inventory as an "
                "inventory read returns it (schema_version '1'). They are checked "
                "like stored ones and merged after the stored ones, in the given order."
            ),
        },
    },
    "required": ["format"],
    "anyOf": [{"required": ["inventory_handles"]}, {"required": ["inventories"]}],
    "additionalProperties": False,
    "examples": [{"inventory_handles": [_EXAMPLE_HANDLE], "format": next(iter(EXPORT_FORMATS))}],
}


def _response_schema() -> dict[str, Any]:
    schema = ExportResult.model_json_schema()
    properties = schema["properties"]
    properties["sources"] = {
        "type": "array",
        "description": (
            "The inventories read: where each came from ('stored' with its handle id, "
            "or 'inline'), connector, target and time."
        ),
        "items": {
            "type": "object",
            "properties": {
                "origin": {"type": "string", "enum": ["stored", "inline"]},
                "handle_id": {"type": "string"},
                "connector_id": {"type": "string"},
                "target": {"type": "string"},
                "collected_at": {"type": "string", "format": "date-time"},
            },
        },
    }
    schema["required"] = [*schema.get("required", []), "sources"]
    return schema


INVENTORY_EXPORT_RESPONSE_SCHEMA: dict[str, Any] = _response_schema()

_GROUP_KEY: Final[str] = "inventory"
_GROUP_WHEN_TO_USE: Final[str] = (
    "Read-only work on estate inventories that other reads collected and MEHO "
    "stored: merge them and turn them into files other tools import. Nothing "
    "here calls a source system or changes anything."
)

_SUMMARY: Final[str] = "Turn inventories into a file another tool imports, in a named format."

_DESCRIPTION: Final[str] = (
    "Reads one or more inventories, stored or given inline, merges them and returns "
    "the file in the format named in the call: the file lines, the row count, the "
    "records left out with the reason, the renamed records and notes. Read-only."
)

_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "You have inventory handles or inventories and need a file that another tool "
        "imports, or want to see which records lack the values that file needs."
    ),
    "parameter_hints": {
        "format": "One of the format names in the parameter schema's enum.",
        "inventory_handles": (
            f"1 to {MAX_INVENTORY_HANDLES} handle ids. A stored result "
            "lasts one hour; an expired handle fails with a clear message."
        ),
        "inventories": (
            "Whole inventories given inline, for example a small one you already "
            "have. Give inventory_handles, inventories, or both; at most "
            f"{MAX_INVENTORY_HANDLES} together."
        ),
    },
    "output_shape": (
        "{'format', 'lines': [<header line>, <one line per record>, ...], 'line_end', "
        "'row_count', 'columns', 'left_out': [{source_id, name, reason, missing, same_as}], "
        "'renamed': [{source_id, original_name, name_in_file, reasons}], "
        "'skipped_records': {kind: count}, 'notes', 'sources'}."
    ),
    "next_step": (
        "Show the left-out and renamed records to the user before the file is "
        "imported. Join lines with line_end to get the file. When the result came "
        "back as a handle, read the lines in pages with result_query, path "
        "'/lines' and offset/limit; each page returns rows of {'value': <line>}."
    ),
}


async def inventory_export(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Build the file in ``params["format"]`` from the caller's stored and inline inventories.

    Targetless; *target* is always ``None``. The tenant and the caller come
    from *operator*, never from *params*. An unknown format fails before any
    inventory is read.
    """
    del target
    export_format = get_export_format(str(params.get("format")))
    loaded = await load_inventories(
        operator,
        list(params.get("inventory_handles") or []),
        inventories=list(params.get("inventories") or []),
    )
    export = export_format.build(loaded.records, loaded.duplicates)
    result = export.model_dump(mode="json")
    result["sources"] = loaded.sources()
    return result


async def register_inventory_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert the inventory operations into ``endpoint_descriptor``. Safe to run twice."""
    await register_typed_operation(
        product="inventory",
        version="1.x",
        impl_id="inventory",
        op_id=INVENTORY_EXPORT_OP_ID,
        handler=inventory_export,
        group_key=_GROUP_KEY,
        when_to_use=_GROUP_WHEN_TO_USE,
        summary=_SUMMARY,
        description=_DESCRIPTION,
        parameter_schema=INVENTORY_EXPORT_PARAMETER_SCHEMA,
        response_schema=INVENTORY_EXPORT_RESPONSE_SCHEMA,
        tags=["inventory", "export", "read"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions=_LLM_INSTRUCTIONS,
        embedding_service=embedding_service,
    )
