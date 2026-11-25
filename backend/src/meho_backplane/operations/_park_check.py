# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""An op's opt-in check that runs before its call waits for approval.

A call that waits for approval is stored as an approval request with its
params as they are, so a reviewer can read them and the approved call can
run with them. Some values must never be stored there, for example a
secret that the caller gave as a plain value. Name-based redaction does
not know every such field, so the op itself says what it refuses.

An op turns the check on with :func:`register_park_check`. The check gets
the same :class:`~meho_backplane.operations._preview.PreviewContext` the
preview hooks get and answers with the reason it refuses the call, or
``None``. The reason is shown to the caller, so it must name no param
value.

:func:`park_check_refusal` runs it at every point where a call would wait
for approval, before anything is stored:

* the dispatcher, before it stores the approval request
  (``dispatcher._handle_needs_approval``);
* the gate of a step of a multi-step operation
  (``composite.enforce_subop_policy``);
* ``preview_operation``, when the call would wait for approval
  (``_request_preview.preview_dispatch``), so the preview gives the same
  answer as the call.

A refusal is the ``refused_before_approval`` result
(:func:`~meho_backplane.operations._errors.result_park_check_refused`).

An op can also turn on a check that runs before the policy gate
(:func:`register_gate_check`), for a check that must pass before MEHO reads
anything of the vendor for the call. :func:`gate_check_refusal` runs it in
the dispatcher right before the policy gate, so before the reads that decide
who must approve and before any approval card is built, and
``preview_operation`` runs it first. The approved run skips it: the handler
checks again.
The check is fail-closed: a check that raises refuses the call, with a
reason that names only the exception class. An expired login during the
check is renewed once (:func:`_refresh_rejected_session`).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import httpx
import structlog

from meho_backplane.operations._errors import is_auth_failed_status, result_park_check_refused
from meho_backplane.operations._preview import PreviewContext

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors import OperationResult
    from meho_backplane.db.models import EndpointDescriptor

__all__ = [
    "ParkCheck",
    "gate_check_refusal",
    "park_check_refusal",
    "park_check_registered",
    "register_gate_check",
    "register_park_check",
    "run_park_check",
]

_log = structlog.get_logger(__name__)

#: An op's check before the wait for approval: the reason it refuses the
#: call (a text that names no param value), or ``None`` to let it wait.
ParkCheck = Callable[[PreviewContext], Awaitable[str | None]]

#: ``op_id`` -> registered check. Opt-in: an op without one waits for
#: approval with its params as they are.
_PARK_CHECKS: dict[str, ParkCheck] = {}

#: ``op_id`` -> check that runs before the policy gate (:func:`register_gate_check`).
_GATE_CHECKS: dict[str, ParkCheck] = {}


def register_park_check(op_id: str, check: ParkCheck) -> None:
    """Register *check* as the check *op_id* runs before it waits for approval.

    Registering again overwrites, so a re-import (test reload) is safe.
    """
    _PARK_CHECKS[op_id] = check


def register_gate_check(op_id: str, check: ParkCheck) -> None:
    """Register *check* as the check *op_id* runs before the policy gate.

    Same shape and the same fail-closed run as a check before the wait for
    approval. Registering again overwrites.
    """
    _GATE_CHECKS[op_id] = check


def park_check_registered(op_id: str) -> bool:
    """Whether *op_id* has a check before the wait for approval."""
    return op_id in _PARK_CHECKS


async def _refresh_rejected_session(ctx: PreviewContext, exc: httpx.HTTPStatusError) -> bool:
    """Drop the login the vendor rejected; ``True`` when the check can run again.

    A check can read the vendor with the connector's cached login. When
    that login has expired, the read answers an auth-class status (a 401).
    The data path renews it in the dispatcher's session-recovery step, but
    the check runs before that step. So the same thing happens here: the
    connector's ``invalidate_session(target, rejected=...)`` hook drops the
    rejected login, and the caller runs the check once more, which logs in
    again. A connector without the hook is not retried, as on the data
    path.
    """
    # Imported lazily: the dispatcher imports this module.
    from meho_backplane.operations.dispatcher import _profile_expiry_statuses

    status_code = exc.response.status_code
    if not is_auth_failed_status(status_code, _profile_expiry_statuses(ctx.connector_instance)):
        return False
    invalidate_session = getattr(ctx.connector_instance, "invalidate_session", None)
    if not callable(invalidate_session):
        return False
    _log.info("park_check_session_refreshed", op_id=ctx.descriptor.op_id, status_code=status_code)
    await invalidate_session(ctx.target, rejected=exc.request)
    return True


async def run_park_check(ctx: PreviewContext) -> str | None:
    """Run the op's check, if one is registered; return the refusal reason.

    Returns ``None`` when no check is registered or the check lets the call
    wait. When the vendor rejects the cached login during the check, the
    login is dropped and the check runs once more. A check that raises
    refuses the call (fail-closed): the reason names the exception class
    only, so no param value and no vendor text reaches the caller.
    """
    return await _run_check(ctx, _PARK_CHECKS.get(ctx.descriptor.op_id))


async def _run_check(ctx: PreviewContext, check: ParkCheck | None) -> str | None:
    """Run *check* as :func:`run_park_check` describes."""
    op_id = ctx.descriptor.op_id
    if check is None:
        return None
    try:
        try:
            return await check(ctx)
        except httpx.HTTPStatusError as exc:
            if not await _refresh_rejected_session(ctx, exc):
                raise
        return await check(ctx)
    except Exception as exc:
        # The class name only: a traceback with locals could show a param
        # value, a login header or a vendor text.
        _log.warning(
            "park_check_failed",
            op_id=op_id,
            operator_sub=getattr(ctx.operator, "sub", None),
            error_type=type(exc).__name__,
        )
        return (
            "the check that runs before the wait for approval failed "
            f"({type(exc).__name__}); call the operation again"
        )


async def park_check_refusal(
    *,
    op_id: str,
    connector_id: str,
    descriptor: EndpointDescriptor,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    duration_ms: float,
) -> OperationResult | None:
    """The ``refused_before_approval`` result when the op's check says no.

    Returns ``None`` when no check is registered or the check lets the call
    wait. Resolves the connector the same way the park-time preview does;
    a connector that does not resolve reaches the check as
    ``connector_instance=None``, and the check decides what that means.
    Writes nothing: the caller records the refusal.
    """
    return await _refusal(
        _PARK_CHECKS.get(descriptor.op_id),
        op_id=op_id,
        connector_id=connector_id,
        descriptor=descriptor,
        operator=operator,
        target=target,
        params=params,
        duration_ms=duration_ms,
    )


async def gate_check_refusal(
    *,
    op_id: str,
    connector_id: str,
    descriptor: EndpointDescriptor,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    duration_ms: float,
) -> OperationResult | None:
    """The ``refused_before_approval`` result when the op's check before the gate says no.

    The same as :func:`park_check_refusal`, for the check of
    :func:`register_gate_check`.
    """
    return await _refusal(
        _GATE_CHECKS.get(descriptor.op_id),
        op_id=op_id,
        connector_id=connector_id,
        descriptor=descriptor,
        operator=operator,
        target=target,
        params=params,
        duration_ms=duration_ms,
    )


async def _refusal(
    check: ParkCheck | None,
    *,
    op_id: str,
    connector_id: str,
    descriptor: EndpointDescriptor,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    duration_ms: float,
) -> OperationResult | None:
    """Run *check* for the call; the refusal result, or ``None``."""
    if check is None:
        return None
    # Imported lazily: the dispatcher imports this module.
    from meho_backplane.operations.dispatcher import _resolve_connector_instance

    try:
        connector_instance, resolution_error, _ = await _resolve_connector_instance(
            descriptor, target
        )
    except Exception:
        connector_instance, resolution_error = None, None
    if resolution_error is not None:
        connector_instance = None
    reason = await _run_check(
        PreviewContext(
            descriptor=descriptor,
            connector_instance=connector_instance,
            operator=operator,
            target=target,
            params=params,
            connector_id=connector_id,
        ),
        check,
    )
    if reason is None:
        return None
    return result_park_check_refused(op_id, reason, duration_ms)
