# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``/api/v1/gateway/*`` — outbound long-poll command plane (runner-facing).

Initiative #2415 (Remote execution gateway), Task #2498. Two routes a
satellite runner dials outbound to receive and report centrally-authorized
operations, over the durable ``gateway_command`` queue
(:mod:`meho_backplane.gateway.queue`):

* ``GET /api/v1/gateway/{runner}/next?wait=N`` — the blocking long-poll.
  Holds up to ``N`` seconds (default 25, clamped to the exported
  :data:`~meho_backplane.gateway.queue.GATEWAY_LONGPOLL_MAX_WAIT_SECONDS`;
  ``wait=0`` = one immediate claim attempt), returning ``200`` with the
  claimed command envelope or ``204 No Content`` on timeout.
* ``POST /api/v1/gateway/{runner}/result`` — the runner reports an
  outcome. ``200`` on success, ``404`` for an unknown/foreign command,
  ``409`` for a non-``delivered`` row (duplicate report / never claimed).

There is **no** HTTP enqueue endpoint: an enqueue surface not fronted by
the policy gate would bypass the initiative's "all authorization stays
central" principle. Central code enqueues via
:func:`meho_backplane.gateway.queue.enqueue_command`; #2500's minting path
(post-policy-gate) is the production caller.

Auth (binding decision #2498-5)
-------------------------------

Both routes gate on the runner principal shipped by #2502 — **not** an
operator JWT. :func:`~meho_backplane.auth.runner_guard.require_runner`
admits only ``principal_kind=runner`` tokens; then
:func:`~meho_backplane.auth.runner_guard.assert_runner_scope` binds the
token's ``runner_id`` claim to the ``runner_principal`` row named by the
``{runner}`` path segment, so a runner may only claim / report its own
queue. Every query additionally filters ``tenant_id == operator.tenant_id``.
A bare ``read_only`` role gate would let any human read_only principal
destructively claim commands, and an operator-tier gate would 403 every
runner token (runner tokens carry ``tenant_role=read_only``, rank 0) — so
the runner-scope guard is the only correct gate here.

Hold discipline (binding decision #2498-2)
------------------------------------------

The hold is a bounded claim-poll loop over Postgres, **not** an in-process
``asyncio.Event`` (wrong under >1 replica: an enqueue on replica A cannot
wake a hold on replica B) and **not** Valkey ``XREAD BLOCK`` (the deferred
stream promotion). Each attempt opens a short-lived session, tries one
:func:`~meho_backplane.gateway.queue.claim_next_command`, commits a win,
and — on an empty queue — sleeps briefly before re-attempting. No DB
transaction or pooled connection is held across the sleep, so the handler
imports ``get_sessionmaker`` directly rather than taking a request-scoped
session dependency (which would keep one transaction open for the whole
hold).
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Annotated, Any, Final, Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi import status as http_status
from pydantic import BaseModel, ConfigDict, Field

from meho_backplane.auth.operator import Operator
from meho_backplane.auth.runner_guard import assert_runner_scope, require_runner
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import GatewayCommand, GatewayCommandStatus
from meho_backplane.gateway.deadman import clear_runner_stale
from meho_backplane.gateway.queue import (
    GATEWAY_LONGPOLL_DEFAULT_WAIT_SECONDS,
    GATEWAY_LONGPOLL_MAX_WAIT_SECONDS,
    GatewayCommandNotDeliveredError,
    GatewayCommandNotFoundError,
    claim_next_command,
    clamp_longpoll_wait,
    record_result,
)

__all__ = ["router"]

_log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/gateway", tags=["gateway"])

#: Module-level ``Depends`` closure — resolves the runner dependency once
#: (FastAPI-idiomatic singleton) and avoids ruff B008. Same pattern as
#: :mod:`meho_backplane.api.v1.approvals`'s ``_require_operator``.
_require_runner = Depends(require_runner())

#: Seconds between claim attempts while a long-poll holds an empty queue.
#: Sized so an idle hold costs ~1 claim query/second; the runner re-polls
#: immediately on a ``204``, so end-to-end delivery latency is bounded by
#: this interval, not by the runner's tick cadence. Tests monkeypatch it
#: down for a fast, deterministic hold assertion.
_CLAIM_POLL_INTERVAL_SECONDS: Final[float] = 1.0


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class GatewayCommandEnvelope(BaseModel):
    """The claimed-command envelope returned by ``GET .../next`` (200)."""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    op_id: str
    params: dict[str, Any]
    # ``None`` for a targetless synthetic op (net.*); a resolved descriptor
    # the runner's handler duck-reads otherwise.
    target_descriptor: dict[str, Any] | None


class GatewayResultBody(BaseModel):
    """POST body for ``.../result`` — the runner's outcome report."""

    model_config = ConfigDict(extra="forbid")

    command_id: uuid.UUID = Field(description="The delivered command's id.")
    outcome: Literal["succeeded", "failed"] = Field(
        description="Terminal outcome to record for the command."
    )
    result: dict[str, Any] | None = Field(
        default=None,
        description="Structured success payload (for a 'succeeded' outcome).",
    )
    error: str | None = Field(
        default=None,
        description="Failure summary (for a 'failed' outcome).",
    )


class GatewayResultAck(BaseModel):
    """Response for a successful ``.../result`` report (200)."""

    model_config = ConfigDict(frozen=True)

    command_id: uuid.UUID
    status: str  # "succeeded" | "failed"
    completed_at: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _envelope(command: GatewayCommand) -> GatewayCommandEnvelope:
    """Project a claimed :class:`GatewayCommand` row onto its wire envelope."""
    return GatewayCommandEnvelope(
        id=command.id,
        op_id=command.op_id,
        params=command.params,
        target_descriptor=command.target_descriptor,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get(
    "/{runner}/next",
    response_model=GatewayCommandEnvelope,
    responses={
        200: {"description": "A command was claimed; the envelope is returned."},
        204: {"description": "No command became claimable before the wait deadline."},
    },
)
async def poll_next_command(
    runner: str,
    wait: Annotated[
        int,
        Query(
            ge=0,
            description=(
                "Long-poll hold in seconds. 0 = a single immediate claim attempt "
                "(no hold). Values above the ceiling "
                f"({GATEWAY_LONGPOLL_MAX_WAIT_SECONDS}s) are clamped down, not "
                "rejected — the runner asking for a longer hold gets a bounded one."
            ),
        ),
    ] = GATEWAY_LONGPOLL_DEFAULT_WAIT_SECONDS,
    operator: Operator = _require_runner,
) -> GatewayCommandEnvelope | Response:
    """Long-poll for the next command queued for ``{runner}``.

    Authenticated as the runner principal for ``{runner}`` (#2502's guard):
    a runner may only poll its own queue. Holds up to ``wait`` seconds
    (clamped to the exported ceiling) claiming the oldest ``pending``
    command; returns ``200`` with the envelope on a claim or ``204`` on
    timeout. The claim flips the row ``pending -> delivered`` durably.

    The hold opens a fresh short-lived session per attempt and never holds
    a DB transaction across the inter-attempt sleep (binding decision
    #2498-2). A client disconnect cancels the handler; the
    ``CancelledError`` is logged and re-raised per the asyncio contract.
    """
    sessionmaker = get_sessionmaker()

    # Scope gate first: bind the token's runner_id to the named row (#2502).
    async with sessionmaker() as session:
        await assert_runner_scope(operator, runner_name=runner, session=session)

    effective_wait = clamp_longpoll_wait(wait)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + effective_wait

    try:
        while True:
            async with sessionmaker() as session:
                command = await claim_next_command(
                    session, tenant_id=operator.tenant_id, runner_id=runner
                )
                if command is not None:
                    envelope = _envelope(command)
                    await session.commit()
                    return envelope
            # Empty queue: hold until the deadline, sleeping in bounded steps
            # with no session/transaction open across the sleep.
            remaining = deadline - loop.time()
            if remaining <= 0:
                return Response(status_code=http_status.HTTP_204_NO_CONTENT)
            await asyncio.sleep(min(_CLAIM_POLL_INTERVAL_SECONDS, remaining))
    except asyncio.CancelledError:
        # Client disconnect — log + re-raise per the asyncio cancellation
        # contract (Sonar S7497); never swallow the cancellation.
        _log.info(
            "gateway_poll_disconnected",
            runner=runner,
            operator_sub=operator.sub,
        )
        raise


@router.post("/{runner}/result", response_model=GatewayResultAck)
async def report_command_result(
    runner: str,
    body: GatewayResultBody,
    operator: Operator = _require_runner,
) -> GatewayResultAck:
    """Report the outcome of a delivered command for ``{runner}``.

    Authenticated as the runner principal for ``{runner}`` (#2502's guard).
    Flips the command ``delivered -> succeeded|failed``, stamping the
    result/error and ``completed_at``. Returns ``200`` with an ack; ``404``
    when the command id is unknown or was enqueued for another runner /
    tenant (no existence oracle); ``409`` when the command is not
    ``delivered`` (a duplicate report or a never-claimed row).
    """
    sessionmaker = get_sessionmaker()
    try:
        async with sessionmaker() as session:
            # Scope gate first (#2502), then record on the same session.
            await assert_runner_scope(operator, runner_name=runner, session=session)
            command = await record_result(
                session,
                tenant_id=operator.tenant_id,
                runner_id=runner,
                command_id=body.command_id,
                outcome=GatewayCommandStatus(body.outcome),
                result=body.result,
                error=body.error,
            )
            # Recovery clear seam (#2501): an accepted command result proves
            # the runner is alive and reporting, so reset any dead-man flip
            # marker the central sweeper set (sweeper only flips; this clears).
            await clear_runner_stale(session, tenant_id=operator.tenant_id, runner_name=runner)
            ack = GatewayResultAck(
                command_id=command.id,
                status=command.status,
                completed_at=command.completed_at.isoformat()
                if command.completed_at is not None
                else "",
            )
            await session.commit()
    except GatewayCommandNotFoundError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="gateway_command_not_found",
        ) from exc
    except GatewayCommandNotDeliveredError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=f"gateway_command_not_delivered: {exc.status}",
        ) from exc
    return ack
