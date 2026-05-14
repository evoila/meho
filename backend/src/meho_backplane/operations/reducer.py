# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""JSONFlux reducer protocol + v0.2 pass-through default.

G0.6-T6 (#397) of Initiative #388. The :class:`Reducer` :class:`typing.Protocol`
is the contract every JSONFlux reducer satisfies; the dispatcher invokes
:meth:`Reducer.reduce` after a handler returns and before the audit / broadcast
phase fires (see :mod:`meho_backplane.operations.dispatcher`). v0.2 ships only
:class:`PassThroughReducer` — the no-op default that returns the raw payload
verbatim with a ``None`` :class:`ResultHandle`. Real reduction logic
(set-shaped payload reduction, result-handle store, MinIO/S3 spill, the
``result_query`` / ``result_aggregate`` meta-tools) ships in a follow-on
Initiative once the first generic-ingested connectors produce real payloads
to calibrate against.

The contract design is **future-proof hook with no-op default**: every
connector ships JSONFlux-aware from day 1; swapping the real reducer in later
touches one registration call, not every connector. See the parent Initiative
#388 and v0.1-spec §"JSONFlux / result handles" L294-311.

:class:`ResultHandle` lives in :mod:`meho_backplane.connectors.schemas`
alongside :class:`~meho_backplane.connectors.OperationResult` (which gained an
optional ``handle`` field in T6) and is re-exported here so the public path
``from meho_backplane.operations.reducer import ResultHandle`` keeps working.
The placement avoids an ``operations → connectors → operations`` import cycle
the dispatcher would otherwise need :func:`pydantic.BaseModel.model_rebuild`
plumbing to break.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from meho_backplane.connectors.schemas import ResultHandle

__all__ = [
    "PassThroughReducer",
    "Reducer",
    "ResultHandle",
]


@runtime_checkable
class Reducer(Protocol):
    """JSONFlux reduction contract.

    The dispatcher always invokes :meth:`reduce` after the handler returns;
    the return value flows into the audit row's payload, the broadcast
    event, and the :class:`~meho_backplane.connectors.OperationResult` the
    caller sees. v0.2 ships :class:`PassThroughReducer` as the only impl;
    the real reducer (post-G0.6) returns a reduced payload + a
    :class:`ResultHandle` for large set-shaped responses while leaving
    small / scalar payloads alone.

    Implementations MUST tolerate every response shape connectors emit
    (``dict``, ``list``, ``None``, scalar JSON via a wrapping dict). The
    Protocol is intentionally permissive in v0.2 so the dispatcher's
    reducer invocation can land without breaking on a response shape the
    real reducer hasn't yet considered.

    :func:`typing.runtime_checkable` is set so tests can assert
    ``isinstance(my_reducer, Reducer)`` against the structural contract;
    Pydantic doesn't validate Protocol membership and the dispatcher
    relies on duck typing, so this check is for test ergonomics only.
    """

    async def reduce(
        self,
        payload: Any,
        schema: dict[str, Any] | None,
        context: dict[str, Any] | None = None,
    ) -> tuple[Any, ResultHandle | None]:
        """Return ``(reduced_payload, handle_or_None)`` for *payload*.

        Args:
            payload: The raw handler return value. Typically ``dict`` or
                ``list``; reducers MUST tolerate ``None`` and scalars too.
            schema: The descriptor's ``response_schema`` when known, else
                ``None``. Real reducers use this to drive set-detection
                heuristics (``type=array``) and column-extraction; the
                pass-through default ignores it.
            context: Optional dispatcher context — ``op_id``,
                ``operator_sub``, ``target_id``, etc. Available for
                logging and future routing decisions (e.g. per-operator
                reducer policy). The pass-through default ignores it;
                v0.2 callers may pass ``None``.

        Returns:
            A 2-tuple ``(reduced_payload, handle_or_None)``:

            - ``(summary_payload, ResultHandle)`` when reduction happened.
              ``summary_payload`` is the inlined summary the caller sees;
              the handle addresses the full payload in the backing store.
            - ``(raw_payload, None)`` for pass-through. ``raw_payload``
              equals the input ``payload``.
        """
        ...


class PassThroughReducer:
    """v0.2 default — return the payload unchanged, never produce a handle.

    Structurally implements :class:`Reducer`. Used by the dispatcher when
    no real reducer is configured (the only case in v0.2; per-op reducer
    selection lands alongside the real reduction logic in a follow-on
    Initiative).

    Idempotency: :meth:`reduce` is stateless and pure — calling it twice
    on the same input returns identical output. The dispatcher
    instantiates one module-level instance and reuses it across every
    dispatch call; concurrent dispatches share the same instance safely.
    """

    async def reduce(
        self,
        payload: Any,
        schema: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> tuple[Any, ResultHandle | None]:
        """Pass *payload* through verbatim; never produce a handle."""
        return payload, None
