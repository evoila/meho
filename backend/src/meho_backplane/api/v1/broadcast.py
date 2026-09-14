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
from typing import Annotated, Any, Literal
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import require_role
from meho_backplane.broadcast import (
    ACTIVITY_MAX_CHARS,
    MAX_TARGETS,
    OP_CLASS_ENUM,
    PLANNED_OP_CLASS_VALUES,
    TARGET_MAX_CHARS,
    TTL_MAX_MINUTES,
    TTL_MIN_MINUTES,
    WORK_REF_MAX_CHARS,
    AgentAnnouncementEvent,
    AnnounceRateLimitError,
    InvalidSinceError,
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
    phase: Literal["start", "update", "completion"] = "update"
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
    run_id: UUID | None = None


class BroadcastAnnounceResponse(BaseModel):
    """Acknowledgement with distinct durable event id and stream cursor."""

    event_id: UUID
    cursor: str
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

    event = AgentAnnouncementEvent(
        tenant_id=operator.tenant_id,
        principal_sub=operator.sub,
        activity=payload.activity,
        target=payload.target,
        targets=payload.targets,
        scope=payload.scope,
        planned_op_class=payload.planned_op_class,
        ttl_minutes=payload.ttl_minutes,
        work_ref=payload.work_ref,
        run_id=payload.run_id,
        phase=payload.phase,
        ts=datetime.now(UTC),
    )
    cursor = await publish_agent_announcement(event)
    return BroadcastAnnounceResponse(
        event_id=event.event_id,
        cursor=cursor,
        targets=payload.targets or None,
        planned_op_class=payload.planned_op_class,
        ttl_minutes=payload.ttl_minutes,
        work_ref=payload.work_ref,
        run_id=payload.run_id,
    )
