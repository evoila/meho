# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
HTTP routes for Ingestion Service.

Generic webhook endpoints + admin endpoints for template management.
"""

import hashlib
import hmac
import json
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_app.core.errors import NotFoundError, ValidationError
from meho_app.core.otel import get_logger
from meho_app.database import get_db_session
from meho_app.modules.connectors.models import ConnectorModel
from meho_app.modules.ingestion.api_schemas import HealthResponse, WebhookResponse
from meho_app.modules.ingestion.deps import get_template_repository, get_webhook_processor
from meho_app.modules.ingestion.processor import GenericWebhookProcessor
from meho_app.modules.ingestion.repository import EventTemplateRepository
from meho_app.modules.ingestion.schemas import (
    EventTemplate,
    EventTemplateCreate,
    EventTemplateFilter,
    EventTemplateUpdate,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/ingestion", tags=["ingestion"])


@router.get("/health")
async def health_check() -> HealthResponse:
    """Health check endpoint"""
    return HealthResponse(status="healthy", version="0.2.0")


@router.post(
    "/webhooks/{connector_id}/{event_type}",
    response_model=None,
    status_code=status.HTTP_202_ACCEPTED,
)
async def handle_generic_webhook(
    connector_id: str,
    event_type: str,
    request: Request,
    background_tasks: BackgroundTasks,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    processor: Annotated[GenericWebhookProcessor, Depends(get_webhook_processor)],
    x_webhook_signature: Annotated[str | None, Header(alias="X-Webhook-Signature")] = None,
    x_system_id: Annotated[str | None, Header(alias="X-System-ID")] = None,
) -> WebhookResponse:
    """
    **Generic webhook endpoint - works with ANY system!**

    Processing is defined by event templates (configuration), not code.

    **Usage:**

    1. Create an event template for your connector + event type
    2. Send webhook to: `/webhooks/{connector_id}/{event_type}`
    3. MEHO processes it using your template

    **Examples:**

    - `POST /webhooks/github-prod/push` - GitHub push events
    - `POST /webhooks/argocd-prod/sync_status` - ArgoCD sync events
    - `POST /webhooks/datadog-prod/alert` - Datadog alerts
    - `POST /webhooks/custom-system/custom_event` - Your custom events!

    **Headers:**
    - `X-Webhook-Signature` (required when connector has webhook_secret): HMAC-SHA256 signature
    - `X-System-ID` (optional): Optional system ID

    **Body:**
    - JSON payload from the external system

    **Response:**
    - Status: 202 Accepted
    - Body: `{ "status": "accepted", "event_id": "evt_xxx" }`

    **Security:**
    Tenant is derived from the connector record (not from headers).
    When a connector has a webhook_secret configured, the X-Webhook-Signature
    header is required and verified using HMAC-SHA256.

    **Note:**
    If no template exists for {connector_id}/{event_type}, processing will fail.
    Create a template first using `POST /ingestion/templates`.
    """
    # Read raw body for HMAC verification (must happen before JSON parsing)
    raw_body = await request.body()

    # Parse JSON payload from raw body
    try:
        payload: dict[str, Any] = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc

    # Look up connector to get tenant_id and webhook_secret
    try:
        connector_uuid = uuid.UUID(connector_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid connector_id format") from exc

    result = await session.execute(
        select(
            ConnectorModel.tenant_id,
            ConnectorModel.webhook_secret,
            ConnectorModel.is_active,
        ).where(ConnectorModel.id == connector_uuid)
    )
    connector_row = result.one_or_none()

    if not connector_row:
        # 404 (not 403) to prevent connector ID enumeration
        raise HTTPException(status_code=404, detail="Connector not found")

    tenant_id: str = connector_row.tenant_id
    webhook_secret: str | None = connector_row.webhook_secret
    is_active: bool = connector_row.is_active

    if not is_active:
        raise HTTPException(status_code=404, detail="Connector not found")

    # HMAC-SHA256 signature verification
    if webhook_secret:
        if not x_webhook_signature:
            raise HTTPException(
                status_code=401,
                detail="Missing X-Webhook-Signature header",
            )

        # Compute expected HMAC-SHA256 signature
        expected = hmac.new(
            webhook_secret.encode("utf-8"),
            raw_body,
            hashlib.sha256,
        ).hexdigest()

        # Support both raw hex and sha256=-prefixed formats
        expected_prefixed = f"sha256={expected}"

        sig = x_webhook_signature
        if sig.startswith("sha256="):
            valid = hmac.compare_digest(sig, expected_prefixed)
        else:
            valid = hmac.compare_digest(sig, expected)

        if not valid:
            raise HTTPException(status_code=401, detail="Invalid webhook signature")
    else:
        logger.warning(
            "Webhook received without HMAC verification (no webhook_secret configured)",
            extra={"connector_id": connector_id},
        )

    event_id = f"evt_{uuid.uuid4().hex[:12]}"

    logger.info(
        f"Received webhook: {connector_id}/{event_type} (tenant: {tenant_id}, event_id: {event_id}, hmac: {'verified' if webhook_secret else 'none'})",
    )

    # Queue background task for processing -- tenant derived from connector record
    background_tasks.add_task(
        _process_webhook_background,
        processor=processor,
        connector_id=connector_id,
        event_type=event_type,
        payload=payload,
        tenant_id=tenant_id,
        system_id=x_system_id,
        event_id=event_id,
    )

    return WebhookResponse(
        status="accepted", message="Event queued for processing", event_id=event_id
    )


async def _process_webhook_background(
    processor: GenericWebhookProcessor,
    connector_id: str,
    event_type: str,
    payload: dict[str, Any],
    tenant_id: str,
    system_id: str | None,
    event_id: str,
) -> None:
    """Background task to process webhook"""
    try:
        await processor.process_webhook(
            connector_id=connector_id,
            event_type=event_type,
            payload=payload,
            tenant_id=tenant_id,
            system_id=system_id,
        )
        logger.info(f"Successfully processed webhook {event_id}")
    except Exception as e:
        logger.error(f"Failed to process webhook {event_id}: {e}", exc_info=True)


# ============================================================================
# Admin Endpoints - Event Template Management
# ============================================================================


@router.post(
    "/templates",
    status_code=status.HTTP_201_CREATED,
    responses={
        400: {"description": "Bad request"},
        500: {"description": "Failed to create template: ..."},
    },
)
async def create_template(
    template_create: EventTemplateCreate,
    repo: Annotated[EventTemplateRepository, Depends(get_template_repository)],
) -> EventTemplate:
    """
    **Create a new event template.**

    Templates define how to process webhook events for a specific connector + event type.

    **Example:**

    ```json
    {
      "connector_id": "github-prod",
      "event_type": "push",
      "text_template": "Push to {{ payload.repository.full_name }}\\nCommits: {{ payload.commits | length }}",
      "tag_rules": [
        "source:github",
        "repo:{{ payload.repository.full_name }}",
        "branch:{{ payload.ref | replace('refs/heads/', '') }}"
      ],
      "issue_detection_rule": "false",
      "tenant_id": "tenant-1"
    }
    ```

    **Note:** Templates use Jinja2 syntax. Test your templates before deploying!
    """
    try:
        template = await repo.create_template(template_create)
        return EventTemplate.model_validate(template)
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create template: {e!s}") from e


@router.get("/templates")
async def list_templates(
    repo: Annotated[EventTemplateRepository, Depends(get_template_repository)],
    connector_id: str | None = None,
    event_type: str | None = None,
    tenant_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[EventTemplate]:
    """
    **List event templates with optional filtering.**

    **Query Parameters:**
    - `connector_id`: Filter by connector
    - `event_type`: Filter by event type
    - `tenant_id`: Filter by tenant
    - `limit`: Max results (default: 100)
    - `offset`: Pagination offset (default: 0)

    **Example:**

    ```
    GET /ingestion/templates?connector_id=github-prod
    ```
    """
    filter = EventTemplateFilter(
        connector_id=connector_id,
        event_type=event_type,
        tenant_id=tenant_id,
        limit=limit,
        offset=offset,
    )

    templates = await repo.list_templates(filter)
    return [EventTemplate.model_validate(t) for t in templates]


@router.get("/templates/{template_id}", responses={404: {"description": "Template ... not found"}})
async def get_template(
    template_id: str, repo: Annotated[EventTemplateRepository, Depends(get_template_repository)]
) -> EventTemplate:
    """
    **Get a specific event template by ID.**

    **Example:**

    ```
    GET /ingestion/templates/550e8400-e29b-41d4-a716-446655440000
    ```
    """
    template = await repo.get_template_by_id(template_id)

    if not template:
        raise HTTPException(status_code=404, detail=f"Template {template_id} not found")

    return EventTemplate.model_validate(template)


@router.put(
    "/templates/{template_id}",
    responses={
        404: {"description": "Resource not found"},
        500: {"description": "Failed to update template: ..."},
    },
)
async def update_template(
    template_id: str,
    template_update: EventTemplateUpdate,
    repo: Annotated[EventTemplateRepository, Depends(get_template_repository)],
) -> EventTemplate:
    """
    **Update an existing event template.**

    Only provided fields will be updated. Others remain unchanged.

    **Example:**

    ```json
    {
      "text_template": "Updated template: {{ payload.title }}",
      "tag_rules": ["source:updated", "type:{{ payload.type }}"]
    }
    ```
    """
    try:
        template = await repo.update_template(template_id, template_update)
        return EventTemplate.model_validate(template)
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update template: {e!s}") from e


@router.delete(
    "/templates/{template_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        404: {"description": "Resource not found"},
        500: {"description": "Failed to delete template: ..."},
    },
)
async def delete_template(
    template_id: str, repo: Annotated[EventTemplateRepository, Depends(get_template_repository)]
) -> None:
    """
    **Delete an event template.**

    **Warning:** Webhooks for this connector + event type will fail after deletion!

    **Example:**

    ```
    DELETE /ingestion/templates/550e8400-e29b-41d4-a716-446655440000
    ```
    """
    try:
        await repo.delete_template(template_id)
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete template: {e!s}") from e
