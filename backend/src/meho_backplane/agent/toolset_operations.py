# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Hosted operation tools built from the MCP tools (#4754).

A hosted agent run has three operation tools: ``list_operation_groups``,
``search_operations`` and ``call_operation``. The older hosted copies in
:mod:`meho_backplane.agent.toolset` are written by hand and drifted from the
MCP tools: no exact-op mode, no ``preview_hash``, other text. This module
builds the three tools from the registered MCP tools instead, like
:mod:`meho_backplane.agent.toolset_discovery` does for the discovery tools:

* the input schema is the MCP **wire** schema (``to_wire()``), deep-copied,
  minus the fields in :data:`LEFT_OUT_FIELDS`;
* the description is the MCP description. ``call_operation`` drops the part
  about the left-out ``async`` mode and adds one hosted sentence instead;
* the role floor and capability are the MCP tool's own;
* ``list_operation_groups`` and ``search_operations`` follow the
  ``SEARCH_TYPED_FIRST`` switch like ``tools/list`` does
  (:func:`~meho_backplane.mcp.tools._include_raw.include_raw_for_settings`):
  with it off they have no ``include_raw`` input and no line about it. So
  the toolset builds these tools for each run, not once at import;
* ``list_operation_groups`` follows the ``GROUP_LISTING_OPERATIONS`` switch
  the same way
  (:func:`~meho_backplane.mcp.tools._group_operations.group_operations_for_settings`):
  with it off it has no ``group`` input and no ``operations`` field.

The handlers are the shared ones in
:mod:`meho_backplane.operations.meta_tools`, the same ones the older copies
use. So the approval wait of the hosted ``call_operation`` keeps working.

Two gates run before each handler, because ``pydantic_ai.Tool.from_schema``
does not check the arguments against the schema:

* the arguments are checked against the registered MCP ``inputSchema``
  minus the left-out fields. That schema still has the rule "``query`` or
  ``op_id``" that the wire schema leaves out, so ``search_operations``
  without both goes back to the model as a retry instead of a crash;
* an unknown ``op_id`` in exact-op mode goes back to the model as a failed
  tool result with ``reason: unknown_op``, so the run goes on.

The toolset uses these tools only when the setting
``agent_operation_tools_from_mcp`` is on (off by default). With the setting
off, the hand-written copies run :func:`check_arguments` against their own
schema. While ``SEARCH_TYPED_FIRST`` is on, their two discovery tools get the
``include_raw`` input and its line too (:func:`with_include_raw_input`), so
a hosted run can follow the note that names the flag.
"""

from __future__ import annotations

import copy
from typing import Any

import jsonschema
from pydantic_ai import ModelRetry, ToolFailed

from meho_backplane.agent.meta_tool import MetaToolSpec, _MetaToolHandler
from meho_backplane.auth.operator import Operator
from meho_backplane.mcp.registry import ToolDefinition, get_tool

# Imported for its registration side effect: it registers the three MCP
# tools, so get_tool can resolve them below.
from meho_backplane.mcp.tools import operations as _operations_mcp_tools  # noqa: F401
from meho_backplane.mcp.tools._group_operations import group_operations_for_settings
from meho_backplane.mcp.tools._include_raw import (
    INCLUDE_RAW_INPUT_PROPERTY,
    include_raw_for_settings,
)
from meho_backplane.operations import meta_tools as _meta_tools
from meho_backplane.operations._search_typed_first import INCLUDE_RAW_LINE
from meho_backplane.operations.meta_tools import UnknownOperationError

__all__ = [
    "INCLUDE_RAW_TOOLS",
    "LEFT_OUT_FIELDS",
    "OPERATION_TOOL_NAMES",
    "build_operation_meta_tools",
    "check_arguments",
    "with_include_raw_input",
]


_LIST_OPERATION_GROUPS = "list_operation_groups"
_SEARCH_OPERATIONS = "search_operations"
_CALL_OPERATION = "call_operation"

#: The three tools, in the order the toolset advertises them.
OPERATION_TOOL_NAMES: tuple[str, ...] = (
    _LIST_OPERATION_GROUPS,
    _SEARCH_OPERATIONS,
    _CALL_OPERATION,
)

#: MCP input fields a hosted run does not get, per tool. Each one has its
#: reason here:
#:
#: * ``work_ref`` — MEHO sets the run's change reference itself: a run
#:   started by a trigger carries the trigger's reference, and every
#:   operation of the run is audited with it. The model must not replace it
#:   (the same rule as ``work_ref`` on ``broadcast_announce``).
#: * ``async``, ``run_id``, ``wait_seconds`` — background calls. The hosted
#:   tool waits until each call ends (and waits for an approval when the call
#:   needs one), so a background call and its status check are not needed.
#:   The shared handler does not read these fields either.
#:
#: ``list_operation_groups`` and ``search_operations`` leave out nothing.
#: ``include_scores`` stays: it changes nothing but the ranked hits, and MEHO
#: adds scores only for a ``tenant_admin`` caller, as on MCP.
LEFT_OUT_FIELDS: dict[str, tuple[str, ...]] = {
    _LIST_OPERATION_GROUPS: (),
    _SEARCH_OPERATIONS: (),
    _CALL_OPERATION: ("work_ref", "async", "run_id", "wait_seconds"),
}

#: The tools whose ``include_raw`` input follows the ``SEARCH_TYPED_FIRST``
#: switch.
INCLUDE_RAW_TOOLS: frozenset[str] = frozenset({_LIST_OPERATION_GROUPS, _SEARCH_OPERATIONS})

#: Where the MCP ``call_operation`` text starts to explain ``async`` mode.
#: The hosted text stops here.
_ASYNC_PART_START = " For a long call"

#: Added to the hosted ``call_operation`` text in place of the ``async`` part.
_HOSTED_CALL_NOTE = (
    " In this hosted run each call waits until it ends, also while it waits "
    "for an approval. There is no `async` mode."
)


def _call(handler_name: str) -> _MetaToolHandler:
    """Return a handler that calls the shared handler *handler_name*.

    The handler is looked up in :mod:`meho_backplane.operations.meta_tools`
    when the tool runs, not when it is built, so a test can replace it there.
    """

    async def _run(operator: Operator, arguments: dict[str, Any]) -> dict[str, Any]:
        handler: _MetaToolHandler = getattr(_meta_tools, handler_name)
        return await handler(operator, arguments)

    return _run


def _without_fields(schema: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    """Return a deep copy of *schema* without the properties *fields*."""
    reduced = copy.deepcopy(schema)
    properties = reduced.get("properties", {})
    for name in fields:
        properties.pop(name, None)
    if "required" in reduced:
        reduced["required"] = [name for name in reduced["required"] if name not in fields]
    return reduced


def check_arguments(
    name: str, schema: dict[str, Any], handler: _MetaToolHandler
) -> _MetaToolHandler:
    """Check the arguments against *schema*, then run *handler*.

    The check uses the same validator and format checker as MCP
    ``tools/call``. A bad argument goes back to the model as a retry. An
    unknown ``op_id`` goes back as a failed tool result with the
    ``unknown_op`` reason and the next step.
    """

    async def _wrapped(operator: Operator, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            jsonschema.validate(
                instance=arguments,
                schema=schema,
                cls=jsonschema.Draft202012Validator,
                format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER,
            )
        except jsonschema.ValidationError as exc:
            raise ModelRetry(f"{name}: invalid arguments: {exc.message}") from exc
        try:
            return await handler(operator, arguments)
        except UnknownOperationError as exc:
            data = exc.as_error_data()
            raise ToolFailed(f"{exc} (reason: {data['reason']}). {data['next_step']}") from exc

    return _wrapped


def _description(definition: ToolDefinition) -> str:
    """Return the hosted description: the MCP text, adapted for ``call_operation``."""
    if definition.name != _CALL_OPERATION:
        return definition.description
    kept = definition.description.split(_ASYNC_PART_START, 1)[0]
    return kept + _HOSTED_CALL_NOTE


def _build(name: str) -> MetaToolSpec:
    entry = get_tool(name)
    if entry is None:  # pragma: no cover - defensive; the import registers all three
        raise RuntimeError(f"hosted operation tools need MCP tool {name!r} to be registered")
    definition, _mcp_handler = entry
    if name in INCLUDE_RAW_TOOLS:
        definition = include_raw_for_settings(definition)
    if name == _LIST_OPERATION_GROUPS:
        definition = group_operations_for_settings(definition)
    left_out = LEFT_OUT_FIELDS[name]
    return MetaToolSpec(
        name=name,
        handler=check_arguments(
            name, _without_fields(definition.inputSchema, left_out), _call(name)
        ),
        description=_description(definition),
        parameter_schema=_without_fields(definition.to_wire()["inputSchema"], left_out),
        required_role=definition.required_role,
        required_capability=definition.required_capability,
    )


def build_operation_meta_tools() -> tuple[MetaToolSpec, ...]:
    """Build the three hosted operation tools from the registered MCP tools.

    The result depends on the ``SEARCH_TYPED_FIRST`` switch, so call this
    for each run.
    """
    return tuple(_build(name) for name in OPERATION_TOOL_NAMES)


def with_include_raw_input(spec: MetaToolSpec) -> MetaToolSpec:
    """Return a hand-written discovery tool with the ``include_raw`` input.

    For the hand-written copies while ``SEARCH_TYPED_FIRST`` is on: the same
    input and description line as the MCP tool. The shared handler reads
    the flag. *spec* has no argument check yet; the caller adds it, so the
    check uses the new schema.
    """
    schema = spec.parameter_schema
    return MetaToolSpec(
        name=spec.name,
        handler=spec.handler,
        description=spec.description + INCLUDE_RAW_LINE,
        parameter_schema={
            **schema,
            "properties": {**schema["properties"], **INCLUDE_RAW_INPUT_PROPERTY},
        },
        required_role=spec.required_role,
        required_capability=spec.required_capability,
    )
