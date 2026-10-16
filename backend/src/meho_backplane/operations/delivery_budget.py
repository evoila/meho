# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Wire-size budgets for delivered tool results (#3641).

A result is delivered over MCP as a ``tools/call`` response. For a tool
that declares an ``outputSchema`` the payload goes on the wire in two
blocks: once as the JSON text of ``content[0].text`` and once as the
``structuredContent`` object (MCP 2025-06-18, Tools / Output Schema). The
text block is a JSON string inside the outer JSON-RPC body, so every quote,
backslash and escape sequence it holds is escaped a second time.

:func:`measure_delivered_wire_bytes` finds the real size by running the
same encoders the transport runs and adding up the encoded lengths. It does
not guess with a constant factor, and it measures bytes only (it is not a
token count).

Two terminal ceilings use it. Both sit above the earlier shaping checks
(the reducer's inline sample budget and the ``result_query`` row-bytes
check), which run before encoding:

* :func:`bound_complete_response` -- ``call_operation``. A completed
  operation whose response is too large keeps ``status="ok"`` and gets the
  ``delivery`` state ``partial`` (the receipt and the result handle still
  fit) or ``unavailable`` (only the receipt fits). It is never relabelled
  as an error and never cut silently.
* :mod:`meho_backplane.operations.result_query` -- the read-back paths
  raise the recoverable ``output_too_large`` error instead.

Both REST twins share the same ceiling. REST sends the payload once, so the
MCP measurement is the larger of the two and the safe bound for both.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from meho_backplane.connectors.schemas import OperationResult

__all__ = [
    "bound_complete_response",
    "measure_delivered_wire_bytes",
    "tool_result_text",
]


def tool_result_text(result: Any) -> str:
    """Return the ``content[0].text`` string for a ``tools/call`` result.

    The MCP dispatcher builds the text block with this function, so the
    measurement below encodes exactly the string that is sent.
    """
    return json.dumps(result)


def _transport_encode(value: Any) -> bytes:
    """Encode *value* the way the HTTP layer writes the JSON-RPC body.

    Mirrors ``starlette.responses.JSONResponse.render``: compact separators,
    UTF-8 output, no ASCII escaping. A test pins the two as byte-equal.
    """
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        indent=None,
        separators=(",", ":"),
    ).encode("utf-8")


def measure_delivered_wire_bytes(result: Any, *, structured: bool = True) -> int:
    """Return the bytes *result* takes on the ``tools/call`` wire.

    Sums the encoded text block (the JSON text written as a JSON string,
    escaping included) and, when *structured* is true (the tool declares an
    ``outputSchema``), the encoded ``structuredContent`` object. The fixed
    JSON-RPC framing around them is small and is not counted.
    """
    total = len(_transport_encode(tool_result_text(result)))
    if structured:
        total += len(_transport_encode(result))
    return total


_PARTIAL_REMEDIATION = (
    "The operation completed, but its full response was too large to deliver "
    "({size} bytes; the limit is {limit}). Read the result through `result_query` "
    "with the handle. Do not re-invoke the operation."
)
_UNAVAILABLE_REMEDIATION = (
    "The operation completed, but its response was too large to deliver "
    "({size} bytes; the limit is {limit}). Inspect its audit record when an "
    "audit_id is present, or inspect operation status. Do not re-invoke it."
)


def _trimmed(
    result: OperationResult,
    delivery: Literal["partial", "unavailable"],
    size: int,
    limit: int,
) -> OperationResult:
    template = _PARTIAL_REMEDIATION if delivery == "partial" else _UNAVAILABLE_REMEDIATION
    return OperationResult(
        status=result.status,
        op_id=result.op_id,
        duration_ms=result.duration_ms,
        handle=result.handle if delivery == "partial" else None,
        audit_id=result.audit_id,
        delivery=delivery,
        extras={
            "remediation": template.format(size=size, limit=limit),
            "delivered_bytes": size,
            "delivery_max_bytes": limit,
        },
    )


def bound_complete_response(result: OperationResult, max_bytes: int) -> OperationResult:
    """Keep a completed ``call_operation`` response within *max_bytes* on the wire.

    Only ``status="ok"`` responses are bounded: the operation completed and
    the receipt must reach the caller. An in-budget response is returned
    unchanged. Otherwise the inline ``result`` and the extras are dropped:

    * ``delivery="partial"`` when a result handle exists and the envelope
      with the handle and ``audit_id`` fits -- the caller reads the full
      set through ``result_query``;
    * ``delivery="unavailable"`` otherwise -- the envelope keeps only the
      receipt and the remediation text.

    The status stays ``ok`` in both cases, so a completed write is never
    reported as an error because its response was too large.
    """
    if result.status != "ok":
        return result
    size = measure_delivered_wire_bytes(result.model_dump(mode="json"))
    if size <= max_bytes:
        return result
    if result.handle is not None:
        partial = _trimmed(result, "partial", size, max_bytes)
        if measure_delivered_wire_bytes(partial.model_dump(mode="json")) <= max_bytes:
            return partial
    return _trimmed(result, "unavailable", size, max_bytes)
