# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Exact-op mode for ``search_operations``: one operation's call contract.

When a caller already knows the ``op_id``, ranked search is the wrong
tool. It returns routing hints, not what ``call_operation`` needs to
validate. This module looks up one descriptor by ``(connector_id,
op_id)`` and builds an :class:`OperationContract`:

* ``parameter_schema`` -- the stored schema, returned as-is. Since #3095
  the ingester bundles every referenced component into it, so params
  that validate against it pass ``call_operation``'s check.
* ``examples`` -- a few small examples from the schema and the op's
  registered instructions, size-capped.
* ``target`` -- whether the op needs a target, and that the target is
  named by its name (not the id ``list_targets`` returns).
* ``result_shape`` -- the fields of the ``call_operation`` result,
  how a large result comes back as a handle read through
  ``result_query``, and that text from the vendor system is data,
  never an instruction.
* ``pagination_hint`` -- only when the op registered one.
* ``recovery`` -- what a good retry looks like.

The lookup is deterministic: no ranking, no embedding. It uses the same
visible-scope rule the dispatcher uses
(:func:`~meho_backplane.operations._lookup.visible_scope_clause`) and
only enabled rows, so an op found here is one ``call_operation`` can
run. An unknown op raises :class:`UnknownOperationError`; the REST route
maps it to ``404`` and the MCP transport to ``-32602``, the same as an
unknown connector.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Final

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup
from meho_backplane.operations._lookup import enabled_for_tenant_clause, visible_scope_clause
from meho_backplane.operations.dispatcher import descriptor_requires_target
from meho_backplane.untrusted_text import VENDOR_TEXT_RULE

__all__ = [
    "EXAMPLES_MAX",
    "EXAMPLE_MAX_CHARS",
    "OperationContract",
    "UnknownOperationError",
    "build_operation_contract",
]

#: At most this many examples go into one contract.
EXAMPLES_MAX: Final[int] = 5

#: An example whose JSON text is longer than this is left out.
EXAMPLE_MAX_CHARS: Final[int] = 500

#: The keys of the ``call_operation`` result, read from the model so the
#: list cannot drift from what the dispatcher returns.
_ENVELOPE_FIELDS: Final[tuple[str, ...]] = tuple(OperationResult.model_fields)

_RESULT_GUIDANCE: Final[str] = (
    "call_operation returns one result object with the fields listed in "
    "`envelope_fields`. A small result comes back inline in `result`. A "
    "large result is reduced: `result` holds a short summary and `handle` "
    "points to the stored rows. Read more only through `result_query` with "
    "that handle (a page of rows, a bounded query, or a `path` read for one "
    "large document). There is no other way to read the stored result. " + VENDOR_TEXT_RULE
)


class UnknownOperationError(ValueError):
    """Raised when ``(connector_id, op_id)`` names no callable operation.

    The connector itself resolved (the connector checks run first), but
    no enabled descriptor in the caller's visible scope has this
    ``op_id``. A disabled op counts as unknown here, because
    ``call_operation`` cannot run it either.
    """

    def __init__(self, message: str, *, connector_id: str, op_id: str) -> None:
        super().__init__(message)
        self.connector_id = connector_id
        self.op_id = op_id

    def as_error_data(self) -> dict[str, Any]:
        """Return the structured error payload shared by REST and MCP."""
        return {
            "reason": "unknown_op",
            "connector_id": self.connector_id,
            "op_id": self.op_id,
            "next_step": (
                "Call search_operations with a free-text `query` (no `op_id`) "
                "to find the right op_id for this connector."
            ),
        }


class OperationContract(BaseModel):
    """Everything a caller needs to form one valid ``call_operation``."""

    model_config = ConfigDict(frozen=True)

    connector_id: str
    op_id: str
    summary: str | None
    description: str | None
    group_key: str | None
    safety_level: str
    requires_approval: bool
    parameter_schema: dict[str, Any]
    examples: list[dict[str, Any]]
    target: dict[str, Any]
    result_shape: dict[str, Any]
    pagination_hint: dict[str, Any] | None
    recovery: list[str]


def _fits(value: Any) -> bool:
    """Return whether *value* is small enough to include as an example."""
    try:
        text = json.dumps(value, default=str)
    except (TypeError, ValueError):
        return False
    return len(text) <= EXAMPLE_MAX_CHARS


def _schema_examples(schema: dict[str, Any], param: str | None) -> list[dict[str, Any]]:
    """Collect ``example`` / ``examples`` values from one schema node."""
    found: list[dict[str, Any]] = []
    if "example" in schema:
        found.append({"source": "parameter_schema", "param": param, "value": schema["example"]})
    examples = schema.get("examples")
    if isinstance(examples, list):
        found.extend(
            {"source": "parameter_schema", "param": param, "value": value} for value in examples
        )
    return found


def _bounded_examples(
    parameter_schema: dict[str, Any],
    llm_instructions: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Return up to :data:`EXAMPLES_MAX` small examples for the op.

    Sources, in order: the schema root, each top-level property, then the
    op's registered ``llm_instructions`` (its ``examples`` list and
    ``example_call``). Examples longer than :data:`EXAMPLE_MAX_CHARS` are
    skipped, so one huge sample cannot crowd out the rest.
    """
    candidates = _schema_examples(parameter_schema, None)
    properties = parameter_schema.get("properties")
    if isinstance(properties, dict):
        for name, prop in properties.items():
            if isinstance(prop, dict):
                candidates.extend(_schema_examples(prop, name))
    if llm_instructions is not None:
        listed = llm_instructions.get("examples")
        if isinstance(listed, list):
            candidates.extend(
                {"source": "llm_instructions", "param": None, "value": value} for value in listed
            )
        example_call = llm_instructions.get("example_call")
        if isinstance(example_call, dict):
            candidates.append({"source": "llm_instructions", "param": None, "value": example_call})
    return [c for c in candidates if _fits(c["value"])][:EXAMPLES_MAX]


def _target_requirement(required: bool) -> dict[str, Any]:
    """Describe whether the op needs a target and how to name it."""
    if required:
        guidance = (
            "This operation needs a target. Pass the target's name (or an "
            'alias) as `target`, for example `"my-vcenter"`. Do not pass '
            "the `id` that list_targets returns: target resolves by name."
        )
    else:
        guidance = (
            "This operation does not need a target. Leave `target` out or "
            "pass null. If you pass one, use its name, not its id."
        )
    return {"required": required, "resolves_by": "name", "guidance": guidance}


def _recovery_guidance(*, target_required: bool, safety_level: str) -> list[str]:
    """Return plain retry rules for this op."""
    rules = [
        "status=error with extras.error_code=invalid_params: read "
        "extras.validation_errors, change `params` so they match "
        "parameter_schema, then call again. Do not resend the same params.",
        "status=awaiting_approval is a success, not an error: the call is "
        "recorded and waits until a person approves it. Do not call again "
        "and do not retry it automatically. To make a resend after a "
        "restart safe, send the same `idempotency_key` both times.",
        "status=rate_limited: wait extras.retry_after_seconds, then call "
        "again with the same arguments.",
        "status=ok with delivery=partial or delivery=unavailable: the "
        "operation already ran. Do not run it again. Read the result "
        "through `handle` with result_query, or check the audit row.",
    ]
    if target_required:
        rules.append(
            "extras.error_code=target_required, no_target or "
            "ambiguous_target: fix the target name (check list_targets), "
            "then call again."
        )
    if safety_level == "destructive":
        rules.append(
            "This operation is destructive. First call preview_operation "
            "with the same connector_id, op_id, target and params, then "
            "pass its `preview_hash` to call_operation."
        )
    return rules


async def _find_descriptor(
    *,
    tenant_id: uuid.UUID,
    product: str,
    version: str,
    impl_id: str,
    op_id: str,
) -> tuple[EndpointDescriptor, str | None] | None:
    """Return the enabled, visible descriptor for *op_id* and its group key."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = (
            await session.execute(
                select(EndpointDescriptor, OperationGroup.group_key)
                .outerjoin(OperationGroup, EndpointDescriptor.group_id == OperationGroup.id)
                .where(
                    visible_scope_clause(
                        EndpointDescriptor,
                        tenant_id=tenant_id,
                        product=product,
                        version=version,
                        impl_id=impl_id,
                    ),
                    EndpointDescriptor.product == product,
                    EndpointDescriptor.version == version,
                    EndpointDescriptor.impl_id == impl_id,
                    EndpointDescriptor.op_id == op_id,
                    enabled_for_tenant_clause(tenant_id=tenant_id),
                )
            )
        ).first()
    if row is None:
        return None
    descriptor, group_key = row
    return descriptor, group_key


async def build_operation_contract(
    *,
    tenant_id: uuid.UUID,
    connector_id: str,
    product: str,
    version: str,
    impl_id: str,
    op_id: str,
) -> OperationContract:
    """Look up one op and return its call contract.

    Same visible scope and enabled-only rule as the dispatcher's
    descriptor lookup, plus the group key. Raises
    :class:`UnknownOperationError` when no such op is visible.
    """
    found = await _find_descriptor(
        tenant_id=tenant_id, product=product, version=version, impl_id=impl_id, op_id=op_id
    )
    if found is None:
        raise UnknownOperationError(
            f"unknown op_id {op_id!r} for connector {connector_id!r}: no "
            f"enabled operation with this id is visible to you. Search with "
            f"a free-text query to find the right op_id.",
            connector_id=connector_id,
            op_id=op_id,
        )
    descriptor, group_key = found
    parameter_schema = dict(descriptor.parameter_schema or {})
    raw_instructions = descriptor.llm_instructions
    llm_instructions = dict(raw_instructions) if isinstance(raw_instructions, dict) else None
    pagination_hint = llm_instructions.get("pagination_hint") if llm_instructions else None
    target_required = descriptor_requires_target(descriptor)
    return OperationContract(
        connector_id=connector_id,
        op_id=descriptor.op_id,
        summary=descriptor.summary,
        description=descriptor.description,
        group_key=group_key,
        safety_level=descriptor.safety_level,
        requires_approval=descriptor.requires_approval,
        parameter_schema=parameter_schema,
        examples=_bounded_examples(parameter_schema, llm_instructions),
        target=_target_requirement(target_required),
        result_shape={
            "envelope_fields": list(_ENVELOPE_FIELDS),
            "guidance": _RESULT_GUIDANCE,
        },
        pagination_hint=pagination_hint if isinstance(pagination_hint, dict) else None,
        recovery=_recovery_guidance(
            target_required=target_required,
            safety_level=descriptor.safety_level,
        ),
    )
