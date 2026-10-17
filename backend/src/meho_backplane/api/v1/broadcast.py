# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Operator REST parity for the broadcast working-surface tools (#3470).

The routes are deliberately thin adapters.  They use the same strict history
reader and announcement publisher as the MCP tools, so REST and CLI callers
keep the broadcast subsystem's tenant isolation, durable-before-stream
announcement order, rate limit, and fail-loud semantics.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal, cast
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from meho_backplane.auth.agent_admission import (
    AgentIdentityRequiredError,
    resolve_agent_admission,
)
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import require_role
from meho_backplane.broadcast import (
    ACTIVITY_MAX_CHARS,
    IDEMPOTENCY_KEY_MAX_CHARS,
    MAX_TARGETS,
    OP_CLASS_ENUM,
    PLANNED_OP_CLASS_VALUES,
    TARGET_MAX_CHARS,
    TTL_MAX_MINUTES,
    TTL_MIN_MINUTES,
    WORK_REF_MAX_CHARS,
    AnnouncementClaimError,
    AnnounceRateLimitError,
    InvalidSinceError,
    build_trusted_announcement,
    enforce_announce_rate_limit,
    list_recent_events_strict,
    publish_agent_announcement,
)

__all__ = ["router"]

router = APIRouter(prefix="/api/v1/broadcast", tags=["broadcast"])

_REQUIRE_OPERATOR = Depends(require_role(TenantRole.OPERATOR))


class BroadcastAnnounceRequest(BaseModel):
    """The REST representation of a governed agent announcement."""

    model_config = ConfigDict(extra="forbid")

    activity: str = Field(min_length=1, max_length=ACTIVITY_MAX_CHARS)
    target: str | None = Field(default=None, max_length=TARGET_MAX_CHARS)
    scope: str | None = Field(default=None, max_length=TARGET_MAX_CHARS)
    phase: Literal["start", "update", "completion"] = "start"
    targets: list[str] = Field(default_factory=list, max_length=MAX_TARGETS)
    planned_op_class: (
        Literal[
            "read",
            "write",
            "credential_read",
            "credential_write",
            "credential_mint",
            "audit_query",
            "approval",
            "other",
        ]
        | None
    ) = None
    ttl_minutes: int | None = Field(default=None, ge=TTL_MIN_MINUTES, le=TTL_MAX_MINUTES)
    work_ref: str | None = Field(default=None, min_length=1, max_length=WORK_REF_MAX_CHARS)
    #: Deprecated: the server sets the run id. A value that is not the
    #: checked run is refused.
    run_id: UUID | None = None
    claim_id: UUID | None = None
    idempotency_key: str | None = Field(
        default=None, min_length=1, max_length=IDEMPOTENCY_KEY_MAX_CHARS
    )


class BroadcastAnnounceResponse(BaseModel):
    """Acknowledgement with distinct durable event id and stream cursor.

    ``replayed=true`` means the step was already stored and no new row was
    written. ``cursor`` is ``None`` when the stream entry already existed;
    it is set when this call wrote the stream entry, including a retry
    that finished a delivery whose first stream write failed.
    """

    event_id: UUID
    cursor: str | None
    claim_id: UUID
    phase: str
    owner_sub: str
    admission: str
    replayed: bool
    actor_sub: str | None = None
    targets: list[str] | None = None
    planned_op_class: str | None = None
    ttl_minutes: int | None = None
    work_ref: str | None = None
    run_id: UUID | None = None


def _bind_audit(*, op_id: str, op_class: str) -> None:
    """Declare the REST action for the chassis audit middleware."""
    structlog.contextvars.bind_contextvars(audit_op_id=op_id, audit_op_class=op_class)


@router.get("/recent")
async def recent_broadcast_events(
    operator: Operator = _REQUIRE_OPERATOR,
    cursor: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    op_class: Annotated[str | None, Query()] = None,
    principal: Annotated[str | None, Query(min_length=1, max_length=256)] = None,
    target: Annotated[str | None, Query(min_length=1, max_length=256)] = None,
    actor_sub: Annotated[str | None, Query(min_length=1, max_length=256)] = None,
    work_ref: Annotated[str | None, Query(min_length=1, max_length=WORK_REF_MAX_CHARS)] = None,
    active_only: bool = False,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> dict[str, Any]:
    """Read one tenant-scoped, LLM-safe broadcast history page."""
    if op_class is not None and op_class not in OP_CLASS_ENUM:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="invalid_op_class",
        )
    _bind_audit(op_id="meho_broadcast_recent", op_class="read")
    try:
        return await list_recent_events_strict(
            operator,
            since=cursor,
            op_class=op_class,
            principal=principal,
            target=target,
            actor_sub=actor_sub,
            work_ref=work_ref,
            active_only=active_only,
            limit=limit,
        )
    except InvalidSinceError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc


@router.post(
    "/announce",
    response_model=BroadcastAnnounceResponse,
    status_code=status.HTTP_201_CREATED,
)
async def announce_broadcast(
    payload: BroadcastAnnounceRequest,
    operator: Operator = _REQUIRE_OPERATOR,
) -> BroadcastAnnounceResponse:
    """Durably publish a tenant-scoped agent announcement through the shared seam."""
    if len(payload.targets) > MAX_TARGETS or any(
        not 1 <= len(target) <= TARGET_MAX_CHARS for target in payload.targets
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="invalid_targets",
        )
    if (
        payload.planned_op_class is not None
        and payload.planned_op_class not in PLANNED_OP_CLASS_VALUES
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="invalid_planned_op_class",
        )
    _bind_audit(op_id="meho_broadcast_announce", op_class="write")
    # Trusted admission (#3736): only a checked agent identity publishes.
    try:
        admission = await resolve_agent_admission(operator)
    except AgentIdentityRequiredError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": exc.code, "reason": exc.reason, "remediation": exc.remediation},
        ) from exc
    try:
        await enforce_announce_rate_limit(operator.tenant_id, operator.sub)
    except AnnounceRateLimitError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "broadcast_announce_rate_limited",
                "limit": exc.limit,
                "window_seconds": exc.window_seconds,
                "retry_after_seconds": exc.retry_after_seconds,
            },
            headers={"Retry-After": str(exc.retry_after_seconds)},
        ) from exc

    try:
        event = build_trusted_announcement(
            admission,
            tenant_id=operator.tenant_id,
            activity=payload.activity,
            target=payload.target,
            targets=payload.targets,
            scope=payload.scope,
            planned_op_class=payload.planned_op_class,
            ttl_minutes=payload.ttl_minutes,
            work_ref=payload.work_ref,
            run_id=payload.run_id,
            phase=payload.phase,
            claim_id=payload.claim_id,
            idempotency_key=payload.idempotency_key,
            ts=datetime.now(UTC),
        )
        result = await publish_agent_announcement(event, idempotency_key=payload.idempotency_key)
    except AnnouncementClaimError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    stored = result.event
    return BroadcastAnnounceResponse(
        event_id=stored.event_id,
        cursor=result.cursor,
        claim_id=cast("UUID", stored.claim_id),
        phase=stored.phase,
        owner_sub=cast("str", stored.owner_sub),
        admission=cast("str", stored.admission),
        replayed=result.replayed,
        actor_sub=stored.actor_sub,
        targets=list(stored.targets) or None,
        planned_op_class=stored.planned_op_class,
        ttl_minutes=stored.ttl_minutes,
        work_ref=stored.work_ref,
        run_id=stored.run_id,
    )
