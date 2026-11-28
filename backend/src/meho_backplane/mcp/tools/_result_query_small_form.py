# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The small form of ``result_query`` (``RESULT_QUERY_SMALL_FORM``, #4174).

Small models call ``result_query`` and MEHO accepts the calls, but they
often ask the wrong thing: a field that is not on the handle, or a value
that is not in the data. The small form helps them fix the call. It is
off by default; with it off, the tool and its answers do not change.

With the switch on:

* an error in ``query`` starts with the path of the wrong input, for
  example ``query.filter[0].field: unknown field 'state' ...``. The
  ``tools/call`` input check names the path with the switch on or off
  (:func:`~meho_backplane.input_refusal.refusal_text`);
* a filter that matches no stored row adds ``no_match`` to the answer, with
  a few values each filter field really has
  (:mod:`meho_backplane.operations.result_query_no_match`);
* ``query.filter`` takes one more operator, ``contains``: the text holds
  the value, in any case.

There is no new parameter. The REST route keeps its own body and does not
change.
"""

from __future__ import annotations

import copy
from typing import Any, Final

from pydantic import ValidationError

from meho_backplane.jsonflux.query.contract import QueryContractError
from meho_backplane.mcp.field_path import field_path
from meho_backplane.mcp.registry import ToolDefinition
from meho_backplane.settings import get_settings

__all__ = [
    "contract_error_text",
    "small_form_for_settings",
    "small_form_on",
    "spec_error_text",
]

#: The description of ``query.filter[].op`` while the switch is on.
_OP_DESCRIPTION: Final = "`contains`: the text holds `value`, in any case."

#: The ``no_match`` part of the answer while the switch is on.
_NO_MATCH_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "description": (
        "Only when the filter matched no row: `values` has values each filter "
        "field really has. Fix the filter and query again."
    ),
    "properties": {
        "note": {"type": "string"},
        "values": {"type": "object"},
    },
}


def small_form_on() -> bool:
    """True when the ``RESULT_QUERY_SMALL_FORM`` switch is on."""
    return get_settings().result_query_small_form


def small_form_for_settings(definition: ToolDefinition) -> ToolDefinition:
    """Return ``result_query`` as it is shown while the switch is on or off.

    Off: the registered tool, unchanged. On: ``contains`` in the filter
    operators, ``no_match`` in the output schema, and input errors that
    name the path of the wrong input.
    """
    if not small_form_on():
        return definition
    schema = copy.deepcopy(definition.inputSchema)
    op = schema["properties"]["query"]["properties"]["filter"]["items"]["properties"]["op"]
    op["enum"] = [*op["enum"], "contains"]
    op["description"] = _OP_DESCRIPTION
    output = copy.deepcopy(definition.outputSchema or {})
    output.setdefault("properties", {})["no_match"] = _NO_MATCH_SCHEMA
    return definition.model_copy(update={"inputSchema": schema, "outputSchema": output})


def spec_error_text(exc: ValidationError) -> str:
    """The error text for a ``query`` that is not a valid query, with its path."""
    first = exc.errors()[0]
    path = field_path(("query", *first.get("loc", ())))
    return (
        f"invalid query spec: {exc.error_count()} validation error(s); "
        f"first: {path}: {first.get('msg', 'invalid')}"
    )


def contract_error_text(exc: QueryContractError) -> str:
    """The error text for a query the handle cannot run, with its path when known."""
    if exc.loc is None:
        return str(exc)
    return f"query.{exc.loc}: {exc}"
