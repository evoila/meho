# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
REST API endpoints for scheduled task CRUD, toggle, run-now, run history,
NL-to-cron conversion, cron validation, and timezone listing.

All endpoints require JWT authentication via ``get_current_user``. Tenant
isolation is enforced by extracting ``tenant_id`` from the user context.

NL-to-cron uses PydanticAI with Sonnet 4.6 (same lightweight LLM pattern
as ``event_executor.generate_session_title``).
"""

from __future__ import annotations

import asyncio
import functools
from datetime import datetime
from typing import Annotated, Any
from zoneinfo import available_timezones

from croniter import croniter
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from meho_app.api.dependencies import CurrentUser
from meho_app.core.otel import get_logger
from meho_app.database import get_db_session
from meho_app.modules.scheduled_tasks.models import (
    ScheduledTaskModel,
    ScheduledTaskRunModel,
)
from meho_app.modules.scheduled_tasks.service import ScheduledTaskService

logger = get_logger(__name__)

MSG_SCHEDULED_TASK_NOT_FOUND = "Scheduled task not found"

router = APIRouter(prefix="/scheduled-tasks", tags=["scheduled-tasks"])


def _require_tenant_id(user: CurrentUser) -> str:
    """Extract tenant_id with runtime validation, narrowing str | None to str."""
    if user.tenant_id is None:
        raise HTTPException(status_code=403, detail="Tenant context required")
    return user.tenant_id


# ============================================================================
# Request/Response Schemas
# ============================================================================


class CreateScheduledTaskRequest(BaseModel):
    """Request body for creating a scheduled task."""

    name: str = Field(..., max_length=255)
    description: str | None = None
    cron_expression: str
    timezone: str = "UTC"
    prompt: str
    allowed_connector_ids: list[str] | None = Field(
        default=None,
        description="Connector IDs this task can access. None = all tenant connectors.",
    )
    delegate_credentials: bool = Field(
        default=False,
        description="Allow credential delegation from creating user",
    )
    notification_targets: list[dict[str, str]] | None = Field(
        default=None,
        description='Notification targets for approval alerts. Each item: {"connector_id": "uuid", "contact": "email"}.',
    )


class UpdateScheduledTaskRequest(BaseModel):
    """Request body for updating a scheduled task (partial update)."""

    name: str | None = Field(default=None, max_length=255)
    description: str | None = None
    cron_expression: str | None = None
    timezone: str | None = None
    prompt: str | None = None
    is_enabled: bool | None = None
    notification_targets: list[dict[str, str]] | None = None


class ParseScheduleRequest(BaseModel):
    """Request body for NL-to-cron conversion."""

    text: str
    timezone: str = "UTC"


class ValidateCronRequest(BaseModel):
    """Request body for cron expression validation."""

    cron_expression: str
    timezone: str = "UTC"


class ScheduledTaskResponse(BaseModel):
    """Response schema for a scheduled task."""

    id: str
    tenant_id: str
    name: str
    description: str | None = None
    cron_expression: str
    timezone: str
    prompt: str
    is_enabled: bool
    next_run_at: str | None = None
    total_runs: int
    last_run_at: str | None = None
    last_run_status: str | None = None
    created_by_user_id: str | None = None
    created_at: str
    updated_at: str
    # Identity model (Phase 74)
    allowed_connector_ids: list[str] | None = None
    delegate_credentials: bool = False
    delegation_active: bool = True
    # Phase 75: notification targets
    notification_targets: list[dict[str, str]] | None = None


class ScheduledTaskRunResponse(BaseModel):
    """Response schema for a scheduled task run."""

    id: str
    task_id: str
    status: str
    session_id: str | None = None
    error_message: str | None = None
    prompt_snapshot: str
    started_at: str
    completed_at: str | None = None
    duration_seconds: int | None = None


class ParseScheduleResponse(BaseModel):
    """Response schema for NL-to-cron conversion."""

    cron_expression: str
    next_runs: list[str]
    human_readable: str | None = None


class ValidateCronResponse(BaseModel):
    """Response schema for cron validation."""

    is_valid: bool
    cron_expression: str | None = None
    next_runs: list[str] = []
    error: str | None = None


class GenerateTaskPromptResponse(BaseModel):
    """Response for LLM-generated scheduled task prompt."""

    prompt: str


# ============================================================================
# Helpers
# ============================================================================


def _dt_to_iso(dt: datetime | None) -> str | None:
    """Convert a datetime to ISO 8601 string, or None."""
    if dt is None:
        return None
    return dt.isoformat()


def _task_to_response(task: ScheduledTaskModel) -> ScheduledTaskResponse:
    """Convert a ScheduledTaskModel to its API response."""
    return ScheduledTaskResponse(
        id=str(task.id),
        tenant_id=task.tenant_id,  # type: ignore[arg-type]  # SQLAlchemy ORM attribute
        name=task.name,  # type: ignore[arg-type]  # SQLAlchemy ORM attribute
        description=task.description,  # type: ignore[arg-type]  # SQLAlchemy ORM attribute
        cron_expression=task.cron_expression,  # type: ignore[arg-type]  # SQLAlchemy ORM attribute
        timezone=task.timezone,  # type: ignore[arg-type]  # SQLAlchemy ORM attribute
        prompt=task.prompt,  # type: ignore[arg-type]  # SQLAlchemy ORM attribute
        is_enabled=task.is_enabled,  # type: ignore[arg-type]  # SQLAlchemy ORM attribute
        next_run_at=_dt_to_iso(task.next_run_at),  # type: ignore[arg-type]  # SQLAlchemy ORM attribute
        total_runs=task.total_runs,  # type: ignore[arg-type]  # SQLAlchemy ORM attribute
        last_run_at=_dt_to_iso(task.last_run_at),  # type: ignore[arg-type]  # SQLAlchemy ORM attribute
        last_run_status=task.last_run_status,  # type: ignore[arg-type]  # SQLAlchemy ORM attribute
        created_by_user_id=task.created_by_user_id,  # type: ignore[arg-type]  # SQLAlchemy ORM attribute
        created_at=task.created_at.isoformat(),
        updated_at=task.updated_at.isoformat(),
        allowed_connector_ids=task.allowed_connector_ids,  # type: ignore[arg-type]  # SQLAlchemy ORM attribute
        delegate_credentials=task.delegate_credentials,  # type: ignore[arg-type]  # SQLAlchemy ORM attribute
        delegation_active=task.delegation_active,  # type: ignore[arg-type]  # SQLAlchemy ORM attribute
        notification_targets=task.notification_targets,  # type: ignore[arg-type]  # SQLAlchemy ORM attribute
    )


def _run_to_response(run: ScheduledTaskRunModel) -> ScheduledTaskRunResponse:
    """Convert a ScheduledTaskRunModel to its API response."""
    return ScheduledTaskRunResponse(
        id=str(run.id),
        task_id=str(run.task_id),
        status=run.status,  # type: ignore[arg-type]  # SQLAlchemy ORM attribute
        session_id=str(run.session_id) if run.session_id else None,
        error_message=run.error_message,  # type: ignore[arg-type]  # SQLAlchemy ORM attribute
        prompt_snapshot=run.prompt_snapshot,  # type: ignore[arg-type]  # SQLAlchemy ORM attribute
        started_at=run.started_at.isoformat(),
        completed_at=_dt_to_iso(run.completed_at),  # type: ignore[arg-type]  # SQLAlchemy ORM attribute
        duration_seconds=run.duration_seconds,  # type: ignore[arg-type]  # SQLAlchemy ORM attribute
    )


def _compute_next_runs(cron_expression: str, timezone: str, count: int = 5) -> list[str]:
    """Compute the next N run times for a cron expression."""
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(timezone)
    now = datetime.now(tz)
    cron = croniter(cron_expression, now)
    runs = []
    for _ in range(count):
        next_run = cron.get_next(datetime)
        runs.append(next_run.isoformat())
    return runs


# Cached sorted timezone list (does not change at runtime)
@functools.lru_cache(maxsize=1)
def _get_sorted_timezones() -> list[str]:
    """Return sorted list of available IANA timezone names."""
    return sorted(available_timezones())


# ============================================================================
# Endpoints
# ============================================================================


@router.get("/timezones", response_model=list[str])
async def list_timezones(user: CurrentUser) -> Any:
    """Return sorted list of available IANA timezone names."""
    return _get_sorted_timezones()


@router.post(
    "/parse-schedule",
    response_model=ParseScheduleResponse,
    responses={422: {"description": "Failed to parse schedule: ..."}},
)
async def parse_schedule(body: ParseScheduleRequest, user: CurrentUser) -> Any:
    """Convert natural language schedule description to cron expression.

    Uses PydanticAI with Sonnet 4.6 for the NL-to-cron conversion. The LLM
    output is validated with croniter before being returned. Times out after
    10 seconds.
    """
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    # Validate timezone
    try:
        ZoneInfo(body.timezone)
    except (ZoneInfoNotFoundError, KeyError):
        raise HTTPException(status_code=422, detail=f"Invalid timezone: {body.timezone}") from None

    try:
        from pydantic_ai import Agent

        agent = Agent(
            "anthropic:claude-sonnet-4-6",
            system_prompt=(
                "Convert the following natural language schedule description to a "
                "standard 5-field cron expression (minute hour day-of-month month day-of-week). "
                "Return ONLY the cron expression, nothing else. No quotes, no explanation. "
                "Examples:\n"
                "- 'every day at 9am' -> '0 9 * * *'\n"
                "- 'every Monday at 3:30pm' -> '30 15 * * 1'\n"
                "- 'every 5 minutes' -> '*/5 * * * *'\n"
                "- 'first day of every month at midnight' -> '0 0 1 * *'\n"
                "- 'weekdays at 8am' -> '0 8 * * 1-5'\n"
            ),
        )

        result = await asyncio.wait_for(
            agent.run(body.text),
            timeout=10.0,
        )
        cron_expr = str(result.output).strip().strip("\"'`")

        # Validate LLM output
        if not croniter.is_valid(cron_expr):
            raise HTTPException(
                status_code=422,
                detail=f"LLM returned invalid cron expression: {cron_expr}",
            )

        next_runs = _compute_next_runs(cron_expr, body.timezone)
        return ParseScheduleResponse(
            cron_expression=cron_expr,
            next_runs=next_runs,
            human_readable=None,
        )

    except TimeoutError:
        raise HTTPException(
            status_code=422,
            detail="NL-to-cron conversion timed out (10s). Please try again.",
        ) from None
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"NL-to-cron conversion failed: {e}", exc_info=True)
        raise HTTPException(
            status_code=422,
            detail=f"Failed to parse schedule: {e}",
        ) from e


@router.post("/validate-cron", response_model=ValidateCronResponse)
async def validate_cron(body: ValidateCronRequest, user: CurrentUser) -> Any:
    """Validate a cron expression and return next 5 scheduled runs.

    Always returns 200 -- ``is_valid=false`` for bad expressions (not 422).
    """
    if not croniter.is_valid(body.cron_expression):
        return ValidateCronResponse(
            is_valid=False,
            cron_expression=body.cron_expression,
            next_runs=[],
            error=f"Invalid cron expression: {body.cron_expression}",
        )

    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        ZoneInfo(body.timezone)
    except (ZoneInfoNotFoundError, KeyError):
        return ValidateCronResponse(
            is_valid=False,
            cron_expression=body.cron_expression,
            next_runs=[],
            error=f"Invalid timezone: {body.timezone}",
        )

    try:
        next_runs = _compute_next_runs(body.cron_expression, body.timezone)
        return ValidateCronResponse(
            is_valid=True,
            cron_expression=body.cron_expression,
            next_runs=next_runs,
        )
    except Exception as e:
        return ValidateCronResponse(
            is_valid=False,
            cron_expression=body.cron_expression,
            next_runs=[],
            error=str(e),
        )


@router.post(
    "/generate-prompt",
    response_model=GenerateTaskPromptResponse,
    responses={422: {"description": "Failed to generate prompt: ..."}},
)
async def generate_task_prompt(user: CurrentUser) -> Any:
    """Generate a generic investigation prompt for a scheduled task.

    Uses PydanticAI with Sonnet 4.6 to generate a contextually relevant
    scheduled investigation prompt. Each call produces a different variation
    focusing on health checks, capacity planning, incident review, etc.
    """
    try:
        from pydantic_ai import Agent

        agent = Agent(
            "anthropic:claude-sonnet-4-6",
            system_prompt=(
                "Generate a concise scheduled task investigation prompt for MEHO, "
                "an AI operations assistant that connects to infrastructure systems "
                "(Kubernetes, VMware, Prometheus, Jira, Confluence, etc.). "
                "The prompt should instruct MEHO to perform a routine check or investigation. "
                "No template variables needed (scheduled tasks have no payload). "
                "The prompt should be 2-4 sentences, actionable, and focused on one area. "
                "Generate a DIFFERENT prompt each time -- vary the focus: health checks, "
                "capacity planning, recent incidents, pending alerts, resource utilization, "
                "anomaly detection, certificate expiry, backup verification, SLA compliance, "
                "or security posture review. "
                "Return ONLY the prompt text. No markdown, no code fences, no explanation."
            ),
        )

        result = await asyncio.wait_for(
            agent.run("Generate a scheduled task investigation prompt"),
            timeout=10.0,
        )
        generated = str(result.output).strip().strip("`\"'")
        return GenerateTaskPromptResponse(prompt=generated)

    except TimeoutError:
        raise HTTPException(
            status_code=422,
            detail="Prompt generation timed out (10s). Please try again.",
        ) from None
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Scheduled task prompt generation failed: {e}", exc_info=True)
        raise HTTPException(
            status_code=422,
            detail=f"Failed to generate prompt: {e}",
        ) from e


@router.get("/", response_model=list[ScheduledTaskResponse])
async def list_tasks(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> Any:
    """List all scheduled tasks for the current tenant."""
    service = ScheduledTaskService(db)
    tenant_id = _require_tenant_id(user)
    tasks = await service.list_tasks(tenant_id)
    return [_task_to_response(t) for t in tasks]


@router.post(
    "/",
    response_model=ScheduledTaskResponse,
    status_code=201,
    responses={400: {"description": "Bad request"}, 422: {"description": "Validation error"}},
)
async def create_task(
    body: CreateScheduledTaskRequest,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> Any:
    """Create a new scheduled task."""
    # Phase 74: Creation-time credential pre-validation
    if body.delegate_credentials:
        from meho_app.modules.connectors.repositories.credential_repository import (
            CredentialRepository,
        )

        cred_repo = CredentialRepository(db)
        connector_ids_to_check = body.allowed_connector_ids or []
        missing_creds = []
        for cid in connector_ids_to_check:
            creds = await cred_repo.get_credentials(user.user_id, cid)
            if not creds:
                missing_creds.append(cid)
        if missing_creds:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Cannot enable credential delegation: you have no stored "
                    f"credentials for connector(s): {', '.join(missing_creds)}. "
                    f"Store your credentials first."
                ),
            )

    try:
        task = await service_create(db, user, body)
        await db.commit()
        return _task_to_response(task)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


async def service_create(
    db: AsyncSession, user: Any, body: CreateScheduledTaskRequest
) -> ScheduledTaskModel:
    """Helper to create task via service (extracted for testability)."""
    service = ScheduledTaskService(db)
    return await service.create_task(
        tenant_id=user.tenant_id,
        user_id=user.user_id,
        name=body.name,
        description=body.description,
        cron_expression=body.cron_expression,
        timezone_name=body.timezone,
        prompt=body.prompt,
        allowed_connector_ids=body.allowed_connector_ids,
        delegate_credentials=body.delegate_credentials,
        notification_targets=body.notification_targets,
    )


@router.get(
    "/{task_id}",
    response_model=ScheduledTaskResponse,
    responses={404: {"description": "Scheduled task not found"}},
)
async def get_task(
    task_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> Any:
    """Get a scheduled task by ID."""
    service = ScheduledTaskService(db)
    tenant_id = _require_tenant_id(user)
    task = await service.get_task(tenant_id, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=MSG_SCHEDULED_TASK_NOT_FOUND)
    return _task_to_response(task)


@router.put(
    "/{task_id}",
    response_model=ScheduledTaskResponse,
    responses={
        404: {"description": "Scheduled task not found"},
        422: {"description": "Validation error"},
    },
)
async def update_task(
    task_id: str,
    body: UpdateScheduledTaskRequest,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> Any:
    """Update a scheduled task."""
    tenant_id = _require_tenant_id(user)
    service = ScheduledTaskService(db)
    update_fields = body.model_dump(exclude_unset=True)
    if "timezone" in update_fields:
        update_fields["timezone"] = update_fields.pop("timezone")
    try:
        task = await service.update_task(tenant_id, task_id, **update_fields)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    if task is None:
        raise HTTPException(status_code=404, detail=MSG_SCHEDULED_TASK_NOT_FOUND)
    await db.commit()
    return _task_to_response(task)


@router.delete(
    "/{task_id}",
    status_code=204,
    response_model=None,
    responses={404: {"description": "Scheduled task not found"}},
)
async def delete_task(
    task_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> None:
    """Delete a scheduled task."""
    tenant_id = _require_tenant_id(user)
    service = ScheduledTaskService(db)
    deleted = await service.delete_task(tenant_id, task_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=MSG_SCHEDULED_TASK_NOT_FOUND)
    await db.commit()
    return None


@router.patch(
    "/{task_id}/toggle",
    response_model=ScheduledTaskResponse,
    responses={404: {"description": "Scheduled task not found"}},
)
async def toggle_task(
    task_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> Any:
    """Toggle a scheduled task's enabled/disabled state."""
    tenant_id = _require_tenant_id(user)
    service = ScheduledTaskService(db)
    task = await service.toggle_task(tenant_id, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=MSG_SCHEDULED_TASK_NOT_FOUND)
    await db.commit()
    return _task_to_response(task)


@router.post(
    "/{task_id}/run",
    response_model=ScheduledTaskRunResponse,
    status_code=202,
)
async def run_task_now(
    task_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> Any:
    """Trigger immediate execution of a scheduled task (fire-and-forget).

    Returns 202 Accepted with the run record immediately. The actual
    execution happens asynchronously via ``asyncio.create_task``.
    """
    tenant_id = _require_tenant_id(user)
    service = ScheduledTaskService(db)
    run = await service.run_now(tenant_id, task_id)
    if run is None:
        raise HTTPException(status_code=404, detail=MSG_SCHEDULED_TASK_NOT_FOUND)
    await db.commit()
    return _run_to_response(run)


@router.get(
    "/{task_id}/runs",
    response_model=list[ScheduledTaskRunResponse],
    responses={404: {"description": "Scheduled task not found"}},
)
async def list_task_runs(
    task_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Any:
    """Get run history for a scheduled task."""
    tenant_id = _require_tenant_id(user)
    service = ScheduledTaskService(db)
    task = await service.get_task(tenant_id, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=MSG_SCHEDULED_TASK_NOT_FOUND)
    runs = await service.get_runs(tenant_id, task_id, limit=limit, offset=offset)
    return [_run_to_response(r) for r in runs]
