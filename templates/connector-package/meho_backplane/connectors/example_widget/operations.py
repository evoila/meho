# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The two operations of the example widget connector, and their texts.

* ``example.widget.list``: a safe read. It returns every widget. A long list
  comes back as a result handle (a link to the stored result): MEHO does
  that by itself after the handler returns.
* ``example.widget.rename``: a change. MEHO waits until a person approves
  it. While it waits, the preview shows the person the widget's current
  name and the new name.

The texts are for agents: an agent picks an operation from its summary,
description, group text and guidance, and fills in the inputs from the
field descriptions. MEHO's operation contract check
(``backend/tests/test_operation_contracts.py``) checks these texts.

This file imports MEHO only through the connector kit.
"""

from __future__ import annotations

from typing import Any

from meho_connector_kit import EmbeddingService, PreviewContext, register_typed_operation

from .connector import (
    IMPL_ID,
    PRODUCT,
    VERSION,
    WIDGET_ID_PATTERN,
    ExampleWidgetConnector,
)

LIST_OP_ID = "example.widget.list"
RENAME_OP_ID = "example.widget.rename"
GROUP_KEY = "widgets"

#: The text of the operation group: when an agent should use this group.
GROUP_TEXT = (
    "Use to read and rename the widgets of an example widget service: list every "
    "widget with its id, name and color, or give one widget a new name."
)

_WIDGET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "The widget's id. It never changes."},
        "name": {"type": ["string", "null"], "description": "The widget's name."},
        "color": {"type": ["string", "null"], "description": "The widget's color."},
    },
}

_WIDGET_ID_FIELD: dict[str, Any] = {
    "type": "string",
    "pattern": WIDGET_ID_PATTERN,
    "description": "The id of the widget, as example.widget.list returns it.",
}

OPERATIONS: tuple[dict[str, Any], ...] = (
    {
        "op_id": LIST_OP_ID,
        "handler": ExampleWidgetConnector.list_widgets,
        "summary": "List every widget with its id, name and color.",
        "description": (
            "Reads all pages of the widget list. A long list comes back as a result "
            "handle; read it with result_query."
        ),
        "parameter_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
            "examples": [{}],
        },
        "response_schema": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "description": "Every widget.",
                    "items": _WIDGET_SCHEMA,
                },
            },
        },
        "safety_level": "safe",
        "requires_approval": False,
        "tags": ["read-only"],
        "llm_instructions": {
            "when_to_call": "To find a widget, or its id, before you work with it.",
            "output_shape": "{items: [{id, name, color}]}, or a result handle for a long list.",
            "next_step": "Use a widget's id with example.widget.rename.",
        },
    },
    {
        "op_id": RENAME_OP_ID,
        "handler": ExampleWidgetConnector.rename_widget,
        "summary": "Give one widget a new name. A person must approve it first.",
        "description": (
            "Changes the name of one widget, found by its id. MEHO waits until a person "
            "approves the call; the preview shows the current and the new name."
        ),
        "parameter_schema": {
            "type": "object",
            "properties": {
                "widget_id": _WIDGET_ID_FIELD,
                "new_name": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                    "description": "The new name of the widget.",
                },
            },
            "required": ["widget_id", "new_name"],
            "additionalProperties": False,
            "examples": [{"widget_id": "w-0001", "new_name": "blue widget"}],
        },
        "response_schema": _WIDGET_SCHEMA,
        "safety_level": "caution",
        "requires_approval": True,
        "tags": ["write"],
        "llm_instructions": {
            "when_to_call": "When the user asks to rename a widget.",
            "preconditions": "You know the widget's id. Get it from example.widget.list.",
            "output_shape": "The renamed widget: {id, name, color}.",
            "next_step": "Tell the user that the call waits until a person approves it.",
        },
    },
)


async def register_example_widget_operations(
    *, embedding_service: EmbeddingService | None = None
) -> None:
    """Register both operations. MEHO calls this once when it starts."""
    for operation in OPERATIONS:
        await register_typed_operation(
            product=PRODUCT,
            version=VERSION,
            impl_id=IMPL_ID,
            group_key=GROUP_KEY,
            when_to_use=GROUP_TEXT,
            embedding_service=embedding_service,
            **operation,
        )


async def rename_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """The preview of ``example.widget.rename``, shown while the call waits.

    It only reads: it gets the widget by its id and shows its current name
    next to the new name. It changes nothing.
    """
    connector = ctx.connector_instance
    if not isinstance(connector, ExampleWidgetConnector) or ctx.target is None:
        return None
    widget = await connector.get_widget(ctx.operator, ctx.target, ctx.params.get("widget_id"))
    return {
        "widget_id": widget["id"],
        "current_name": widget["name"],
        "new_name": ctx.params.get("new_name"),
    }
