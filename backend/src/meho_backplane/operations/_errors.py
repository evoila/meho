# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Structured :class:`OperationResult` builders for the G0.6 dispatcher.

The dispatcher (T5 #396) never raises -- every operator-visible failure
mode returns one of these :class:`OperationResult` shapes. Keeping the
builders here (rather than inline in :mod:`dispatcher`) lets the
dispatcher's :func:`dispatch` body stay focused on control flow.

Each builder owns one ``error_code`` from the contract documented in
:mod:`meho_backplane.operations.dispatcher`'s module docstring:
``unknown_op`` / ``invalid_params`` / ``no_connector`` /
``handler_unreachable`` / ``denied`` / ``connector_error``. The
``status`` field maps to ``OperationResult.status``; the ``error_code``
lives in ``extras`` so callers can both string-match the ``error``
field (``error.startswith("unknown_op:")``) and parse the code for
structured handling.
"""

from __future__ import annotations

from typing import Any

from meho_backplane.connectors import OperationResult
from meho_backplane.operations.reducer import ResultHandle

__all__ = [
    "result_connector_error",
    "result_denied",
    "result_handler_unreachable",
    "result_invalid_params",
    "result_no_connector",
    "result_unknown_op",
    "status_code_for_result",
    "wrap_ok_result",
]

#: Cap on the exception-message length recorded in the ``connector_error``
#: extras payload. A misbehaving connector could embed a credential into
#: a stringified exception; 256 chars is enough for an operator to
#: recognise the failure shape while capping the leak surface.
_EXC_MESSAGE_CAP: int = 256


def result_unknown_op(op_id: str, known_op_count: int, duration_ms: float) -> OperationResult:
    """Descriptor lookup miss for *(product, version, impl_id, op_id)*."""
    return OperationResult(
        status="error",
        op_id=op_id,
        error=f"unknown_op: {op_id}",
        duration_ms=duration_ms,
        extras={"error_code": "unknown_op", "known_op_count": known_op_count},
    )


def result_invalid_params(
    op_id: str,
    validation_errors: list[dict[str, Any]],
    duration_ms: float,
) -> OperationResult:
    """JSON Schema validation against ``parameter_schema`` failed."""
    return OperationResult(
        status="error",
        op_id=op_id,
        error=f"invalid_params: {len(validation_errors)} validation error(s)",
        duration_ms=duration_ms,
        extras={
            "error_code": "invalid_params",
            "validation_errors": validation_errors,
        },
    )


def result_no_connector(
    op_id: str, product: str, version: str, duration_ms: float
) -> OperationResult:
    """Resolver miss -- no registered impl for *(product, version)*."""
    return OperationResult(
        status="error",
        op_id=op_id,
        error=f"no_connector: no implementation for product={product!r} version={version!r}",
        duration_ms=duration_ms,
        extras={"error_code": "no_connector", "product": product, "version": version},
    )


def result_handler_unreachable(
    op_id: str, handler_ref: str, exc: BaseException, duration_ms: float
) -> OperationResult:
    """``importlib`` couldn't resolve ``handler_ref`` to a callable."""
    return OperationResult(
        status="error",
        op_id=op_id,
        error=f"handler_unreachable: {handler_ref}",
        duration_ms=duration_ms,
        extras={
            "error_code": "handler_unreachable",
            "handler_ref": handler_ref,
            "exception_class": type(exc).__name__,
        },
    )


def result_denied(op_id: str, reason: str, duration_ms: float) -> OperationResult:
    """Policy gate denied the call (``requires_approval`` in v0.2)."""
    return OperationResult(
        status="denied",
        op_id=op_id,
        error=f"denied: {reason}",
        duration_ms=duration_ms,
        extras={"error_code": "denied", "reason": reason},
    )


def result_connector_error(
    op_id: str,
    exc: BaseException,
    duration_ms: float,
) -> OperationResult:
    """Connector / handler raised. Exception class + capped message land in extras."""
    msg = str(exc)
    if len(msg) > _EXC_MESSAGE_CAP:
        msg = msg[:_EXC_MESSAGE_CAP] + "...<truncated>"
    return OperationResult(
        status="error",
        op_id=op_id,
        error=f"connector_error: {type(exc).__name__}",
        duration_ms=duration_ms,
        extras={
            "error_code": "connector_error",
            "exception_class": type(exc).__name__,
            "exception_message": msg,
        },
    )


def wrap_ok_result(
    op_id: str, payload: Any, duration_ms: float, handle: ResultHandle | None
) -> OperationResult:
    """Build a successful :class:`OperationResult` from a reducer's output.

    :class:`OperationResult.result` is typed ``dict[str, Any] |
    list[Any] | None``; scalars are wrapped in a single-key
    ``{"value": ...}`` dict so the contract stays honest. The
    :class:`ResultHandle` (when non-None) is recorded in ``extras`` --
    T6 (#397) will promote it to a first-class field on
    :class:`OperationResult`.
    """
    if payload is None or isinstance(payload, (dict, list)):
        result_value: dict[str, Any] | list[Any] | None = payload
    else:
        result_value = {"value": payload}
    extras: dict[str, Any] = {}
    if handle is not None:
        extras["result_handle"] = repr(handle)
    return OperationResult(
        status="ok",
        op_id=op_id,
        result=result_value,
        duration_ms=duration_ms,
        extras=extras,
    )


def status_code_for_result(result_status: str) -> int:
    """Map a dispatcher ``result_status`` to a synthetic HTTP-shaped status code.

    The ``audit_log.status_code`` column is NOT NULL :class:`int` --
    optimised for the HTTP middleware path. The dispatcher contract is
    not HTTP, so the dispatcher synthesises one: ``200`` for ok,
    ``500`` for error, ``403`` for denied. The synthetic values are
    not surfaced to operators; the canonical signal lives in
    ``payload["result_status"]`` on the audit row.
    """
    if result_status == "ok":
        return 200
    if result_status == "denied":
        return 403
    return 500
