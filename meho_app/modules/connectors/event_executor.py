# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Event background execution -- prompt rendering, LLM title, session creation, agent launch.

This module runs as a FastAPI background task after the event endpoint
returns 202. It:
1. Renders the Jinja2 prompt template with the event payload (SandboxedEnvironment)
2. Generates a human-readable session title via LLM (Sonnet 4.6 with fallback)
3. Creates a group session (visibility=tenant, trigger_source=connector name)
4. Launches agent investigation that persists results and broadcasts via Redis SSE

Security:
- SandboxedEnvironment for Jinja2 (defense-in-depth -- prevents template injection)
- Background task creates its OWN DB session (not request-scoped)
- Tenant isolation from registration (never from payload)

Agent execution:
- READ operations auto-execute (Phase 38 trust tiers handle this)
- WRITE operations pause for human approval (Phase 38 approval cards)
- Results persisted to session history and broadcast via Redis SSE
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

from jinja2 import TemplateError
from jinja2.sandbox import SandboxedEnvironment, SecurityError

from meho_app.core.otel import get_logger
from meho_app.database import get_session_maker

logger = get_logger(__name__)

SYSTEM_EVENT_USER = "system:event"


class EventPromptRenderer:
    """Render Jinja2 prompt templates in a sandboxed environment.

    Uses ``jinja2.sandbox.SandboxedEnvironment`` (NOT plain Environment) for
    defense-in-depth. Even though prompt templates are admin-authored, the
    sandbox prevents accidental or malicious template injection attacks.

    Template variables:
    - ``payload``: Full payload dict (for field access like ``payload.issue.key``)
    - ``connector_name``: Human name of the source connector
    - ``event_type``: Event type (e.g. "jira:issue_created")
    """

    def __init__(self) -> None:
        self._env = SandboxedEnvironment(
            autoescape=False,
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def render_prompt(
        self,
        template_str: str,
        payload: dict,
        *,
        connector_name: str = "",
        event_type: str = "",
    ) -> str:
        """Render a Jinja2 template with the event payload and metadata.

        Args:
            template_str: Jinja2 template string.
            payload: Event payload dict (available as ``payload``).
            connector_name: Human name of the connector (available as ``connector_name``).
            event_type: Event type string (available as ``event_type``).

        Returns:
            Rendered prompt string.

        Raises:
            ValueError: If template rendering fails.
        """
        # Detect event_type from common payload shapes if not provided
        if not event_type:
            event_type = (
                payload.get("webhookEvent", "")  # Jira
                or payload.get("event_type", "")  # Generic
                or payload.get("action", "")  # GitHub
                or payload.get("status", "")  # Alertmanager (firing/resolved)
                or payload.get("type", "")  # Kubernetes (ADDED/MODIFIED/DELETED)
                or (
                    payload["event"].get("type", "")
                    if isinstance(payload.get("event"), dict)
                    else ""
                )
                or ""
            )

        try:
            template = self._env.from_string(template_str)
            return template.render(
                payload=payload,
                connector_name=connector_name,
                event_type=event_type,
            )
        except (TemplateError, TypeError, SecurityError) as e:
            raise ValueError(f"Prompt template rendering failed: {e}") from e


async def generate_session_title(
    payload: dict,
    connector_name: str,
) -> str:
    """Generate a concise session title from the event payload via LLM.

    Uses Sonnet 4.6 (fast/cheap) with a 10-second timeout. On any failure
    (timeout, API error, unexpected output), falls back to a static title.
    Title generation must NEVER block investigation.

    Args:
        payload: Event payload dict.
        connector_name: Name of the source connector (used in fallback title).

    Returns:
        Session title string (max ~100 chars).
    """
    fallback_title = f"Event: {connector_name}"

    try:
        from pydantic_ai import Agent

        agent = Agent(
            "anthropic:claude-sonnet-4-6",
            system_prompt=(
                "Generate a concise session title (max 80 chars) from this event "
                "payload. Focus on what happened: the problem, the system, and severity. "
                "Examples: 'Pod CrashLoopBackoff in prod-api deployment', "
                "'High CPU alert on k8s-node-7', 'Jira issue INFRA-1234 created'. "
                "Return ONLY the title, no quotes or explanation."
            ),
        )

        # Truncate large payloads to avoid token waste
        payload_text = json.dumps(payload)[:2000]

        result = await asyncio.wait_for(
            agent.run(payload_text),
            timeout=10.0,
        )
        title = str(result.output).strip().strip("\"'")[:100]
        if title:
            return title
        return fallback_title

    except TimeoutError:
        logger.warning("LLM title generation timed out (10s), using fallback")
        return fallback_title
    except Exception as e:
        logger.warning(f"LLM title generation failed: {e}, using fallback")
        return fallback_title


async def _execute_response_channel(
    response_config: dict,
    payload: dict,
    result: str,
    session_id: str,
    session_title: str,
    tenant_id: str,
) -> None:
    """Execute response channel -- best-effort, never raises (per D-09).

    Posts the investigation result back to the source system using the
    connector pool. Follows the fire-and-forget pattern from
    notification_dispatcher.py.

    Args:
        response_config: Dict with connector_id, operation_id, parameter_mapping.
        payload: Original event payload (for Jinja2 template rendering).
        result: Investigation result markdown string.
        session_id: Session UUID string.
        session_title: Session title string.
        tenant_id: Tenant identifier.
    """
    try:
        connector_id = response_config["connector_id"]
        operation_id = response_config["operation_id"]
        parameter_mapping = response_config["parameter_mapping"]

        # 1. Load connector model to get type and config
        from sqlalchemy import select

        from meho_app.modules.connectors.models import ConnectorModel

        session_maker = get_session_maker()
        async with session_maker() as db:
            stmt = select(ConnectorModel).where(
                ConnectorModel.id == connector_id,
                ConnectorModel.tenant_id == tenant_id,
            )
            result_row = await db.execute(stmt)
            connector = result_row.scalar_one_or_none()
            if not connector:
                logger.warning(f"Response channel connector {connector_id} not found")
                return

            connector_type = connector.connector_type
            connector_config: dict[str, Any] = connector.protocol_config or {}  # type: ignore[assignment]  # SQLAlchemy ORM attribute
            connector_name = connector.name

        # 2. Format result for target connector type (per D-11)
        from meho_app.modules.connectors.response_formatters import (
            format_for_connector,
            render_response_parameters,
        )

        formatted_result = format_for_connector(connector_type, result)  # type: ignore[arg-type]  # SQLAlchemy ORM attribute access

        # 3. Render parameter mapping with Jinja2 (per D-08)
        rendered_params = render_response_parameters(
            parameter_mapping=parameter_mapping,
            payload=payload,
            result=formatted_result,
            session_id=session_id,
            session_title=session_title,
            connector_name=connector_name,  # type: ignore[arg-type]  # SQLAlchemy ORM attribute access
        )
        if not rendered_params:
            logger.warning("Response channel parameter rendering failed")
            return

        # 4. Resolve credentials (per D-09, use SENTINEL_SERVICE_USER)
        from meho_app.api.config import get_api_config
        from meho_app.modules.connectors.credential_resolver import (
            CredentialResolver,
            SessionType,
        )
        from meho_app.modules.connectors.keycloak_user_checker import KeycloakUserChecker
        from meho_app.modules.connectors.repositories.credential_repository import (
            UserCredentialRepository,
        )

        config = get_api_config()
        async with session_maker() as db:
            cred_repo = UserCredentialRepository(db)
            keycloak_checker = KeycloakUserChecker(
                keycloak_url=config.keycloak_url,
                admin_username=config.keycloak_admin_username,
                admin_password=config.keycloak_admin_password,
            )
            resolver = CredentialResolver(cred_repo, keycloak_checker)
            resolved = await resolver.resolve(
                session_type=SessionType.AUTOMATED_EVENT,
                user_id=CredentialResolver.SENTINEL_SERVICE_USER,
                connector_id=str(connector_id),
                tenant_id=tenant_id,
            )

        # 5. Execute operation via connector pool
        from meho_app.modules.connectors.pool import execute_connector_operation

        await execute_connector_operation(
            connector_type=connector_type,  # type: ignore[arg-type]  # SQLAlchemy ORM attribute access
            connector_id=str(connector_id),
            config=connector_config,
            credentials=resolved.credentials,
            operation_id=operation_id,
            parameters=rendered_params,
        )
        logger.info(
            "Response channel executed successfully",
            extra={"connector_id": str(connector_id), "operation_id": operation_id},
        )

    except Exception:
        logger.warning("Response channel execution failed", exc_info=True)


async def execute_event_investigation(
    registration_id: str,
    _registration_id: str,
    connector_id: str,
    connector_name: str,
    tenant_id: str,
    payload: dict,
    payload_hash: str,
    _raw_body_size: int,
    prompt_template: str,
    session_id: str | None = None,
    rendered_prompt: str | None = None,
) -> None:
    """Execute a full event investigation as a background task.

    This is the main entry point called by ``BackgroundTasks.add_task()``
    from the event endpoint. It creates its OWN database session (not
    request-scoped -- pitfall #6) and runs the full investigation pipeline:

    1. Render prompt template with payload
    2. Generate LLM session title
    3. Create group session (visibility=tenant, trigger_source=connector name)
    4. Update event history record with session_id
    5. Launch agent investigation (OrchestratorAgent via adapter)

    Args:
        registration_id: UUID of the event registration.
        _registration_id: Same as registration_id (for clarity in logging).
        connector_id: UUID of the parent connector.
        connector_name: Human name of the connector (used as trigger_source).
        tenant_id: Tenant identifier (from registration, never payload).
        payload: Parsed JSON event payload.
        payload_hash: SHA-256 hex digest of the raw body.
        _raw_body_size: Size of the raw body in bytes.
        prompt_template: Jinja2 template string for prompt rendering.
        session_id: Optional pre-created session ID (test event flow).
            When provided, the executor skips session creation / title
            generation / event update / user message and goes directly
            to agent investigation using this session.
        rendered_prompt: Optional pre-rendered prompt string. Used with
            session_id to avoid duplicate template rendering.
    """
    logger.info(
        f"Event executor started: registration={registration_id} "
        f"connector={connector_name} tenant={tenant_id}"
    )

    # Create own DB session (NOT request-scoped)
    session_maker = get_session_maker()
    async with session_maker() as db:
        try:
            # Phase 74: Load registration to extract identity fields
            from sqlalchemy import select

            from meho_app.modules.connectors.models import EventRegistrationModel

            reg_stmt = select(EventRegistrationModel).where(
                EventRegistrationModel.id == registration_id
            )
            reg_result = await db.execute(reg_stmt)
            registration = reg_result.scalar_one_or_none()

            # Extract identity fields (default to safe values if registration missing)
            _created_by_user_id = registration.created_by_user_id if registration else None
            _allowed_connector_ids = registration.allowed_connector_ids if registration else None
            _delegation_active = registration.delegation_active if registration else True
            # Phase 75: notification targets for approval alerts
            _notification_targets = registration.notification_targets if registration else None

            # ---- Pre-created session path (test event flow) ----
            if session_id:
                # Pre-created session (test event flow) -- skip creation steps
                logger.info(
                    f"Event {registration_id}: using pre-created session (id={session_id[:8]}...)"
                )
                # Use pre-rendered prompt or render now
                if not rendered_prompt:
                    renderer = EventPromptRenderer()
                    rendered_prompt = renderer.render_prompt(
                        prompt_template,
                        payload,
                        connector_name=connector_name,
                    )

                # Skip: title generation, session creation, event update, user message
                # Go directly to agent investigation
                from meho_app.modules.agents.service import AgentService

                agent_service = AgentService(db)
                await _run_agent_investigation(
                    db=db,
                    session_id=session_id,
                    tenant_id=tenant_id,
                    connector_name=connector_name,
                    rendered_prompt=rendered_prompt,
                    agent_service=agent_service,
                    created_by_user_id=_created_by_user_id,  # type: ignore[arg-type]  # SQLAlchemy ORM attribute access
                    allowed_connector_ids=_allowed_connector_ids,  # type: ignore[arg-type]  # SQLAlchemy ORM attribute access
                    delegation_active=_delegation_active,  # type: ignore[arg-type]  # SQLAlchemy ORM attribute access
                    registration_id=registration_id,
                    notification_targets=_notification_targets,  # type: ignore[arg-type]  # SQLAlchemy ORM attribute access
                    # Phase 94: response channel
                    response_config=registration.response_config if registration else None,  # type: ignore[arg-type]  # SQLAlchemy ORM attribute access
                    payload=payload,
                    session_title=connector_name,
                )
                logger.info(
                    f"Event {registration_id}: investigation complete (session={session_id[:8]}...)"
                )
                return

            # ---- Step 1: Render prompt template ----
            try:
                renderer = EventPromptRenderer()
                rendered_prompt = renderer.render_prompt(
                    prompt_template,
                    payload,
                    connector_name=connector_name,
                )
                logger.info(
                    f"Event {registration_id}: prompt rendered ({len(rendered_prompt)} chars)"
                )
            except ValueError as e:
                logger.error(f"Event {registration_id}: prompt rendering failed -- {e}")
                await _update_event_status(db, registration_id, payload_hash, "failed", str(e))
                return

            # ---- Step 2: Generate session title ----
            title = await generate_session_title(payload, connector_name)
            logger.info(f"Event {registration_id}: session title = '{title}'")

            # ---- Step 3: Create group session ----
            from meho_app.modules.agents.service import AgentService

            agent_service = AgentService(db)
            session = await agent_service.create_chat_session(
                tenant_id=tenant_id,
                user_id=SYSTEM_EVENT_USER,
                title=title,
                visibility="tenant",
                created_by_name=connector_name,
                trigger_source=connector_name,
            )
            session_id = str(session.id)
            logger.info(
                f"Event {registration_id}: session created "
                f"(id={session_id[:8]}..., visibility=tenant, "
                f"trigger_source={connector_name})"
            )

            # ---- Step 4: Update event record with session_id ----
            await _update_event_session(db, registration_id, payload_hash, session_id)

            # ---- Step 5: Save rendered prompt as first user message ----
            await agent_service.add_chat_message(
                session_id=session_id,
                role="user",
                content=rendered_prompt,
                sender_id=SYSTEM_EVENT_USER,
                sender_name=connector_name,
            )

            # ---- Step 6: Launch agent investigation ----
            await _run_agent_investigation(
                db=db,
                session_id=session_id,
                tenant_id=tenant_id,
                connector_name=connector_name,
                rendered_prompt=rendered_prompt,
                agent_service=agent_service,
                created_by_user_id=_created_by_user_id,  # type: ignore[arg-type]  # SQLAlchemy ORM attribute access
                allowed_connector_ids=_allowed_connector_ids,  # type: ignore[arg-type]  # SQLAlchemy ORM attribute access
                delegation_active=_delegation_active,  # type: ignore[arg-type]  # SQLAlchemy ORM attribute access
                registration_id=registration_id,
                notification_targets=_notification_targets,  # type: ignore[arg-type]  # SQLAlchemy ORM attribute access
                # Phase 94: response channel
                response_config=registration.response_config if registration else None,  # type: ignore[arg-type]  # SQLAlchemy ORM attribute access
                payload=payload,
                session_title=title,
            )

            logger.info(
                f"Event {registration_id}: investigation complete (session={session_id[:8]}...)"
            )

        except Exception as e:
            logger.error(
                f"Event {registration_id}: executor failed -- {e}",
                exc_info=True,
            )
            try:
                await _update_event_status(db, registration_id, payload_hash, "failed", str(e))
            except Exception as log_err:
                logger.error(f"Event {registration_id}: failed to update event status -- {log_err}")


async def _event_delegation_flag_callback(
    trigger_type: str, trigger_id: str, is_active: bool
) -> None:
    """Write delegation_active flag back to EventRegistrationModel."""
    session_maker = get_session_maker()
    async with session_maker() as session:
        from sqlalchemy import update

        from meho_app.modules.connectors.models import EventRegistrationModel

        stmt = (
            update(EventRegistrationModel)
            .where(EventRegistrationModel.id == trigger_id)
            .values(delegation_active=is_active)
        )
        await session.execute(stmt)
        await session.commit()


async def _run_agent_investigation(
    db: Any,
    session_id: str,
    tenant_id: str,
    connector_name: str,
    rendered_prompt: str,
    agent_service: Any,
    # Phase 74: automation identity context
    created_by_user_id: str | None = None,
    allowed_connector_ids: list[str] | None = None,
    delegation_active: bool = True,
    registration_id: str | None = None,
    # Phase 75: notification targets for approval alerts
    notification_targets: list[dict[str, str]] | None = None,
    # Phase 94: response channel context
    response_config: dict | None = None,
    payload: dict | None = None,
    session_title: str = "",
) -> None:
    """Run the OrchestratorAgent investigation and persist results.

    Replicates the core execution path from ``chat_stream`` in routes_chat.py
    WITHOUT the SSE streaming response. Instead, it persists the final answer
    to the session and broadcasts events via Redis SSE for live viewers.

    Args:
        db: AsyncSession for database operations.
        session_id: UUID of the created session.
        tenant_id: Tenant identifier.
        connector_name: Connector name for logging.
        rendered_prompt: The rendered investigation prompt.
        agent_service: AgentService instance for message persistence.
        created_by_user_id: JWT user_id of event registration creator (Phase 74).
        allowed_connector_ids: Connector scope (Phase 74).
        delegation_active: Current delegation_active flag (Phase 74).
        registration_id: UUID of the event registration (Phase 74).
        notification_targets: Notification targets for approval alerts (Phase 75).
    """
    from meho_app.api.config import get_api_config
    from meho_app.api.dependencies import create_agent_dependencies, create_agent_state_store
    from meho_app.core.auth_context import UserContext
    from meho_app.core.redis import get_redis_client
    from meho_app.modules.agents.adapter import run_orchestrator_streaming
    from meho_app.modules.agents.orchestrator.agent import OrchestratorAgent
    from meho_app.modules.agents.sse.broadcaster import RedisSSEBroadcaster
    from meho_app.modules.agents.unified_executor import get_unified_executor

    # Create a synthetic system user context for the event
    system_user = UserContext(
        user_id=SYSTEM_EVENT_USER,
        name=connector_name,
        tenant_id=tenant_id,
        roles=["user"],
    )

    # Create agent dependencies (same pattern as chat_stream)
    dependencies = create_agent_dependencies(
        user=system_user,
        session=db,
        current_question=rendered_prompt,
        # Phase 74: automation context
        session_type="automated_event",
        created_by_user_id=created_by_user_id,
        allowed_connector_ids=allowed_connector_ids,
        trigger_type="event",
        trigger_id=registration_id,
        delegation_active=delegation_active,
        delegation_flag_callback=_event_delegation_flag_callback,
        # Phase 75: notification targets
        notification_targets=notification_targets,
    )

    # Initialize UnifiedExecutor with Redis
    config = get_api_config()
    redis_client = get_redis_client(config.redis_url)
    get_unified_executor(redis_client=redis_client)

    # Create Redis SSE broadcaster for live viewers
    broadcaster = RedisSSEBroadcaster(redis_client)

    # Create state store for multi-turn persistence
    state_store = create_agent_state_store()

    # Create OrchestratorAgent
    agent = OrchestratorAgent(dependencies=dependencies)

    # Track final answer for persistence
    final_answer_content = None

    try:
        # Broadcast processing_started
        await broadcaster.publish(
            session_id,
            {"type": "processing_started", "sender_id": SYSTEM_EVENT_USER},
        )

        # Set SETNX processing guard
        await redis_client.set(
            f"meho:active:{session_id}",
            SYSTEM_EVENT_USER,
            nx=True,
            ex=300,
        )

        # Stream events from the orchestrator
        event_stream = run_orchestrator_streaming(
            agent=agent,
            user_message=rendered_prompt,
            session_id=session_id,
            conversation_history=[],  # No prior history for event sessions
            state_store=state_store,
        )

        async for sse_data in event_stream:
            # Broadcast to Redis for live viewers
            try:
                await broadcaster.publish(session_id, sse_data)
            except Exception as pub_err:
                logger.warning(f"Failed to publish event to Redis: {pub_err}")

            # Capture final answer
            event_type = sse_data.get("type", "")
            if event_type == "final_answer":
                final_answer_content = sse_data.get("content", "")
                logger.info(f"Event session {session_id[:8]}...: final answer ready")

        # Persist assistant response
        if final_answer_content:
            await agent_service.add_chat_message(
                session_id=session_id,
                role="assistant",
                content=final_answer_content,
            )
            logger.info(f"Event session {session_id[:8]}...: assistant message persisted")

            # Execute response channel if configured (per D-06)
            if response_config and payload is not None:
                await _execute_response_channel(
                    response_config=response_config,
                    payload=payload,
                    result=final_answer_content,
                    session_id=session_id,
                    session_title=session_title,
                    tenant_id=tenant_id,
                )

    except Exception as e:
        logger.error(
            f"Event session {session_id[:8]}...: agent investigation failed -- {e}",
            exc_info=True,
        )
        # Persist error as assistant message so it's visible in session
        with contextlib.suppress(Exception):
            await agent_service.add_chat_message(
                session_id=session_id,
                role="assistant",
                content=f"Investigation failed: {e}",
            )

    finally:
        # Broadcast processing_complete and clear active status
        with contextlib.suppress(Exception):
            await broadcaster.publish(session_id, {"type": "processing_complete"})
        with contextlib.suppress(Exception):
            await redis_client.delete(f"meho:active:{session_id}")


async def _update_event_status(
    db: Any,
    registration_id: str,
    payload_hash: str,
    status: str,
    error_message: str,
) -> None:
    """Update the event history record status and error message.

    Used when the background task fails (template error, agent error, etc.)
    to mark the event as "failed" with the error details.

    Args:
        db: AsyncSession.
        registration_id: Event registration UUID.
        payload_hash: SHA-256 hash to identify the event.
        status: New status (typically "failed").
        error_message: Error details.
    """
    from sqlalchemy import update

    from meho_app.modules.connectors.models import EventHistoryModel

    stmt = (
        update(EventHistoryModel)
        .where(EventHistoryModel.event_registration_id == registration_id)
        .where(EventHistoryModel.payload_hash == payload_hash)
        .where(EventHistoryModel.status == "processed")
        .values(status=status, error_message=error_message[:500])
    )
    await db.execute(stmt)
    await db.commit()
    logger.info(f"Event history {registration_id}: status updated to '{status}'")


async def _update_event_session(
    db: Any,
    registration_id: str,
    payload_hash: str,
    session_id: str,
) -> None:
    """Update the event history record with the created session ID.

    The event was logged as "processed" in the endpoint without a session_id.
    After session creation in the background task, we update the record.

    Args:
        db: AsyncSession.
        registration_id: Event registration UUID.
        payload_hash: SHA-256 hash to identify the event.
        session_id: UUID of the created session.
    """
    from uuid import UUID

    from sqlalchemy import update

    from meho_app.modules.connectors.models import EventHistoryModel

    stmt = (
        update(EventHistoryModel)
        .where(EventHistoryModel.event_registration_id == registration_id)
        .where(EventHistoryModel.payload_hash == payload_hash)
        .where(EventHistoryModel.status == "processed")
        .values(session_id=UUID(session_id))
    )
    await db.execute(stmt)
    await db.commit()
    logger.info(f"Event history {registration_id}: session_id updated to {session_id[:8]}...")
