# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Public event ingestion endpoint for MEHO event infrastructure.

Receives POST requests from external systems (Alertmanager, CI/CD, etc.),
verifies HMAC-SHA256 signatures, checks dedup/rate-limit via Redis, and
dispatches a background task that creates a group session and launches
agent investigation.

Security:
- NO JWT auth -- HMAC signature is the sole authentication gate
- Raw body read BEFORE JSON parsing (signature covers raw bytes)
- Tenant isolation from registration (never from payload)
"""

import hashlib
import json

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from meho_app.core.otel import get_logger
from meho_app.modules.connectors.event_executor import execute_event_investigation

logger = get_logger(__name__)

router = APIRouter(prefix="/events", tags=["events"])


@router.post("/{event_id}")
async def receive_event(
    event_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
):
    """
    Receive and process an incoming event.

    This endpoint has NO JWT auth dependency -- HMAC-SHA256 is the only
    authentication gate. The full processing flow:

    1. Read raw body (before any JSON parsing -- HMAC signs raw bytes)
    2. Look up event registration (404 / 410)
    3. Decrypt HMAC secret and verify signature (401)
    4. Check dedup via Redis (200 duplicate_suppressed)
    5. Check rate limit via Redis (429)
    6. Parse JSON payload (400)
    7. Log event as "processed"
    8. Dispatch background task for session creation + agent investigation
    9. Return 202 immediately

    Args:
        event_id: UUID of the event registration (path parameter).
        request: FastAPI Request for raw body and headers.
        background_tasks: FastAPI BackgroundTasks for async dispatch.

    Returns:
        202 on success, or appropriate error status.
    """
    from meho_app.api.config import get_api_config
    from meho_app.core.redis import get_redis_client
    from meho_app.database import get_session_maker
    from meho_app.modules.connectors.event_service import EventService

    # 1. Read raw body BEFORE any JSON parsing (pitfall #1: HMAC signs raw bytes)
    raw_body = await request.body()

    # 2. Create own DB session (NOT request-scoped -- no auth middleware)
    session_maker = get_session_maker()
    async with session_maker() as db:
        try:
            event_service = EventService(db)

            # 3. Look up event registration
            registration = await event_service.get_event_registration(event_id)
            if registration is None:
                raise HTTPException(status_code=404, detail="Event registration not found")
            if not registration.is_active:
                raise HTTPException(status_code=410, detail="Event registration is disabled")

            # 4. Verify HMAC signature (unless opted out)
            if registration.require_signature:
                try:
                    secret = event_service.decrypt_secret(registration.encrypted_secret)
                    EventService.verify_hmac_signature(
                        raw_body,
                        request.headers.get("X-Webhook-Signature", ""),
                        secret,
                    )
                except ValueError as e:
                    logger.warning(f"Event {event_id}: HMAC verification failed -- {e}")
                    raise HTTPException(status_code=401, detail=str(e)) from e
            else:
                logger.info(
                    f"Event {event_id}: signature verification skipped "
                    f"(require_signature=False)"
                )

            # 5. Compute payload hash for dedup
            payload_hash = hashlib.sha256(raw_body).hexdigest()

            # 6. Get Redis client
            config = get_api_config()
            redis_client = await get_redis_client(config.redis_url)

            # 7. Dedup check
            is_dup = await EventService.is_duplicate(redis_client, event_id, payload_hash)
            if is_dup:
                await event_service.log_event(
                    registration_id=event_id,
                    tenant_id=registration.tenant_id,
                    status="deduplicated",
                    payload_hash=payload_hash,
                    payload_size_bytes=len(raw_body),
                )
                await db.commit()
                logger.info(
                    f"Event {event_id}: duplicate suppressed (hash={payload_hash[:12]}...)"
                )
                return {"status": "duplicate_suppressed"}

            # 8. Rate limit check
            is_limited = await EventService.is_rate_limited(
                redis_client, event_id, registration.rate_limit_per_hour
            )
            if is_limited:
                await event_service.log_event(
                    registration_id=event_id,
                    tenant_id=registration.tenant_id,
                    status="rate_limited",
                    payload_hash=payload_hash,
                    payload_size_bytes=len(raw_body),
                )
                await db.commit()
                logger.warning(
                    f"Event {event_id}: rate limited "
                    f"(limit={registration.rate_limit_per_hour}/hr)"
                )
                raise HTTPException(
                    status_code=429,
                    detail="Rate limit exceeded for this event registration",
                )

            # 9. Parse JSON payload
            try:
                payload = json.loads(raw_body)
            except json.JSONDecodeError:
                raise HTTPException(status_code=400, detail="Invalid JSON payload") from None

            # 10. Log event as "processed" (session_id updated later by bg task)
            await event_service.log_event(
                registration_id=event_id,
                tenant_id=registration.tenant_id,
                status="processed",
                payload_hash=payload_hash,
                payload_size_bytes=len(raw_body),
            )
            await db.commit()

            # 11. Get connector name for trigger_source
            connector_name = registration.connector.name if registration.connector else "event"

            # 12. Dispatch background task
            background_tasks.add_task(
                execute_event_investigation,
                registration_id=str(registration.id),
                _registration_id=str(registration.id),
                connector_id=str(registration.connector_id),
                connector_name=connector_name,
                tenant_id=registration.tenant_id,
                payload=payload,
                payload_hash=payload_hash,
                _raw_body_size=len(raw_body),
                prompt_template=registration.prompt_template,
            )

            logger.info(
                f"Event {event_id}: accepted, background task dispatched "
                f"(connector={connector_name}, size={len(raw_body)}B)"
            )

            # 13. Return 202 immediately
            return {"status": "accepted", "event_id": event_id}

        except HTTPException:
            raise
        except Exception as e:
            logger.error(
                f"Event {event_id}: unexpected error -- {e}",
                exc_info=True,
            )
            raise HTTPException(status_code=500, detail="Internal server error") from e
