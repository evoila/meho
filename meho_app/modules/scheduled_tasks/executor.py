# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Scheduled task executor -- session creation and agent launch.

Simplified version of ``meho_app.modules.connectors.event_executor.py``.
When APScheduler fires a cron job, it calls ``execute_scheduled_task(task_id)``.
This function:
1. Creates its own DB session (NOT request-scoped -- pitfall #3)
2. Reads the task config from ScheduledTaskModel
3. Logs a run record (ScheduledTaskRunModel, status=running)
4. Generates an LLM session title from the prompt
5. Creates a group session (visibility=tenant, trigger_source=task.name)
6. Saves the prompt as the first user message
7. Launches OrchestratorAgent investigation
8. Updates run record with result (success/failed, session_id, duration)

CRITICAL: The import path ``meho_app.modules.scheduled_tasks.executor:execute_scheduled_task``
MUST remain stable. APScheduler pickles the function reference in SQLAlchemyJobStore.
Renaming or moving this function will break all persisted jobs (pitfall #4).
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from croniter import croniter

from meho_app.core.otel import get_logger

logger = get_logger(__name__)


async def generate_scheduled_task_title(
    task_name: str,
    prompt: str,
) -> str:
    """Generate a concise session title from the task prompt via LLM.

    Uses Sonnet 4.6 (fast/cheap) with a 10-second timeout. On any failure
    (timeout, API error, unexpected output), falls back to a static title.
    Title generation must NEVER block investigation.

    Args:
        task_name: Name of the scheduled task (used in fallback title).
        prompt: The task prompt text.

    Returns:
        Session title string (max ~100 chars).
    """
    fallback_title = f"Scheduled: {task_name}"

    try:
        from pydantic_ai import Agent

        agent = Agent(
            "anthropic:claude-sonnet-4-6",
            system_prompt=(
                "Generate a concise session title (max 80 chars) from this scheduled "
                "task prompt. Focus on what the task does: the system, check type, and scope. "
                "Examples: 'Daily health check on prod Kubernetes cluster', "
                "'Weekly Jira sprint burndown analysis', 'Hourly API latency review'. "
                "Return ONLY the title, no quotes or explanation."
            ),
        )

        # Truncate large prompts to avoid token waste
        prompt_text = prompt[:2000]

        result = await asyncio.wait_for(
            agent.run(prompt_text),
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


async def execute_scheduled_task(task_id: str) -> None:
    """Execute a scheduled task -- create session and launch agent.

    This is the APScheduler job callback. It creates its own DB session
    (NOT request-scoped) and runs the full investigation pipeline:
    static prompt -> LLM title -> create session -> launch agent.

    Args:
        task_id: UUID string of the scheduled task to execute.
    """
    from sqlalchemy import select, update

    from meho_app.database import get_session_maker
    from meho_app.modules.scheduled_tasks.models import (
        ScheduledTaskModel,
        ScheduledTaskRunModel,
    )

    logger.info(f"Scheduled task executor started: task_id={task_id}")

    session_maker = get_session_maker()
    async with session_maker() as db:
        try:
            # ---- Step 1: Read task from DB ----
            stmt = select(ScheduledTaskModel).where(ScheduledTaskModel.id == UUID(task_id))
            result = await db.execute(stmt)
            task = result.scalar_one_or_none()

            if task is None:
                logger.warning(f"Scheduled task {task_id}: not found in DB, skipping")
                return

            if not task.is_enabled:
                logger.warning(f"Scheduled task {task_id}: disabled, skipping")
                return

            # ---- Step 2: Create run record ----
            started_at = datetime.now(UTC)
            run = ScheduledTaskRunModel(
                task_id=task.id,
                tenant_id=task.tenant_id,
                status="running",
                prompt_snapshot=task.prompt,
                started_at=started_at,
            )
            db.add(run)
            await db.flush()
            await db.refresh(run)
            run_id = str(run.id)

            logger.info(f"Scheduled task {task_id}: run {run_id} created (tenant={task.tenant_id})")

            # ---- Step 3: Generate session title ----
            title = await generate_scheduled_task_title(task.name, task.prompt)
            logger.info(f"Scheduled task {task_id}: session title = '{title}'")

            # ---- Step 4: Create group session ----
            from meho_app.modules.agents.service import AgentService

            agent_service = AgentService(db)
            session = await agent_service.create_chat_session(
                tenant_id=task.tenant_id,
                user_id="system:scheduler",
                title=title,
                visibility="tenant",
                created_by_name=task.name,
                trigger_source=task.name,
            )
            session_id = str(session.id)
            logger.info(
                f"Scheduled task {task_id}: session created "
                f"(id={session_id[:8]}..., visibility=tenant, "
                f"trigger_source={task.name})"
            )

            # ---- Step 5: Save prompt as first user message ----
            await agent_service.add_chat_message(
                session_id=session_id,
                role="user",
                content=task.prompt,
                sender_id="system:scheduler",
                sender_name=task.name,
            )

            # ---- Step 6: Launch agent investigation ----
            await _run_agent_investigation(
                db=db,
                session_id=session_id,
                tenant_id=task.tenant_id,
                task_name=task.name,
                rendered_prompt=task.prompt,
                agent_service=agent_service,
                # Phase 74: automation context
                created_by_user_id=task.created_by_user_id if task.delegate_credentials else None,
                allowed_connector_ids=task.allowed_connector_ids,
                delegation_active=task.delegation_active,
                task_id_str=str(task.id),
                # Phase 75: notification targets
                notification_targets=task.notification_targets,
            )

            # ---- Step 7: Update run record (success) ----
            completed_at = datetime.now(UTC)
            duration = int((completed_at - started_at).total_seconds())

            # Compute next_run_at from croniter
            try:
                tz = ZoneInfo(task.timezone)
                next_run = croniter(task.cron_expression, datetime.now(tz)).get_next(datetime)
            except Exception:
                next_run = None

            await db.execute(
                update(ScheduledTaskRunModel)
                .where(ScheduledTaskRunModel.id == run.id)
                .values(
                    status="success",
                    session_id=UUID(session_id),
                    completed_at=completed_at,
                    duration_seconds=duration,
                )
            )
            await db.execute(
                update(ScheduledTaskModel)
                .where(ScheduledTaskModel.id == task.id)
                .values(
                    last_run_at=completed_at,
                    last_run_status="success",
                    total_runs=ScheduledTaskModel.total_runs + 1,
                    next_run_at=next_run,
                )
            )
            await db.commit()

            logger.info(
                f"Scheduled task {task_id}: run {run_id} completed "
                f"(duration={duration}s, session={session_id[:8]}...)"
            )

        except Exception as e:
            logger.error(
                f"Scheduled task {task_id}: executor failed -- {e}",
                exc_info=True,
            )
            try:
                completed_at = datetime.now(UTC)
                duration = (
                    int(
                        (completed_at - started_at).total_seconds()  # type: ignore[possibly-undefined]
                    )
                    if "started_at" in dir()
                    else None
                )

                # Update run record as failed
                if "run" in dir() and run is not None:
                    await db.execute(
                        update(ScheduledTaskRunModel)
                        .where(ScheduledTaskRunModel.id == run.id)
                        .values(
                            status="failed",
                            error_message=str(e)[:500],
                            completed_at=completed_at,
                            duration_seconds=duration,
                        )
                    )

                # Update task with failure status
                if "task" in dir() and task is not None:
                    await db.execute(
                        update(ScheduledTaskModel)
                        .where(ScheduledTaskModel.id == task.id)
                        .values(
                            last_run_at=completed_at,
                            last_run_status="failed",
                            total_runs=ScheduledTaskModel.total_runs + 1,
                        )
                    )

                await db.commit()
            except Exception as update_err:
                logger.error(
                    f"Scheduled task {task_id}: failed to update run status -- {update_err}"
                )


async def _scheduler_delegation_flag_callback(
    trigger_type: str, trigger_id: str, is_active: bool
) -> None:
    """Write delegation_active flag back to ScheduledTaskModel."""
    from meho_app.database import get_session_maker

    session_maker = get_session_maker()
    async with session_maker() as session:
        from sqlalchemy import update

        from meho_app.modules.scheduled_tasks.models import ScheduledTaskModel

        stmt = update(ScheduledTaskModel).where(
            ScheduledTaskModel.id == trigger_id
        ).values(delegation_active=is_active)
        await session.execute(stmt)
        await session.commit()


async def _run_agent_investigation(
    db: Any,
    session_id: str,
    tenant_id: str,
    task_name: str,
    rendered_prompt: str,
    agent_service: Any,
    # Phase 74: automation identity context
    created_by_user_id: str | None = None,
    allowed_connector_ids: list[str] | None = None,
    delegation_active: bool = True,
    task_id_str: str | None = None,
    # Phase 75: notification targets for approval alerts
    notification_targets: list[dict[str, str]] | None = None,
) -> None:
    """Run the OrchestratorAgent investigation and persist results.

    Replicates the core execution path from ``event_executor.py``'s
    ``_run_agent_investigation`` with ``system:scheduler`` user context
    instead of ``system:event``.

    Args:
        db: AsyncSession for database operations.
        session_id: UUID of the created session.
        tenant_id: Tenant identifier.
        task_name: Task name for logging and user context.
        rendered_prompt: The static task prompt.
        agent_service: AgentService instance for message persistence.
        created_by_user_id: JWT user_id of task creator (Phase 74).
        allowed_connector_ids: Connector scope (Phase 74).
        delegation_active: Current delegation_active flag (Phase 74).
        task_id_str: UUID string of the scheduled task (Phase 74).
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

    # Create a synthetic system user context for the scheduler
    system_user = UserContext(
        user_id="system:scheduler",
        name=task_name,
        tenant_id=tenant_id,
        roles=["user"],
    )

    # Create agent dependencies (same pattern as chat_stream)
    dependencies = await create_agent_dependencies(
        user=system_user,
        session=db,
        current_question=rendered_prompt,
        # Phase 74: automation context
        session_type="automated_scheduler",
        created_by_user_id=created_by_user_id,
        allowed_connector_ids=allowed_connector_ids,
        trigger_type="scheduler",
        trigger_id=task_id_str,
        delegation_active=delegation_active,
        delegation_flag_callback=_scheduler_delegation_flag_callback,
        # Phase 75: notification targets
        notification_targets=notification_targets,
    )

    # Initialize UnifiedExecutor with Redis
    config = get_api_config()
    redis_client = await get_redis_client(config.redis_url)
    get_unified_executor(redis_client=redis_client)

    # Create Redis SSE broadcaster for live viewers
    broadcaster = RedisSSEBroadcaster(redis_client)

    # Create state store for multi-turn persistence
    state_store = await create_agent_state_store()

    # Create OrchestratorAgent
    agent = OrchestratorAgent(dependencies=dependencies)

    # Track final answer for persistence
    final_answer_content = None

    try:
        # Broadcast processing_started
        await broadcaster.publish(
            session_id,
            {"type": "processing_started", "sender_id": "system:scheduler"},
        )

        # Set SETNX processing guard
        await redis_client.set(
            f"meho:active:{session_id}",
            "system:scheduler",
            nx=True,
            ex=300,
        )

        # Stream events from the orchestrator
        event_stream = run_orchestrator_streaming(
            agent=agent,
            user_message=rendered_prompt,
            session_id=session_id,
            conversation_history=[],  # No prior history for scheduled sessions
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
                logger.info(f"Scheduled session {session_id[:8]}...: final answer ready")

        # Persist assistant response
        if final_answer_content:
            await agent_service.add_chat_message(
                session_id=session_id,
                role="assistant",
                content=final_answer_content,
            )
            logger.info(f"Scheduled session {session_id[:8]}...: assistant message persisted")

    except Exception as e:
        logger.error(
            f"Scheduled session {session_id[:8]}...: agent investigation failed -- {e}",
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
