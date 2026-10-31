# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Metadata of the three Orchestrator library reads (the bodies live in ``library_reads``).

``vro.action.list``, ``vro.config_element.list`` and ``vro.package.list``.
All are ``safe`` and need no approval.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.vro.op_types import (
    GROUP_LIBRARY,
    VroTypedOp,
    id_schema,
    string_schema,
)
from meho_backplane.connectors.vro.read_ops import MAX_RESULTS_SCHEMA

__all__ = ["VRO_LIBRARY_OPS"]

_SCALARS: Final[dict[str, Any]] = {"keys": ["total", "count", "more_rows"]}


def _list_schema(description: str, row_properties: dict[str, Any]) -> dict[str, Any]:
    """``{items: [row], total, count, more_rows}`` for a list the server sends whole."""
    return {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "description": description,
                "items": {
                    "type": "object",
                    "description": "One row; field names as the server sends them.",
                    "properties": row_properties,
                },
            },
            "total": {
                "type": ["integer", "null"],
                "description": "How many rows exist (after the module filter), when known.",
            },
            "count": {"type": "integer", "description": "How many rows came back."},
            "more_rows": {
                "type": "boolean",
                "description": "True when more rows exist than came back; raise max_results.",
            },
        },
    }


_ACTION_LIST = VroTypedOp(
    op_id="vro.action.list",
    handler_attr="action_list",
    summary="List Orchestrator actions (scripts), optionally of one module.",
    description=(
        "Reads GET /vco/api/actions. Returns one flat row per action: id, name, module, "
        "fqn, version and description. module keeps the actions of one module."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "module": {
                "type": "string",
                "minLength": 1,
                "maxLength": 300,
                "description": "Keep the actions of the module with exactly this name.",
            },
            "max_results": MAX_RESULTS_SCHEMA,
        },
        "additionalProperties": False,
        "examples": [{"module": "<module-name>"}],
    },
    response_schema=_list_schema(
        "One flat row per action.",
        {
            "id": string_schema("Action id."),
            "name": string_schema("Action name."),
            "module": string_schema("Module (folder) of the action."),
            "fqn": string_schema("Full name: <module>/<name>."),
            "version": string_schema("Action version."),
            "description": string_schema("Action description."),
            "href": string_schema("API address of the action."),
        },
    ),
    group_key=GROUP_LIBRARY,
    tags=("read-only", "vro", "orchestrator", "action"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to see which actions (scripts) exist, for example to find the module "
            "and version of an action a workflow uses."
        ),
        "output_shape": (
            "{items: [{id, name, module, fqn, version, description, href, ...}], total, "
            "count, more_rows}. A long list is behind a result handle; filter it with "
            "result_query."
        ),
        "next_step": (
            "Report the actions. If more_rows is true, call again with a larger "
            "max_results or a module."
        ),
        "result_scalars": _SCALARS,
    },
)

_CONFIG_ELEMENT_LIST = VroTypedOp(
    op_id="vro.config_element.list",
    handler_attr="config_element_list",
    summary="List Orchestrator configuration elements, or the attributes of one element.",
    description=(
        "Without config_element_id: GET /vco/api/configurations, one row per element. "
        "With it: GET /vco/api/configurations/{id}, the element and one row per "
        "attribute (name, type, value). Secret values come back replaced."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "config_element_id": id_schema(
                "Id of one configuration element, as a row of this list returns it. "
                "Leave it out to list the elements."
            ),
            "max_results": MAX_RESULTS_SCHEMA,
        },
        "additionalProperties": False,
        "examples": [{}, {"config_element_id": "<config-element-id>"}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "element": {
                "type": ["object", "null"],
                "description": "The element read by config_element_id; null for the list.",
                "properties": {
                    "id": string_schema("Element id."),
                    "name": string_schema("Element name."),
                    "version": string_schema("Element version."),
                    "description": string_schema("Element description."),
                    "category_id": string_schema("Id of the element's folder."),
                },
            },
            **_list_schema(
                "One row per element (list) or per attribute (one element).",
                {
                    "id": string_schema("Element id (list rows)."),
                    "name": string_schema("Element name, or attribute name."),
                    "type": string_schema("Attribute type, for example string or SecureString."),
                    "description": string_schema("Description."),
                    "masked": {
                        "type": "boolean",
                        "description": "Attribute rows: true when the value is secret and hidden.",
                    },
                    "value": {
                        "description": (
                            "Attribute rows: the value as the server sends it, for example "
                            "{string: {value: ...}}. A secret value is ***REDACTED***."
                        ),
                    },
                },
            )["properties"],
        },
    },
    group_key=GROUP_LIBRARY,
    tags=("read-only", "vro", "orchestrator", "configuration"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to find the configuration elements that workflows read their settings "
            "from, and to read the attributes of one element."
        ),
        "output_shape": (
            "{element, items, total, count, more_rows}. List: element is null and items "
            "are element rows {id, name, ...}. One element: element is {id, name, version, "
            "description, category_id} and items are {name, type, description, masked, "
            "value}; a secret value is ***REDACTED***."
        ),
        "next_step": (
            "Call again with config_element_id set to the id of a row to read its "
            "attributes. MEHO never shows a secret value."
        ),
        "result_scalars": _SCALARS,
    },
)

_PACKAGE_LIST = VroTypedOp(
    op_id="vro.package.list",
    handler_attr="package_list",
    summary="List Orchestrator packages and their versions.",
    description=(
        "Reads GET /vco/api/packages. Returns one flat row per package with its name, "
        "version and description, as the server sends them."
    ),
    parameter_schema={
        "type": "object",
        "properties": {"max_results": MAX_RESULTS_SCHEMA},
        "additionalProperties": False,
        "examples": [{}],
    },
    response_schema=_list_schema(
        "One flat row per package.",
        {
            "name": string_schema("Package name, for example com.example.package."),
            "id": string_schema("Package id, when the server sends one."),
            "version": string_schema("Package version."),
            "description": string_schema("Package description."),
            "href": string_schema("API address of the package."),
        },
    ),
    group_key=GROUP_LIBRARY,
    tags=("read-only", "vro", "orchestrator", "package"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to see which packages are installed and at which version, for example "
            "before or after a content update."
        ),
        "output_shape": (
            "{items: [{name, id, version, description, href, ...}], total, count, "
            "more_rows}. A long list is behind a result handle."
        ),
        "next_step": "Report the packages and versions.",
        "result_scalars": _SCALARS,
    },
)

VRO_LIBRARY_OPS: Final[tuple[VroTypedOp, ...]] = (
    _ACTION_LIST,
    _CONFIG_ELEMENT_LIST,
    _PACKAGE_LIST,
)
