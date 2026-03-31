# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
CRUD service for scheduled tasks.

Provides create, read, update, delete, toggle, run-now, and run-history
operations for scheduled tasks. All APScheduler job registration/removal
happens through this service (not scattered across endpoints).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_app.core.otel import get_logger
from meho_app.modules.scheduled_tasks.models import (
    ScheduledTaskModel,
    ScheduledTaskRunModel,
)

logger = get_logger(__name__)


class ScheduledTaskService:
    """Service for managing scheduled tasks.

    Uses a request-scoped ``AsyncSession`` from FastAPI ``Depends`` for
    all database operations. APScheduler job registration is done via
    the scheduler singleton (``get_scheduler()``).

    Args:
        db: Request-scoped async database session.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_tasks(self, tenant_id: str) -> list[ScheduledTaskModel]:
        """List all scheduled tasks for a tenant.

        For enabled tasks with a stale or missing ``next_run_at``, recomputes
        the next run time from the cron expression.

        Args:
            tenant_id: Tenant identifier.

        Returns:
            List of tasks ordered by created_at descending.
        """
        stmt = (
            select(ScheduledTaskModel)
            .where(ScheduledTaskModel.tenant_id == tenant_id)
            .order_by(ScheduledTaskModel.created_at.desc())
        )
        result = await self.db.execute(stmt)
        tasks = list(result.scalars().all())

        # Recompute stale next_run_at for enabled tasks
        now_utc = datetime.now(tz=ZoneInfo("UTC"))
        for task in tasks:
            if task.is_enabled and (task.next_run_at is None or task.next_run_at < now_utc):
                try:
                    tz = ZoneInfo(task.timezone)
                    now_tz = datetime.now(tz)
                    next_run = croniter(task.cron_expression, now_tz).get_next(datetime)
                    task.next_run_at = next_run
                except Exception:  # noqa: S110 -- intentional silent exception handling
                    pass  # Leave next_run_at as-is on error

        return tasks

    async def create_task(
        self,
        tenant_id: str,
        user_id: str,
        name: str,
        description: str | None,
        cron_expression: str,
        timezone_name: str,
        prompt: str,
        allowed_connector_ids: list[str] | None = None,
        delegate_credentials: bool = False,
        notification_targets: list[dict[str, str]] | None = None,
    ) -> ScheduledTaskModel:
        """Create a new scheduled task.

        Validates the cron expression and timezone, computes the initial
        ``next_run_at``, creates the database record, and registers the
        APScheduler cron job.

        Args:
            tenant_id: Tenant identifier.
            user_id: User creating the task.
            name: Human-readable task name.
            description: Optional task description.
            cron_expression: Standard 5-field cron expression.
            timezone_name: IANA timezone name (e.g., ``Europe/Sarajevo``).
            prompt: Static prompt text for the session.

        Returns:
            The created task model.

        Raises:
            ValueError: If cron expression or timezone is invalid.
        """
        # Validate cron expression
        if not croniter.is_valid(cron_expression):
            raise ValueError(f"Invalid cron expression: {cron_expression}")

        # Validate timezone
        try:
            tz = ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, KeyError):
            raise ValueError(f"Invalid timezone: {timezone_name}") from None

        # Compute initial next_run_at
        now_tz = datetime.now(tz)
        next_run = croniter(cron_expression, now_tz).get_next(datetime)

        task = ScheduledTaskModel(
            tenant_id=tenant_id,
            name=name,
            description=description,
            cron_expression=cron_expression,
            timezone=timezone_name,
            prompt=prompt,
            is_enabled=True,
            next_run_at=next_run,
            created_by_user_id=user_id,
            allowed_connector_ids=allowed_connector_ids,
            delegate_credentials=delegate_credentials,
            notification_targets=notification_targets,
        )
        self.db.add(task)
        await self.db.flush()
        await self.db.refresh(task)

        # Register with APScheduler
        try:
            from meho_app.modules.scheduled_tasks.scheduler import (
                get_scheduler,
                register_task_job,
            )

            scheduler = get_scheduler()
            register_task_job(scheduler, str(task.id), cron_expression, timezone_name)
        except RuntimeError:
            # Scheduler not yet initialized (e.g., during tests or migration)
            logger.warning(
                f"Scheduler not initialized, skipping job registration for task {task.id}"
            )

        logger.info(
            f"Created scheduled task: id={task.id}, name={name}, "
            f"cron={cron_expression}, tz={timezone_name}"
        )
        return task

    async def get_task(self, tenant_id: str, task_id: str) -> ScheduledTaskModel | None:
        """Get a scheduled task by ID, verifying tenant ownership.

        Args:
            tenant_id: Tenant identifier.
            task_id: UUID string of the task.

        Returns:
            The task model, or None if not found or tenant mismatch.
        """
        stmt = select(ScheduledTaskModel).where(
            ScheduledTaskModel.id == UUID(task_id),
            ScheduledTaskModel.tenant_id == tenant_id,
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def update_task(
        self,
        tenant_id: str,
        task_id: str,
        **fields: object,
    ) -> ScheduledTaskModel | None:
        """Update a scheduled task's fields.

        Allowed fields: ``name``, ``description``, ``cron_expression``,
        ``timezone``, ``prompt``, ``is_enabled``. If the cron expression
        or timezone changes, the APScheduler job is re-registered. If
        ``is_enabled`` changes, the job is registered or removed.

        Args:
            tenant_id: Tenant identifier.
            task_id: UUID string of the task.
            **fields: Fields to update.

        Returns:
            The updated task model, or None if not found.

        Raises:
            ValueError: If cron expression or timezone is invalid.
        """
        task = await self.get_task(tenant_id, task_id)
        if task is None:
            return None

        allowed_fields = {
            "name",
            "description",
            "cron_expression",
            "timezone",
            "prompt",
            "is_enabled",
            "notification_targets",
        }
        update_values = {k: v for k, v in fields.items() if k in allowed_fields}

        if not update_values:
            return task

        # Validate new cron expression if changed
        new_cron = update_values.get("cron_expression", task.cron_expression)
        new_tz_name = update_values.get("timezone", task.timezone)

        if "cron_expression" in update_values and not croniter.is_valid(str(new_cron)):
            raise ValueError(f"Invalid cron expression: {new_cron}")

        if "timezone" in update_values:
            try:
                ZoneInfo(str(new_tz_name))
            except (ZoneInfoNotFoundError, KeyError):
                raise ValueError(f"Invalid timezone: {new_tz_name}") from None

        # Track schedule/state changes for APScheduler
        schedule_changed = "cron_expression" in update_values or "timezone" in update_values
        enabled_changed = "is_enabled" in update_values
        new_enabled = update_values.get("is_enabled", task.is_enabled)

        # Recompute next_run_at if schedule changed
        if schedule_changed and new_enabled:
            try:
                tz = ZoneInfo(str(new_tz_name))
                now_tz = datetime.now(tz)
                next_run = croniter(str(new_cron), now_tz).get_next(datetime)
                update_values["next_run_at"] = next_run
            except Exception:  # noqa: S110 -- intentional silent exception handling
                pass

        # Apply updates
        for key, value in update_values.items():
            setattr(task, key, value)
        await self.db.flush()
        await self.db.refresh(task)

        # Update APScheduler job
        try:
            from meho_app.modules.scheduled_tasks.scheduler import (
                get_scheduler,
                register_task_job,
                remove_task_job,
            )

            scheduler = get_scheduler()

            if enabled_changed and not new_enabled:
                # Disabled -- remove job
                remove_task_job(scheduler, str(task.id))
                task.next_run_at = None
            elif enabled_changed and new_enabled:
                # Enabled -- register job
                register_task_job(
                    scheduler,
                    str(task.id),
                    str(task.cron_expression),
                    str(task.timezone),
                )
            elif schedule_changed and new_enabled:
                # Schedule changed while enabled -- re-register
                register_task_job(
                    scheduler,
                    str(task.id),
                    str(task.cron_expression),
                    str(task.timezone),
                )
        except RuntimeError:
            logger.warning(f"Scheduler not initialized, skipping job update for task {task.id}")

        logger.info(f"Updated scheduled task: id={task.id}, fields={list(update_values.keys())}")
        return task

    async def delete_task(self, tenant_id: str, task_id: str) -> bool:
        """Delete a scheduled task and its APScheduler job.

        Args:
            tenant_id: Tenant identifier.
            task_id: UUID string of the task.

        Returns:
            True if deleted, False if not found.
        """
        task = await self.get_task(tenant_id, task_id)
        if task is None:
            return False

        # Remove from APScheduler
        try:
            from meho_app.modules.scheduled_tasks.scheduler import (
                get_scheduler,
                remove_task_job,
            )

            scheduler = get_scheduler()
            remove_task_job(scheduler, task_id)
        except RuntimeError:
            pass

        # Delete from DB (cascade deletes runs)
        await self.db.execute(
            delete(ScheduledTaskModel).where(
                ScheduledTaskModel.id == UUID(task_id),
                ScheduledTaskModel.tenant_id == tenant_id,
            )
        )

        logger.info(f"Deleted scheduled task: id={task_id}")
        return True

    async def toggle_task(self, tenant_id: str, task_id: str) -> ScheduledTaskModel | None:
        """Toggle a scheduled task's enabled state.

        Registers or removes the APScheduler job accordingly. When enabling,
        recomputes ``next_run_at``.

        Args:
            tenant_id: Tenant identifier.
            task_id: UUID string of the task.

        Returns:
            The updated task, or None if not found.
        """
        task = await self.get_task(tenant_id, task_id)
        if task is None:
            return None

        new_enabled = not task.is_enabled
        return await self.update_task(tenant_id, task_id, is_enabled=new_enabled)

    async def get_runs(
        self,
        tenant_id: str,
        task_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ScheduledTaskRunModel]:
        """Get run history for a scheduled task.

        Args:
            tenant_id: Tenant identifier.
            task_id: UUID string of the task.
            limit: Maximum number of runs to return (default 50).
            offset: Number of runs to skip (for pagination).

        Returns:
            List of runs ordered by started_at descending.
        """
        stmt = (
            select(ScheduledTaskRunModel)
            .where(
                ScheduledTaskRunModel.task_id == UUID(task_id),
                ScheduledTaskRunModel.tenant_id == tenant_id,
            )
            .order_by(ScheduledTaskRunModel.started_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def run_now(self, tenant_id: str, task_id: str) -> ScheduledTaskRunModel | None:
        """Trigger immediate execution of a scheduled task.

        Creates a run record with status ``running`` and fires the executor
        as a fire-and-forget async task. Does NOT go through APScheduler --
        the execution is direct.

        Args:
            tenant_id: Tenant identifier.
            task_id: UUID string of the task.

        Returns:
            The created run record (status=running), or None if task not found.
        """
        task = await self.get_task(tenant_id, task_id)
        if task is None:
            return None

        # Create a placeholder run record so the caller gets immediate feedback

        run = ScheduledTaskRunModel(
            task_id=task.id,
            tenant_id=task.tenant_id,
            status="running",
            prompt_snapshot=task.prompt,
            started_at=datetime.now(UTC),
        )
        self.db.add(run)
        await self.db.flush()
        await self.db.refresh(run)

        logger.info(f"Run-now triggered for task {task_id}: run_id={run.id}")

        # Fire-and-forget: launch executor in background
        from meho_app.modules.scheduled_tasks.executor import (
            execute_scheduled_task,
        )

        asyncio.create_task(execute_scheduled_task(task_id))  # noqa: RUF006 -- fire-and-forget task pattern

        return run
