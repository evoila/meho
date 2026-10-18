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
  ``delivery`` state ``partial`` (the receipt and a readable result handle
  still fit) or ``unavailable`` (only the receipt fits). It is never
  relabelled as an error and never cut silently.
* :mod:`meho_backplane.operations.result_query` -- the read-back paths
  raise the recoverable ``output_too_large`` error instead.

The receipt of a completed operation (``status``, ``op_id``, ``audit_id``,
``delivery`` and the remediation text) must always reach the caller. Two
checks make sure it can:

* The ``call_operation`` ceiling has a floor,
  :data:`~meho_backplane.settings.RESULT_COMPLETE_RESPONSE_WIRE_MIN_BYTES`.
  :class:`~meho_backplane.settings.Settings` rejects a smaller value at
  startup, before any operation runs.
* The receipt grows with the ``op_id``. :func:`refuse_undeliverable_receipt`
  runs before dispatch and measures the widest receipt the ``op_id`` can
  produce, escaping included. A call whose receipt cannot fit the ceiling
  is refused with ``error_code="receipt_too_large"`` and never runs.

Both REST twins share the same ceiling. REST sends the payload once, so the
MCP measurement is the larger of the two and the safe bound for both.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Literal
from uuid import UUID

import structlog

from meho_backplane.connectors.schemas import OperationResult, ResultHandle
from meho_backplane.operations._errors import result_receipt_too_large
from meho_backplane.settings import RESULT_COMPLETE_RESPONSE_WIRE_MIN_BYTES

__all__ = [
    "RESULT_COMPLETE_RESPONSE_WIRE_MIN_BYTES",
    "bound_complete_response",
    "handle_is_readable",
    "measure_delivered_wire_bytes",
    "receipt_wire_bytes",
    "refuse_undeliverable_receipt",
    "tool_result_text",
]

_log = structlog.get_logger(__name__)


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


def handle_is_readable(handle: ResultHandle | None) -> bool:
    """Return whether ``result_query`` can read the rows behind *handle*.

    A handle alone does not prove its rows were stored: the reducer also
    mints handles whose spill was skipped or failed, and marks them with
    ``fetch_more.drill_in.available=False``. Only a handle whose drill-in
    is available can be read back.
    """
    return (
        handle is not None
        and handle.fetch_more is not None
        and handle.fetch_more.drill_in.available
    )


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
    extras: dict[str, Any] = {
        "remediation": template.format(size=size, limit=limit),
        "delivered_bytes": size,
        "delivery_max_bytes": limit,
    }
    if result.status == "ok":
        # The operation already ran; running it again could repeat a change.
        extras["retry"] = False
    return OperationResult(
        status=result.status,
        op_id=result.op_id,
        duration_ms=result.duration_ms,
        handle=result.handle if delivery == "partial" else None,
        audit_id=result.audit_id,
        delivery=delivery,
        extras=extras,
    )


#: Stand-ins for the receipt fields that are unknown before the operation
#: runs. Each is the widest JSON text its field can take, so the receipt
#: measured from them is never smaller than the one a completed run sends.
_WIDEST_DURATION_MS = -sys.float_info.max  # longest float text, sign included
_WIDEST_BYTE_COUNT = 2 * sys.maxsize  # two encoded blocks can never add up to more
_PLACEHOLDER_AUDIT_ID = UUID(int=0)  # every UUID takes 36 characters


def receipt_wire_bytes(op_id: str, max_bytes: int) -> int:
    """Return the most bytes the ``unavailable`` receipt for *op_id* can take.

    The receipt is :func:`bound_complete_response`'s last resort. Its only
    variable parts are the ``op_id``, the ``audit_id``, ``duration_ms`` and
    the two byte counts in the remediation text. The ``op_id`` is known
    before dispatch; the others are filled with the widest value each can
    take, so the result is an upper bound of the receipt a completed run of
    this ``op_id`` would send under *max_bytes*. It is measured with the
    same encoders as the real response, so escaping is counted.
    """
    widest = OperationResult(
        status="ok",
        op_id=op_id,
        duration_ms=_WIDEST_DURATION_MS,
        audit_id=_PLACEHOLDER_AUDIT_ID,
    )
    receipt = _trimmed(widest, "unavailable", _WIDEST_BYTE_COUNT, max_bytes)
    return measure_delivered_wire_bytes(receipt.model_dump(mode="json"))


def refuse_undeliverable_receipt(op_id: str, max_bytes: int) -> OperationResult | None:
    """Refuse a ``call_operation`` whose receipt could not be delivered.

    Runs before dispatch. Returns ``None`` when the widest receipt for
    *op_id* (see :func:`receipt_wire_bytes`) fits *max_bytes*, which is the
    case for any realistic ``op_id``. Otherwise returns the
    ``receipt_too_large`` error envelope and the operation must not run:
    a completed operation whose receipt cannot be delivered would lose its
    ``audit_id`` or break the ceiling, and refusing before the run avoids
    both. Nothing ran, so the call is safe to repeat.
    """
    receipt_bytes = receipt_wire_bytes(op_id, max_bytes)
    if receipt_bytes <= max_bytes:
        return None
    return result_receipt_too_large(op_id, receipt_bytes, max_bytes, 0.0)


def bound_complete_response(result: OperationResult, max_bytes: int) -> OperationResult:
    """Keep a completed ``call_operation`` response within *max_bytes* on the wire.

    Only ``status="ok"`` responses are bounded: the operation completed and
    the receipt must reach the caller. An in-budget response is returned
    unchanged. Otherwise the inline ``result`` and the extras are dropped:

    * ``delivery="partial"`` when the result handle can be read back
      through ``result_query`` (see :func:`handle_is_readable`) and the
      envelope with the handle and ``audit_id`` fits;
    * ``delivery="unavailable"`` otherwise -- the envelope keeps only the
      receipt and the remediation text. A handle whose rows were never
      stored is dropped too, so the caller is never sent to a read that
      cannot work.

    The status stays ``ok`` in both cases, so a completed write is never
    reported as an error because its response was too large.

    The receipt is the smallest honest response. It fits by construction:
    :func:`refuse_undeliverable_receipt` ran before the operation and
    measured the widest receipt this ``op_id`` can produce against the same
    *max_bytes*. The final size is still checked. Should it ever be over
    the ceiling, the receipt is delivered anyway and a warning is logged,
    because dropping the ``audit_id`` of a completed write would be worse
    than the overrun. A caller that skipped the pre-check is the only way
    to reach that branch.
    """
    if result.status != "ok":
        return result
    size = measure_delivered_wire_bytes(result.model_dump(mode="json"))
    if size <= max_bytes:
        return result
    if handle_is_readable(result.handle):
        partial = _trimmed(result, "partial", size, max_bytes)
        if measure_delivered_wire_bytes(partial.model_dump(mode="json")) <= max_bytes:
            return partial
    receipt = _trimmed(result, "unavailable", size, max_bytes)
    receipt_size = measure_delivered_wire_bytes(receipt.model_dump(mode="json"))
    if receipt_size > max_bytes:
        _log.warning(
            "delivery_receipt_over_ceiling",
            op_id=result.op_id,
            audit_id=str(result.audit_id) if result.audit_id is not None else None,
            receipt_bytes=receipt_size,
            delivery_max_bytes=max_bytes,
        )
    return receipt
