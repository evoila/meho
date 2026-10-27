# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Registration of ``migration.assess.vm``: its schemas, its guidance and its group.

The operation is multi-step (it runs the Azure connector's reads as child
calls) and only reads, so it is ``safe`` and sets ``requires_approval=False``
on purpose: :func:`register_composite_operation` defaults to ``dangerous``
with approval.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.migration.assess_vm import migration_assess_vm
from meho_backplane.connectors.migration.assess_vm_figures import PRICING_OPTIONS
from meho_backplane.connectors.migration.contracts import assessment_result_json_schema
from meho_backplane.connectors.migration.inventory_load import MAX_INVENTORY_HANDLES
from meho_backplane.operations.typed_register import register_composite_operation

if TYPE_CHECKING:
    from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "MIGRATION_ASSESS_VM_OP_ID",
    "MIGRATION_ASSESS_VM_PARAMETER_SCHEMA",
    "MIGRATION_ASSESS_VM_RESPONSE_SCHEMA",
    "register_assess_vm_operation",
]

MIGRATION_ASSESS_VM_OP_ID: Final[str] = "migration.assess.vm"

_EXAMPLE_HANDLE: Final[str] = "3f2b8c1e-6a4d-4e2f-9b7a-1c5d8e9f0a21"

MIGRATION_ASSESS_VM_PARAMETER_SCHEMA: dict[str, Any] = {
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
        "azure_target": {
            "type": "string",
            "minLength": 1,
            "description": "Name of the Azure target whose subscription the sizes are read for.",
        },
        "region": {
            "type": "string",
            "pattern": "^[a-z0-9]+$",
            "description": "Azure region name, for example 'westeurope'.",
        },
        "currency": {
            "type": "string",
            "pattern": "^[A-Z]{3}$",
            "default": "EUR",
            "description": "Price currency code. Default EUR.",
        },
        "pricing": {
            "type": "array",
            "items": {"type": "string", "enum": list(PRICING_OPTIONS)},
            "minItems": 1,
            "uniqueItems": True,
            "description": "Pricing options to estimate. Default: all three.",
        },
        "hybrid_benefit": {
            "type": "boolean",
            "default": False,
            "description": "True when Windows licenses are brought along (Azure Hybrid Benefit).",
        },
        "comfort_factor": {
            "type": "number",
            "minimum": 1,
            "maximum": 3,
            "default": 1.3,
            "description": "Headroom on measured use for the right-sized option. Default 1.3.",
        },
        "size_families": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "maxItems": 50,
            "description": (
                "Only these VM size families, for example 'DSv5' or 'standardESv5Family'. "
                "Default: all families."
            ),
        },
    },
    "required": ["inventory_handles", "azure_target", "region"],
    "additionalProperties": False,
    "examples": [
        {
            "inventory_handles": [_EXAMPLE_HANDLE],
            "azure_target": "azure-example",
            "region": "westeurope",
            "currency": "EUR",
            "pricing": ["pay_as_you_go", "reserved_3_years"],
        }
    ],
}


def _response_schema() -> dict[str, Any]:
    schema = assessment_result_json_schema()
    schema["properties"]["totals"] = {
        "type": "object",
        "description": (
            "Per pricing option: 'allocated' (all servers; unknown when one has no "
            "price), 'allocated_priced_servers' and 'measured_priced_servers' (the "
            "sum over the servers that have a price, with their count)."
        ),
        "additionalProperties": {
            "type": "object",
            "properties": {
                "allocated": {"$ref": "#/$defs/Figure"},
                "allocated_priced_servers": {"$ref": "#/$defs/Figure"},
                "measured_priced_servers": {"$ref": "#/$defs/Figure"},
            },
        },
    }
    schema["required"] = [*schema.get("required", []), "totals"]
    return schema


MIGRATION_ASSESS_VM_RESPONSE_SCHEMA: dict[str, Any] = _response_schema()

_GROUP_KEY: Final[str] = "assessment"
_GROUP_WHEN_TO_USE: Final[str] = (
    "Turn stored estate inventories into an Azure migration assessment: VM sizes, "
    "readiness and monthly cost estimates. Every number shows its inputs and "
    "assumptions. These operations only read."
)

_SUMMARY: Final[str] = "Pick an Azure VM size for every server and estimate its monthly cost."

_DESCRIPTION: Final[str] = (
    "Reads stored inventories, picks the cheapest fitting Azure VM size per server "
    "(as allocated today and, with enough measurements, right-sized), and prices "
    "compute and managed disks from Azure's public retail prices. Read-only."
)

_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "You have inventory handles and need Azure VM sizes and a monthly cost "
        "estimate per server and in total, for pay-as-you-go or reserved pricing."
    ),
    "parameter_hints": {
        "inventory_handles": (
            f"Required. 1 to {MAX_INVENTORY_HANDLES} handle ids from the inventory reads."
        ),
        "azure_target": "Required. The Azure target name (list_targets), not its id.",
        "region": "Required. Azure region name such as 'westeurope'.",
        "pricing": "Any of pay_as_you_go, reserved_1_year, reserved_3_years.",
        "comfort_factor": "Headroom on measured p95 use; 1.3 adds 30%.",
    },
    "output_shape": (
        "The assessment result: 'assumptions', 'inputs', 'items' (per server: figures "
        "'size.allocated', 'compute.allocated.<pricing>', 'disk.<n>.allocated', "
        "'disks.allocated', 'monthly.allocated.<pricing>' and the same for 'measured'; "
        "'flags'), 'unknowns', 'omissions', and 'totals' per pricing option. Each "
        "figure has value (null when unknown, with unknown_reason), unit, basis and inputs."
    ),
    "next_step": (
        "Show the totals with their assumptions, then the servers flagged 'not_ready' "
        "or 'right_sized_unknown' and the unknowns. Prices in another currency than "
        "USD are reference values, not quotes. A large result comes back whole as a "
        "handle: read it with result_query and a path, for example '/totals', "
        "'/assumptions' or '/unknowns', and page '/items' with offset and limit."
    ),
    # The result is one document: 'items' beside 'totals', 'assumptions',
    # 'inputs', 'unknowns' and 'omissions'. Without this hint the reducer
    # would store 'items' as rows and drop the rest.
    "result_document": {"keep_whole": True},
}


async def register_assess_vm_operation(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert ``migration.assess.vm`` into ``endpoint_descriptor``. Safe to run twice."""
    await register_composite_operation(
        product="migration",
        version="1.x",
        impl_id="migration-assess",
        op_id=MIGRATION_ASSESS_VM_OP_ID,
        handler=migration_assess_vm,
        group_key=_GROUP_KEY,
        when_to_use=_GROUP_WHEN_TO_USE,
        summary=_SUMMARY,
        description=_DESCRIPTION,
        parameter_schema=MIGRATION_ASSESS_VM_PARAMETER_SCHEMA,
        response_schema=MIGRATION_ASSESS_VM_RESPONSE_SCHEMA,
        tags=["migration", "assessment", "azure", "sizing", "cost", "read"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions=_LLM_INSTRUCTIONS,
        embedding_service=embedding_service,
    )
