"""
HTTP routes for Ingestion Service.

Generic webhook endpoints + admin endpoints for template management.
"""
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, status, Header
from meho_ingestion.api_schemas import WebhookResponse, HealthResponse
from meho_ingestion.processor import GenericWebhookProcessor
from meho_ingestion.schemas import (
    EventTemplateCreate,
    EventTemplateUpdate,
    EventTemplate,
    EventTemplateFilter
)
from meho_ingestion.deps import get_webhook_processor, get_template_repository
from meho_ingestion.repository import EventTemplateRepository
from typing import Optional, Dict, Any, List
from meho_core.errors import NotFoundError, ValidationError
import uuid
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ingestion", tags=["ingestion"])


@router.get("/health", response_model=HealthResponse)
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
    payload: Dict[str, Any],
    background_tasks: BackgroundTasks,
    processor: GenericWebhookProcessor = Depends(get_webhook_processor),
    x_tenant_id: Optional[str] = Header(None, alias="X-Tenant-ID"),
    x_system_id: Optional[str] = Header(None, alias="X-System-ID")
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
    - `X-Tenant-ID` (required): Your tenant ID
    - `X-System-ID` (optional): Optional system ID
    
    **Body:**
    - JSON payload from the external system
    
    **Response:**
    - Status: 202 Accepted
    - Body: `{ "status": "accepted", "event_id": "evt_xxx" }`
    
    **Note:**
    If no template exists for {connector_id}/{event_type}, processing will fail.
    Create a template first using `POST /ingestion/templates`.
    """
    if not x_tenant_id:
        raise HTTPException(
            status_code=400,
            detail="X-Tenant-ID header is required"
        )
    
    event_id = f"evt_{uuid.uuid4().hex[:12]}"
    
    logger.info(
        f"Received webhook: {connector_id}/{event_type} "
        f"(tenant: {x_tenant_id}, event_id: {event_id})"
    )
    
    # Queue background task for processing
    background_tasks.add_task(
        _process_webhook_background,
        processor=processor,
        connector_id=connector_id,
        event_type=event_type,
        payload=payload,
        tenant_id=x_tenant_id,
        system_id=x_system_id,
        event_id=event_id
    )
    
    return WebhookResponse(
        status="accepted",
        message=f"Event queued for processing",
        event_id=event_id
    )


async def _process_webhook_background(
    processor: GenericWebhookProcessor,
    connector_id: str,
    event_type: str,
    payload: Dict[str, Any],
    tenant_id: str,
    system_id: Optional[str],
    event_id: str
) -> None:
    """Background task to process webhook"""
    try:
        await processor.process_webhook(
            connector_id=connector_id,
            event_type=event_type,
            payload=payload,
            tenant_id=tenant_id,
            system_id=system_id
        )
        logger.info(f"Successfully processed webhook {event_id}")
    except Exception as e:
        logger.error(f"Failed to process webhook {event_id}: {e}", exc_info=True)


# ============================================================================
# Admin Endpoints - Event Template Management
# ============================================================================

@router.post("/templates", response_model=EventTemplate, status_code=status.HTTP_201_CREATED)
async def create_template(
    template_create: EventTemplateCreate,
    repo: EventTemplateRepository = Depends(get_template_repository)
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
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create template: {str(e)}")


@router.get("/templates", response_model=List[EventTemplate])
async def list_templates(
    connector_id: Optional[str] = None,
    event_type: Optional[str] = None,
    tenant_id: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    repo: EventTemplateRepository = Depends(get_template_repository)
) -> List[EventTemplate]:
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
        offset=offset
    )
    
    templates = await repo.list_templates(filter)
    return [EventTemplate.model_validate(t) for t in templates]


@router.get("/templates/{template_id}", response_model=EventTemplate)
async def get_template(
    template_id: str,
    repo: EventTemplateRepository = Depends(get_template_repository)
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


@router.put("/templates/{template_id}", response_model=EventTemplate)
async def update_template(
    template_id: str,
    template_update: EventTemplateUpdate,
    repo: EventTemplateRepository = Depends(get_template_repository)
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
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update template: {str(e)}")


@router.delete("/templates/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_template(
    template_id: str,
    repo: EventTemplateRepository = Depends(get_template_repository)
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
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete template: {str(e)}")
