# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""JSONFlux reducer protocol -- pass-through stub for the G0.6-T5 dispatcher.

T6 (#397) ships the real :class:`Reducer` ABC + :class:`PassThroughReducer`
+ :class:`ResultHandle` Pydantic model. T5 (this PR, #396) only needs the
dispatcher to *invoke* a reducer so the integration point is locked in
v0.2 -- the actual reduction logic ships later without touching every
caller. To keep that promise:

* :class:`Reducer` is a :class:`typing.Protocol`. T6 can swap to a
  concrete ABC + module hierarchy without breaking the dispatcher import.
* :class:`PassThroughReducer` is the v0.2 default. It returns the
  response verbatim and a ``None`` :class:`ResultHandle` -- the
  dispatcher's response shape is identical to what a connector would
  have returned pre-substrate.
* :class:`ResultHandle` is a thin sentinel. T6 promotes it to a Pydantic
  model with row-count / spill-uri / schema-uri fields; today the type
  exists so the dispatcher can already type-hint the second-element of
  the reducer's return tuple.

This module deliberately ships **no behaviour** beyond the pass-through.
T6 is responsible for the >50-row / >4 KB threshold logic, MinIO/S3
spill, set-shaped payload reduction, and the schema-aware view. Anything
beyond "preserve the dispatcher seam" lives in T6 by acceptance contract.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

__all__ = [
    "PassThroughReducer",
    "Reducer",
    "ResultHandle",
]


class ResultHandle:
    """Placeholder for the T6 ResultHandle model.

    The dispatcher type-hints the second tuple-element of the reducer's
    return as ``ResultHandle | None``. T6 promotes this class to a
    Pydantic model carrying ``schema_uri`` / ``spill_uri`` / ``row_count``
    fields; v0.2's pass-through reducer always returns ``None`` here so
    no production caller ever instantiates the placeholder.
    """


@runtime_checkable
class Reducer(Protocol):
    """JSONFlux reducer contract.

    A reducer turns a raw connector response into a ``(summary_payload,
    handle_or_None)`` tuple. The summary is what the dispatcher returns
    to the caller; the handle (when non-None) is the addressable form
    the caller can use to fetch the full payload via a follow-up
    operation (T6 territory).

    v0.2 reducers MUST tolerate every response shape connectors emit
    (``dict``, ``list``, scalar JSON via a wrapping dict). T6 will
    formalise the contract; the protocol stays permissive in T5 so the
    dispatcher's reducer invocation can land without breaking on a
    response shape T6 hasn't yet considered.
    """

    async def reduce(
        self,
        response: Any,
        response_schema: dict[str, Any] | None,
    ) -> tuple[Any, ResultHandle | None]:
        """Return ``(summary_payload, handle_or_None)`` for *response*."""
        ...


class PassThroughReducer:
    """v0.2 default -- return the response unchanged, no handle.

    Implements the :class:`Reducer` protocol structurally. Used by the
    dispatcher when no per-op reducer is configured (the only case in
    v0.2; T6 ships per-op reducer selection alongside the real reduction
    logic).

    Idempotency note: calling :meth:`reduce` twice with the same input
    returns identical output. The reducer is stateless; the dispatcher
    instantiates one module-level instance and reuses it across every
    dispatch call.
    """

    async def reduce(
        self,
        response: Any,
        response_schema: dict[str, Any] | None,
    ) -> tuple[Any, ResultHandle | None]:
        """Pass *response* through verbatim; never produce a handle."""
        return response, None
