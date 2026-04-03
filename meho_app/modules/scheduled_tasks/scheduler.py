# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
APScheduler lifecycle management for scheduled tasks.

Manages an ``AsyncIOScheduler`` singleton with ``SQLAlchemyJobStore``
(PostgreSQL-backed) for restart-safe job persistence. Provides functions
to register/remove cron jobs and sync APScheduler state with the
ScheduledTaskModel on startup.

Key design decisions:
- SQLAlchemyJobStore uses a SYNCHRONOUS PostgreSQL URL (psycopg2).
  APScheduler 3.x's job store is synchronous internally.
- Job IDs follow the pattern ``scheduled_task:{task_id}`` for deterministic
  identification and ``replace_existing=True`` to prevent duplicates.
- Jobs only store the ``task_id``; the prompt is read from the DB at
  execution time (per research anti-pattern guidance).
"""

from __future__ import annotations

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from meho_app.core.otel import get_logger

logger = get_logger(__name__)

# Module-level singleton
_scheduler: AsyncIOScheduler | None = None


def get_sync_db_url(async_url: str) -> str:
    """Convert an async PostgreSQL URL to a synchronous one.

    APScheduler 3.x ``SQLAlchemyJobStore`` calls ``sqlalchemy.create_engine()``
    (sync), not ``create_async_engine()``. We strip the ``+asyncpg`` dialect
    suffix so it uses the default sync driver (psycopg2).

    Example:
        ``postgresql+asyncpg://user:pass@host/db`` -> ``postgresql://user:pass@host/db``
    """
    return async_url.replace("+asyncpg", "")


def create_scheduler(database_url: str) -> AsyncIOScheduler:
    """Create an APScheduler instance with PostgreSQL persistence.

    Args:
        database_url: The async database URL (will be converted to sync).

    Returns:
        Configured ``AsyncIOScheduler`` (not yet started).
    """
    global _scheduler

    sync_url = get_sync_db_url(database_url)

    jobstores = {
        "default": SQLAlchemyJobStore(
            url=sync_url,
            tablename="apscheduler_jobs",
        ),
    }
    job_defaults = {
        "coalesce": True,  # If multiple runs were missed, run only once
        "max_instances": 1,  # Only one instance of each job at a time
        "misfire_grace_time": 3600,  # 1 hour grace for missed jobs
    }

    scheduler = AsyncIOScheduler(
        jobstores=jobstores,
        job_defaults=job_defaults,
    )

    _scheduler = scheduler
    return scheduler


def get_scheduler() -> AsyncIOScheduler:
    """Get the singleton scheduler instance.

    Raises:
        RuntimeError: If the scheduler has not been initialized via
            ``create_scheduler()``.
    """
    if _scheduler is None:
        raise RuntimeError("Scheduler not initialized -- call create_scheduler() first")
    return _scheduler


def register_task_job(
    scheduler: AsyncIOScheduler,
    task_id: str,
    cron_expression: str,
    timezone: str,
) -> None:
    """Register or update an APScheduler cron job for a scheduled task.

    Uses ``replace_existing=True`` with a deterministic job ID to prevent
    duplicate jobs on restart (pitfall #2). Only passes ``task_id`` as an
    argument -- the prompt is read from the DB at execution time (anti-pattern
    avoidance: don't store prompt in APScheduler's pickled job store).

    Args:
        scheduler: The APScheduler instance.
        task_id: UUID string of the scheduled task.
        cron_expression: Standard 5-field cron expression.
        timezone: IANA timezone name (e.g., ``Europe/Sarajevo``).
    """
    from meho_app.modules.scheduled_tasks.executor import execute_scheduled_task

    trigger = CronTrigger.from_crontab(cron_expression, timezone=timezone)
    job_id = f"scheduled_task:{task_id}"

    scheduler.add_job(
        execute_scheduled_task,
        trigger=trigger,
        id=job_id,
        args=[task_id],
        replace_existing=True,
        name=f"Scheduled task {task_id}",
    )
    logger.info(f"Registered APScheduler job: {job_id} (cron={cron_expression}, tz={timezone})")


def remove_task_job(scheduler: AsyncIOScheduler, task_id: str) -> None:
    """Remove an APScheduler job for a scheduled task.

    Silently ignores ``JobLookupError`` if the job doesn't exist (idempotent).

    Args:
        scheduler: The APScheduler instance.
        task_id: UUID string of the scheduled task.
    """
    from apscheduler.jobstores.base import JobLookupError

    job_id = f"scheduled_task:{task_id}"
    try:
        scheduler.remove_job(job_id)
        logger.info(f"Removed APScheduler job: {job_id}")
    except JobLookupError:
        logger.debug(f"Job {job_id} not found in scheduler (already removed)")


async def sync_scheduler_with_db(
    scheduler: AsyncIOScheduler,
) -> None:  # NOSONAR (cognitive complexity)
    """Sync APScheduler jobs with the ScheduledTaskModel state.

    On startup, APScheduler restores jobs from ``SQLAlchemyJobStore``. This
    function ensures those jobs match the current ``ScheduledTaskModel`` state:

    1. Read all enabled tasks from the database
    2. Register a job for each (``replace_existing=True``)
    3. Remove any APScheduler jobs (prefixed ``scheduled_task:``) that don't
       match an enabled task
    4. Update ``next_run_at`` on each task from the APScheduler job's
       ``next_run_time``

    Uses its own DB session (not request-scoped).
    """
    from sqlalchemy import select, update

    from meho_app.database import get_session_maker
    from meho_app.modules.scheduled_tasks.models import ScheduledTaskModel

    session_maker = get_session_maker()
    async with session_maker() as db:
        # Step 1: Get all enabled tasks
        stmt = select(ScheduledTaskModel).where(ScheduledTaskModel.is_enabled.is_(True))
        result = await db.execute(stmt)
        enabled_tasks = result.scalars().all()

        enabled_task_ids = set()

        # Step 2: Register job for each enabled task
        for task in enabled_tasks:
            task_id_str = str(task.id)
            enabled_task_ids.add(task_id_str)
            try:
                register_task_job(
                    scheduler,
                    task_id_str,
                    task.cron_expression,  # type: ignore[arg-type]  # SQLAlchemy ORM attribute access
                    task.timezone,  # type: ignore[arg-type]  # SQLAlchemy ORM attribute access
                )
            except Exception as e:
                logger.error(f"Failed to register job for task {task_id_str}: {e}")
                continue

            # Step 4: Update next_run_at from APScheduler
            job = scheduler.get_job(f"scheduled_task:{task_id_str}")
            if job and job.next_run_time:
                await db.execute(
                    update(ScheduledTaskModel)
                    .where(ScheduledTaskModel.id == task.id)
                    .values(next_run_at=job.next_run_time)
                )

        # Step 3: Remove orphaned APScheduler jobs
        all_jobs = scheduler.get_jobs()
        for job in all_jobs:
            if job.id.startswith("scheduled_task:"):
                task_id_from_job = job.id.replace("scheduled_task:", "")
                if task_id_from_job not in enabled_task_ids:
                    try:
                        scheduler.remove_job(job.id)
                        logger.info(f"Removed orphaned APScheduler job: {job.id}")
                    except Exception as e:
                        logger.warning(f"Failed to remove orphaned job {job.id}: {e}")

        await db.commit()

        logger.info(
            f"Scheduler sync complete: {len(enabled_tasks)} enabled tasks, "
            f"{len(all_jobs)} total APScheduler jobs"
        )
