# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Event management BFF endpoints.

CRUD, event history, and test pipeline for event registrations.
Follows the exact pattern from memories.py: verify connector ownership,
create session via create_openapi_session_maker(), delegate to EventService.

Security:
- All endpoints require JWT authentication via RequirePermission
- Connector ownership verified per tenant (tenant isolation)
- HMAC secret returned ONLY at creation time (display-once pattern)
- Test endpoint bypasses HMAC (operator is already JWT-authenticated)
"""

# mypy: disable-error-code="no-untyped-def,arg-type,attr-defined"
import asyncio
import hashlib
import json
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from meho_app.core.auth_context import UserContext
from meho_app.core.otel import get_logger
from meho_app.core.permissions import Permission, RequirePermission

logger = get_logger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Pydantic schemas (inline, same pattern as memories.py)
# ---------------------------------------------------------------------------


class EventCreateRequest(BaseModel):
    """Request to create an event registration."""

    name: str = Field(..., max_length=255)
    prompt_template: str = Field(default="Investigate: {{payload}}")
    rate_limit_per_hour: int = Field(default=10, ge=1, le=100)
    require_signature: bool = Field(default=True)
    allowed_connector_ids: list[str] | None = Field(
        default=None,
        description="Connector IDs this event registration can access. None = all tenant connectors.",
    )
    notification_targets: list[dict[str, str]] | None = Field(
        default=None,
        description='Notification targets for approval alerts. Each item: {"connector_id": "uuid", "contact": "email"}.',
    )
    response_config: dict | None = Field(
        default=None,
        description='Response channel config: {"connector_id": "uuid", "operation_id": "op_name", "parameter_mapping": {"key": "{{payload.field}}"}}',
    )


class EventUpdateRequest(BaseModel):
    """Request to update an event registration (PATCH semantics).

    Note: created_by_user_id is NOT updatable -- it persists across edits.
    """

    name: str | None = Field(None, max_length=255)
    prompt_template: str | None = None
    rate_limit_per_hour: int | None = Field(None, ge=1, le=100)
    is_active: bool | None = None
    require_signature: bool | None = None
    allowed_connector_ids: list[str] | None = None
    notification_targets: list[dict[str, str]] | None = None
    response_config: dict | None = Field(
        default=None,
        description='Response channel config: {"connector_id": "uuid", "operation_id": "op_name", "parameter_mapping": {"key": "{{payload.field}}"}}',
    )


class EventResponse(BaseModel):
    """Event registration response (NO secret)."""

    id: str
    name: str
    event_url: str
    prompt_template: str
    rate_limit_per_hour: int
    is_active: bool
    require_signature: bool
    total_events_received: int
    total_events_processed: int
    total_events_deduplicated: int
    last_event_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    events_today: int = 0
    # Identity model (Phase 74)
    created_by_user_id: str | None = None
    allowed_connector_ids: list[str] | None = None
    delegation_active: bool = True
    # Phase 75: notification targets
    notification_targets: list[dict[str, str]] | None = None
    # Phase 94: response channel config
    response_config: dict | None = None


class EventCreateResponse(EventResponse):
    """Extended response returned ONLY at creation time."""

    secret: str  # Plaintext HMAC secret -- display-once


class EventHistoryResponse(BaseModel):
    """Single event history entry for display."""

    id: str
    status: str
    payload_hash: str
    payload_size_bytes: int
    session_id: str | None = None
    error_message: str | None = None
    created_at: datetime


class EventHistoryListResponse(BaseModel):
    """Paginated event history response."""

    events: list[EventHistoryResponse]
    total: int
    has_more: bool


class EventTestRequest(BaseModel):
    """Request to test an event pipeline."""

    payload: dict = Field(
        default_factory=lambda: {
            "alertname": "HighCPUUsage",
            "severity": "warning",
            "instance": "web-server-01",
            "description": "CPU usage above 90% for 5 minutes",
        }
    )


class EventTestStepResponse(BaseModel):
    """Single step result in test pipeline."""

    step: str
    status: str
    detail: str | None = None


class EventTestResponse(BaseModel):
    """Full test pipeline response."""

    steps: list[EventTestStepResponse]
    status: str
    session_id: str | None = None
    rendered_prompt: str | None = None
    error: str | None = None


class GeneratePromptRequest(BaseModel):
    """Optional request body for LLM-generated event prompt template."""

    user_instructions: str | None = Field(
        None,
        max_length=2000,
        description="User's instructions or existing prompt to refine. "
        "When provided, the LLM uses this as the primary guide.",
    )


class GeneratePromptResponse(BaseModel):
    """Response for LLM-generated event prompt template."""

    prompt: str


# ---------------------------------------------------------------------------
# Connector type context for prompt generation
# ---------------------------------------------------------------------------

CONNECTOR_TYPE_PROMPTS: dict[str, str] = {
    "jira": "Jira issue tracking. Event payloads include issue key, status, priority, assignee, and changelog. Use {{payload}} for the full event and {{payload.issue.key}} for specific fields.",
    "confluence": "Confluence wiki. Event payloads include page title, space key, author, and change type. Use {{payload}} for the full event and {{payload.page.title}} for specific fields.",
    "prometheus": "Prometheus monitoring. Event payloads include alert name, severity, instance, and metric values. Use {{payload}} for the full alert and {{payload.alerts[0].labels.alertname}} for specific fields.",
    "alertmanager": "Alertmanager alert groups. Event payloads include firing/resolved alerts with labels, annotations, and severity. Use {{payload}} for the full alert group and {{payload.alerts[0].labels.alertname}} for specific fields.",
    "kubernetes": "Kubernetes cluster events. Event payloads include resource kind, name, namespace, and event type. Use {{payload}} for the full event and {{payload.involvedObject.kind}} for specific fields.",
    "vmware": "VMware vSphere infrastructure. Event payloads include VM name, host, event type, and datacenter. Use {{payload}} for the full event and {{payload.vm.name}} for specific fields.",
    "loki": "Grafana Loki log aggregation. Event payloads include log stream labels, alert conditions, and sample log lines.",
    "tempo": "Grafana Tempo distributed tracing. Event payloads include trace context, span information, and service details.",
}

# Default context for connector types without specific mapping
_DEFAULT_TYPE_CONTEXT = (
    "Generic API connector. Event payloads vary by the external system configuration."
)


# ---------------------------------------------------------------------------
# Helper: verify connector belongs to tenant
# ---------------------------------------------------------------------------


async def _verify_connector(session, connector_id: str, tenant_id: str):
    """Verify connector exists and belongs to the user's tenant. Raises 404 if not found."""
    import uuid

    from sqlalchemy import select

    from meho_app.modules.connectors.models import ConnectorModel

    query = select(ConnectorModel).where(
        ConnectorModel.id == uuid.UUID(connector_id),
        ConnectorModel.tenant_id == tenant_id,
    )
    result = await session.execute(query)
    connector = result.scalar_one_or_none()
    if not connector:
        raise HTTPException(status_code=404, detail="Connector not found")
    return connector


async def _verify_event_registration(session, event_id: str, connector_id: str):
    """Verify event registration exists and belongs to the specified connector. Raises 404 if not found."""
    import uuid

    from sqlalchemy import select

    from meho_app.modules.connectors.models import EventRegistrationModel

    query = select(EventRegistrationModel).where(
        EventRegistrationModel.id == uuid.UUID(event_id),
        EventRegistrationModel.connector_id == uuid.UUID(connector_id),
    )
    result = await session.execute(query)
    registration = result.scalar_one_or_none()
    if not registration:
        raise HTTPException(status_code=404, detail="Event registration not found")
    return registration


# ---------------------------------------------------------------------------
# Helper: compute events_today for an event registration or list
# ---------------------------------------------------------------------------


async def _get_events_today_counts(session, registration_ids: list[str]) -> dict[str, int]:
    """Get today's event counts grouped by event_registration_id."""
    if not registration_ids:
        return {}

    import uuid

    from sqlalchemy import func, select

    from meho_app.modules.connectors.models import EventHistoryModel

    today_start = datetime.now(tz=UTC).replace(hour=0, minute=0, second=0, microsecond=0)

    stmt = (
        select(
            EventHistoryModel.event_registration_id,
            func.count().label("count"),
        )
        .where(
            EventHistoryModel.event_registration_id.in_(
                [uuid.UUID(rid) for rid in registration_ids]
            ),
            EventHistoryModel.created_at >= today_start,
        )
        .group_by(EventHistoryModel.event_registration_id)
    )
    result = await session.execute(stmt)
    return {str(row.event_registration_id): row.count for row in result.all()}


def _build_event_response(registration, events_today: int = 0) -> dict:
    """Build an EventResponse dict from a registration model."""
    return {
        "id": str(registration.id),
        "name": registration.name,
        "event_url": f"/api/events/{registration.id}",
        "prompt_template": registration.prompt_template,
        "rate_limit_per_hour": registration.rate_limit_per_hour,
        "is_active": registration.is_active,
        "require_signature": registration.require_signature,
        "total_events_received": registration.total_events_received,
        "total_events_processed": registration.total_events_processed,
        "total_events_deduplicated": registration.total_events_deduplicated,
        "last_event_at": registration.last_event_at,
        "created_at": registration.created_at,
        "updated_at": registration.updated_at,
        "events_today": events_today,
        # Identity model (Phase 74)
        "created_by_user_id": registration.created_by_user_id,
        "allowed_connector_ids": registration.allowed_connector_ids,
        "delegation_active": registration.delegation_active,
        # Phase 75: notification targets
        "notification_targets": registration.notification_targets,
        # Phase 94: response channel config
        "response_config": registration.response_config,
    }


# ---------------------------------------------------------------------------
# CRUD endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/{connector_id}/events",
    response_model=EventCreateResponse,
    status_code=201,
    responses={400: {"description": "Bad request"}, 500: {"description": "Internal error: ..."}},
)
async def create_event_registration(
    connector_id: str,
    request: EventCreateRequest,
    user: Annotated[UserContext, Depends(RequirePermission(Permission.CONNECTOR_UPDATE))],
):
    """
    Create an event registration for a connector.

    Returns the event registration details INCLUDING the plaintext HMAC secret.
    This is the ONLY time the secret is returned in plaintext.
    """
    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.connectors.event_service import EventService
    from meho_app.modules.connectors.repositories.credential_repository import CredentialRepository

    session_maker = create_openapi_session_maker()

    try:
        async with session_maker() as session:
            await _verify_connector(session, connector_id, user.tenant_id)

            # Validate credential availability for allowed connectors
            if request.allowed_connector_ids:
                cred_repo = CredentialRepository(session)
                missing_creds = []
                for cid in request.allowed_connector_ids:
                    creds = await cred_repo.get_credentials(user.user_id, cid)
                    if not creds:
                        missing_creds.append(cid)
                if missing_creds:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"No credentials (service or personal) for connectors: {', '.join(missing_creds)}. "
                            "Event registrations require either service credentials or your personal credentials for the target connectors."
                        ),
                    )

            event_service = EventService(session)
            registration, plaintext_secret = await event_service.create_event_registration(
                connector_id=connector_id,
                tenant_id=user.tenant_id,
                name=request.name,
                prompt_template=request.prompt_template,
                rate_limit_per_hour=request.rate_limit_per_hour,
                require_signature=request.require_signature,
                created_by_user_id=user.user_id,
                allowed_connector_ids=request.allowed_connector_ids,
                notification_targets=request.notification_targets,
                response_config=request.response_config,
            )
            await session.commit()

            logger.info(
                "event_registration_created",
                connector_id=connector_id,
                event_registration_id=str(registration.id),
            )

            resp = _build_event_response(registration, events_today=0)
            resp["secret"] = plaintext_secret
            return EventCreateResponse(**resp)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating event registration for connector {connector_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Internal error: {e!s}") from e


@router.get(
    "/{connector_id}/events",
    response_model=list[EventResponse],
    responses={500: {"description": "Internal error: ..."}},
)
async def list_event_registrations(
    connector_id: str,
    user: Annotated[UserContext, Depends(RequirePermission(Permission.CONNECTOR_READ))],
):
    """
    List all event registrations for a connector.
    """
    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.connectors.event_service import EventService

    session_maker = create_openapi_session_maker()

    try:
        async with session_maker() as session:
            await _verify_connector(session, connector_id, user.tenant_id)

            event_service = EventService(session)
            registrations = await event_service.list_event_registrations_for_connector(connector_id)

            # Get today's event counts in a single grouped query
            registration_ids = [str(r.id) for r in registrations]
            today_counts = await _get_events_today_counts(session, registration_ids)

            return [
                EventResponse(
                    **_build_event_response(r, events_today=today_counts.get(str(r.id), 0))
                )
                for r in registrations
            ]
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing event registrations for connector {connector_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Internal error: {e!s}") from e


@router.get(
    "/{connector_id}/events/{event_id}",
    response_model=EventResponse,
    responses={500: {"description": "Internal error: ..."}},
)
async def get_event_registration(
    connector_id: str,
    event_id: str,
    user: Annotated[UserContext, Depends(RequirePermission(Permission.CONNECTOR_READ))],
):
    """
    Get a single event registration by ID.
    """
    from meho_app.api.database import create_openapi_session_maker

    session_maker = create_openapi_session_maker()

    try:
        async with session_maker() as session:
            await _verify_connector(session, connector_id, user.tenant_id)
            registration = await _verify_event_registration(session, event_id, connector_id)

            today_counts = await _get_events_today_counts(session, [event_id])

            return EventResponse(
                **_build_event_response(
                    registration, events_today=today_counts.get(str(registration.id), 0)
                )
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting event registration {event_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Internal error: {e!s}") from e


@router.patch(
    "/{connector_id}/events/{event_id}",
    response_model=EventResponse,
    responses={400: {"description": "Bad request"}, 500: {"description": "Internal error: ..."}},
)
async def update_event_registration(
    connector_id: str,
    event_id: str,
    request: EventUpdateRequest,
    user: Annotated[UserContext, Depends(RequirePermission(Permission.CONNECTOR_UPDATE))],
):
    """
    Update an event registration (PATCH semantics -- only non-None fields).
    """
    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.connectors.event_service import EventService

    session_maker = create_openapi_session_maker()

    try:
        async with session_maker() as session:
            await _verify_connector(session, connector_id, user.tenant_id)
            await _verify_event_registration(session, event_id, connector_id)

            # Collect only non-None fields for PATCH semantics
            update_fields = {k: v for k, v in request.model_dump().items() if v is not None}

            if update_fields:
                event_service = EventService(session)
                registration = await event_service.update_event_registration(
                    event_id, **update_fields
                )
            else:
                registration = await _verify_event_registration(session, event_id, connector_id)

            await session.commit()

            today_counts = await _get_events_today_counts(session, [event_id])

            logger.info(
                "event_registration_updated",
                connector_id=connector_id,
                event_id=event_id,
                fields=list(update_fields.keys()),
            )

            return EventResponse(
                **_build_event_response(
                    registration, events_today=today_counts.get(str(registration.id), 0)
                )
            )
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.error(f"Error updating event registration {event_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Internal error: {e!s}") from e


@router.delete(
    "/{connector_id}/events/{event_id}", responses={500: {"description": "Internal error: ..."}}
)
async def delete_event_registration(
    connector_id: str,
    event_id: str,
    user: Annotated[UserContext, Depends(RequirePermission(Permission.CONNECTOR_UPDATE))],
):
    """
    Delete an event registration and all associated history.
    """
    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.connectors.event_service import EventService

    session_maker = create_openapi_session_maker()

    try:
        async with session_maker() as session:
            await _verify_connector(session, connector_id, user.tenant_id)
            await _verify_event_registration(session, event_id, connector_id)

            event_service = EventService(session)
            await event_service.delete_event_registration(event_id)
            await session.commit()

            logger.info(
                "event_registration_deleted",
                connector_id=connector_id,
                event_id=event_id,
            )

            return {"deleted": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting event registration {event_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Internal error: {e!s}") from e


# ---------------------------------------------------------------------------
# Event history endpoint
# ---------------------------------------------------------------------------


@router.get(
    "/{connector_id}/events/{event_id}/history",
    response_model=EventHistoryListResponse,
    responses={500: {"description": "Internal error: ..."}},
)
async def list_event_history(
    connector_id: str,
    event_id: str,
    user: Annotated[UserContext, Depends(RequirePermission(Permission.CONNECTOR_READ))],
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    """
    List event history with pagination, ordered by created_at DESC.
    """
    import uuid

    from sqlalchemy import func, select

    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.connectors.models import EventHistoryModel

    session_maker = create_openapi_session_maker()

    try:
        async with session_maker() as session:
            await _verify_connector(session, connector_id, user.tenant_id)
            await _verify_event_registration(session, event_id, connector_id)

            reg_uuid = uuid.UUID(event_id)

            # Count total events
            count_stmt = (
                select(func.count())
                .select_from(EventHistoryModel)
                .where(EventHistoryModel.event_registration_id == reg_uuid)
            )
            total = (await session.execute(count_stmt)).scalar() or 0

            # Fetch page
            stmt = (
                select(EventHistoryModel)
                .where(EventHistoryModel.event_registration_id == reg_uuid)
                .order_by(EventHistoryModel.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
            result = await session.execute(stmt)
            events = result.scalars().all()

            return EventHistoryListResponse(
                events=[
                    EventHistoryResponse(
                        id=str(e.id),
                        status=e.status,
                        payload_hash=e.payload_hash,
                        payload_size_bytes=e.payload_size_bytes,
                        session_id=str(e.session_id) if e.session_id else None,
                        error_message=e.error_message,
                        created_at=e.created_at,
                    )
                    for e in events
                ],
                total=total,
                has_more=(offset + limit) < total,
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing history for event registration {event_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Internal error: {e!s}") from e


# ---------------------------------------------------------------------------
# Prompt generation (LLM-assisted)
# ---------------------------------------------------------------------------


@router.post(
    "/{connector_id}/events/generate-prompt",
    response_model=GeneratePromptResponse,
)
async def generate_event_prompt(
    connector_id: str,
    user: Annotated[UserContext, Depends(RequirePermission(Permission.CONNECTOR_READ))],
    request: GeneratePromptRequest | None = None,
):
    """Generate a connector-type-aware prompt template using LLM.

    When ``user_instructions`` is provided, the LLM treats it as the
    primary guide and produces a refined Jinja2 prompt template that
    fulfils the user's intent while incorporating correct template
    variables for the connector type.

    When no instructions are provided, the LLM generates a generic
    connector-type-aware prompt from scratch.
    """
    from meho_app.api.database import create_openapi_session_maker

    session_maker = create_openapi_session_maker()
    user_instructions = request.user_instructions if request else None

    try:
        async with session_maker() as session:
            connector = await _verify_connector(session, connector_id, user.tenant_id)
            connector_type = connector.connector_type or "rest"
            type_context = CONNECTOR_TYPE_PROMPTS.get(connector_type, _DEFAULT_TYPE_CONTEXT)

            from pydantic_ai import Agent

            variables_doc = (
                "Available Jinja2 template variables:\n"
                "- {{payload}} -- full payload dict (use for field access: {{payload.issue.key}})\n"
                "- {{connector_name}} -- human name of the source connector\n"
                "- {{event_type}} -- event type (e.g. 'jira:issue_created')\n"
                "- Nested field access: {{payload.issue.fields.summary}}, {{payload.alerts[0].labels.alertname}}"
            )

            if user_instructions:
                system_prompt = (
                    f"You are helping create an event prompt template for a {connector_type} "
                    f"connector named '{connector.name}'.\n"
                    f"Connector context: {type_context}\n\n"
                    "The user has described what they want MEHO (an AI operations assistant) to do "
                    "when an event arrives. Your job is to turn their instructions into a "
                    "well-structured Jinja2 prompt template that MEHO will receive.\n\n"
                    f"{variables_doc}\n\n"
                    "Rules:\n"
                    "- Preserve the user's intent faithfully -- do NOT ignore or override their instructions\n"
                    "- ALWAYS use specific payload fields ({{payload.issue.key}}, {{payload.issue.fields.summary}}) "
                    "instead of {{payload}} to avoid dumping the entire raw JSON into the prompt\n"
                    "- The prompt should be actionable and 2-5 sentences\n"
                    "- If the user mentions specific actions (e.g. comment back, create ticket), include them\n"
                    "- Return ONLY the prompt template text. No markdown, no code fences, no explanation."
                )
                user_message = f"User's instructions:\n{user_instructions}"
            else:
                system_prompt = (
                    f"Generate a concise event investigation prompt template for a {connector_type} connector. "
                    f"Context: {type_context}\n\n"
                    f"{variables_doc}\n\n"
                    "The prompt should instruct MEHO (an AI operations assistant) to investigate the incoming event. "
                    "ALWAYS use specific payload fields (e.g. {{payload.issue.key}}) instead of {{payload}} "
                    "to avoid dumping the entire raw JSON into the prompt. "
                    "The prompt should be 2-4 sentences, actionable, and specific to the connector type. "
                    "Generate a DIFFERENT prompt each time -- vary the investigation angle, focus area, and phrasing. "
                    "Return ONLY the prompt template text. No markdown, no code fences, no explanation."
                )
                user_message = f"Generate an event prompt for a {connector_type} connector named '{connector.name}'"

            agent = Agent("anthropic:claude-sonnet-4-6", system_prompt=system_prompt)

            # Retry once on timeout (Anthropic cold starts can be slow)
            last_err: Exception | None = None
            for attempt in range(2):
                try:
                    result = await asyncio.wait_for(
                        agent.run(user_message),
                        timeout=30.0,
                    )
                    generated = str(result.output).strip().strip("`\"'")
                    return GeneratePromptResponse(prompt=generated)
                except TimeoutError as e:
                    last_err = e
                    if attempt == 0:
                        logger.warning(
                            f"Prompt generation attempt {attempt + 1} timed out for "
                            f"connector {connector_id}, retrying..."
                        )

            raise HTTPException(
                status_code=422,
                detail="Prompt generation timed out. Please try again.",
            ) from last_err
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error generating event prompt for connector {connector_id}: {e}")
        raise HTTPException(
            status_code=422,
            detail=f"Failed to generate prompt: {e}",
        ) from e


# ---------------------------------------------------------------------------
# Test event pipeline
# ---------------------------------------------------------------------------


@router.post(
    "/{connector_id}/events/{event_id}/test",
    response_model=EventTestResponse,
    responses={500: {"description": "Internal error: ..."}},
)
async def test_event_pipeline(
    connector_id: str,
    event_id: str,
    request: EventTestRequest,
    background_tasks: BackgroundTasks,
    user: Annotated[UserContext, Depends(RequirePermission(Permission.CONNECTOR_UPDATE))],  # noqa: PT028 -- intentional default value
):
    """
    Test event pipeline end-to-end.

    Bypasses HMAC verification (operator is already JWT-authenticated).
    Steps 1-2 are synchronous, step 3 (agent investigation) runs in background.
    Returns step-by-step progress, session link, and rendered prompt.
    """
    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.agents.service import AgentService
    from meho_app.modules.connectors.event_executor import (
        EventPromptRenderer,
        execute_event_investigation,
    )
    from meho_app.modules.connectors.event_service import EventService

    session_maker = create_openapi_session_maker()
    steps: list[dict] = []

    try:
        async with session_maker() as session:
            connector = await _verify_connector(session, connector_id, user.tenant_id)
            registration = await _verify_event_registration(session, event_id, connector_id)

            # Step 1: Render prompt template
            try:
                renderer = EventPromptRenderer()
                rendered_prompt = renderer.render_prompt(
                    registration.prompt_template,
                    request.payload,
                    connector_name=connector.name,
                )
                steps.append(
                    {
                        "step": "template_rendered",
                        "status": "success",
                        "detail": f"Rendered {len(rendered_prompt)} characters",
                    }
                )
            except ValueError as e:
                steps.append(
                    {
                        "step": "template_rendered",
                        "status": "failed",
                        "detail": str(e),
                    }
                )
                return EventTestResponse(
                    steps=[EventTestStepResponse(**s) for s in steps],
                    status="failed",
                    rendered_prompt=None,
                    error=str(e),
                )

            # Step 2: Create session
            try:
                agent_service = AgentService(session)
                test_title = f"Test: {connector.name} event"
                chat_session = await agent_service.create_chat_session(
                    tenant_id=user.tenant_id,
                    user_id=user.user_id,
                    title=test_title,
                    visibility="tenant",
                    created_by_name=user.name or connector.name,
                    trigger_source=connector.name,
                )
                session_id = str(chat_session.id)

                # Save rendered prompt as first user message
                await agent_service.add_chat_message(
                    session_id=session_id,
                    role="user",
                    content=rendered_prompt,
                    sender_id=user.user_id,
                    sender_name=user.name or connector.name,
                )

                await session.commit()

                steps.append(
                    {
                        "step": "session_created",
                        "status": "success",
                        "detail": f"Session {session_id[:8]}...",
                    }
                )
            except Exception as e:
                steps.append(
                    {
                        "step": "session_created",
                        "status": "failed",
                        "detail": str(e),
                    }
                )
                return EventTestResponse(
                    steps=[EventTestStepResponse(**s) for s in steps],
                    status="failed",
                    rendered_prompt=rendered_prompt,
                    error=str(e),
                )

            # Step 3: Launch agent investigation in background
            payload_json = json.dumps(request.payload)
            payload_hash = hashlib.sha256(payload_json.encode()).hexdigest()

            background_tasks.add_task(
                execute_event_investigation,
                registration_id=event_id,
                _registration_id=event_id,
                connector_id=connector_id,
                connector_name=connector.name,
                tenant_id=user.tenant_id,
                payload=request.payload,
                payload_hash=payload_hash,
                _raw_body_size=len(payload_json.encode()),
                prompt_template=registration.prompt_template,
                session_id=session_id,
                rendered_prompt=rendered_prompt,
            )
            steps.append(
                {
                    "step": "investigation_started",
                    "status": "success",
                    "detail": "Agent investigation launched in background",
                }
            )

            # Log the test event
            try:
                event_service = EventService(session)
                await event_service.log_event(
                    registration_id=event_id,
                    tenant_id=user.tenant_id,
                    status="test",
                    payload_hash=payload_hash,
                    payload_size_bytes=len(payload_json.encode()),
                    session_id=session_id,
                )
                await session.commit()
            except Exception as log_err:
                logger.warning(f"Failed to log test event: {log_err}")

            logger.info(
                "event_test_completed",
                connector_id=connector_id,
                event_id=event_id,
                session_id=session_id,
            )

            return EventTestResponse(
                steps=[EventTestStepResponse(**s) for s in steps],
                status="success",
                session_id=session_id,
                rendered_prompt=rendered_prompt,
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error testing event pipeline {event_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Internal error: {e!s}") from e
