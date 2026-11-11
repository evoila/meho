# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``category`` and ``labels`` on the admin tools that edit one operation or one group.

``meho_connector_edit_op`` and ``meho_connector_edit_group`` take a
``category`` and ``labels``, like the REST PATCH routes. A change of them
is a change of rights: the tool asks for it through
:func:`~meho_backplane.policy.bulk.request_classification_edit`, and the
change waits until another tenant admin approves it. It cannot ride along
with the fields the tool stores at once.

The fields and their text exist only while the ``POLICY_CLASSIFICATION``
switch is on: :func:`classification_for_settings` takes them out of the
description and the input schema while it is off, so ``tools/list`` is
the same as before and a call that still sends them is refused as an
invalid input.

Connector-wide and pattern edits go through the operations
``policy.classify_preview`` (the dry run) and ``policy.classify``.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.auth.operator import Operator
from meho_backplane.mcp.registry import ToolDefinition
from meho_backplane.mcp.server import McpInvalidParamsError
from meho_backplane.policy.bulk import (
    ClassificationEditError,
    ClassificationOffError,
    request_classification_edit,
)
from meho_backplane.policy.store import ClassificationEdit, classification_enabled

__all__ = [
    "CLASSIFICATION_PROPERTIES",
    "CLASSIFICATION_TEXT",
    "classification_for_settings",
    "request_from_tool",
]

#: The part of the tool description about the two fields.
CLASSIFICATION_TEXT: Final[str] = (
    " category (a category of the taxonomy) and labels (free key=value text) are a "
    "change of rights for your tenant: send them without other fields; the answer "
    "has status awaiting_approval and an approval_request_id, and the change waits "
    "until another tenant admin approves it. For a whole connector or a pattern, use "
    "the operations policy.classify_preview (dry run) and policy.classify."
)

CLASSIFICATION_PROPERTIES: Final[dict[str, Any]] = {
    "category": {
        "type": "string",
        "pattern": "^[a-z][a-z0-9_-]{0,62}$",
        "description": "A category of the taxonomy, for example network or storage.",
    },
    "labels": {
        "type": "object",
        "maxProperties": 32,
        "additionalProperties": {"type": "string", "minLength": 1, "maxLength": 255},
        "description": "Free key=value labels. Replaces the labels of this entry.",
    },
}


def classification_for_settings(definition: ToolDefinition, operator: Operator) -> ToolDefinition:
    """Return *definition* without ``category`` / ``labels`` while the switch is off."""
    del operator  # the switch is install-wide
    if classification_enabled():
        return definition
    schema = definition.inputSchema
    properties = {
        key: value
        for key, value in schema["properties"].items()
        if key not in CLASSIFICATION_PROPERTIES
    }
    return definition.model_copy(
        update={
            "description": definition.description.replace(CLASSIFICATION_TEXT, ""),
            "inputSchema": {**schema, "properties": properties},
        },
    )


async def request_from_tool(
    operator: Operator,
    arguments: dict[str, Any],
    *,
    tool: str,
    match: dict[str, str],
    other_fields: tuple[str, ...],
) -> dict[str, Any] | None:
    """Ask for the classification edit in *arguments*; ``None`` when it has none.

    Refused with ``-32602`` when it is mixed with *other_fields*, when the
    switch is off, or when the edit is refused (unknown category, no
    matching operation, a conflict).
    """
    category = arguments.get("category")
    labels = arguments.get("labels")
    if category is None and labels is None:
        return None
    mixed = [name for name in other_fields if arguments.get(name) is not None]
    if mixed:
        raise McpInvalidParamsError(
            f"{tool}: classification_mixed: category and labels wait for another tenant "
            f"admin, so send them in a call of their own, without {', '.join(mixed)}.",
            data={"error_code": "classification_mixed", "fields": mixed},
        )
    edit = ClassificationEdit(match=match, category=category, labels=labels, always_wait=None)
    try:
        result = await request_classification_edit(operator, edit)
    except (ClassificationOffError, ClassificationEditError) as exc:
        code = (
            "classification_off"
            if isinstance(exc, ClassificationOffError)
            else "classification_refused"
        )
        raise McpInvalidParamsError(f"{tool}: {code}: {exc}", data={"error_code": code}) from exc
    extras = result.get("extras") or {}
    return {
        **match,
        "status": result.get("status"),
        "approval_request_id": extras.get("approval_request_id"),
        "error": result.get("error"),
        "matched_operations": result["matched_operations"],
        "changed_operations": result["changed_operations"],
        "ok": result.get("status") in ("awaiting_approval", "ok"),
    }
