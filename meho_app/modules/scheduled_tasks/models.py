# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
SQLAlchemy models for scheduled tasks.

Two-table design:
- ScheduledTaskModel: Task definition (what to run, when, for which tenant)
- ScheduledTaskRunModel: Execution log (did it run, what happened, session link)

APScheduler's own ``apscheduler_jobs`` table handles the scheduling state;
these tables handle the domain model and run history.
"""

from __future__ import annotations

import uuid

from sqlalchemy import (
    TIMESTAMP,
    Boolean,
    Column,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from meho_app.database import Base


class ScheduledTaskModel(Base):
    """Scheduled task definition.

    Stores the cron expression, timezone, prompt, and metadata for a
    scheduled task. Each task belongs to a tenant and, when triggered,
    creates a group session with the static prompt.
    """

    __tablename__ = "scheduled_task"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(String, nullable=False, index=True)

    # Task definition
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    cron_expression = Column(String(100), nullable=False)
    timezone = Column(String(50), nullable=False)  # IANA timezone name
    prompt = Column(Text, nullable=False)

    # State
    is_enabled = Column(Boolean, nullable=False, default=True)
    next_run_at = Column(TIMESTAMP(timezone=True), nullable=True)

    # Stats
    total_runs = Column(Integer, nullable=False, default=0)
    last_run_at = Column(TIMESTAMP(timezone=True), nullable=True)
    last_run_status = Column(String(20), nullable=True)  # success, failed, running

    # Identity model -- who created this task and what it can access
    created_by_user_id = Column(String(255), nullable=True, index=True)
    allowed_connector_ids = Column(JSONB, nullable=True, default=None)
    delegate_credentials = Column(Boolean, nullable=False, default=False)
    delegation_active = Column(Boolean, nullable=False, default=True)

    # Phase 75: Notification targets for approval notifications
    notification_targets = Column(
        JSONB, nullable=True, default=None
    )  # [{"connector_id": "uuid", "contact": "email"}]

    # Audit
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    # Relationships
    runs = relationship(
        "ScheduledTaskRunModel",
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="desc(ScheduledTaskRunModel.started_at)",
    )

    __table_args__ = (
        Index("ix_scheduled_task_tenant", "tenant_id"),
        Index("ix_scheduled_task_enabled", "is_enabled"),
    )

    def __repr__(self) -> str:
        return (
            f"<ScheduledTask(id={self.id!r}, name={self.name!r}, "
            f"cron={self.cron_expression!r}, enabled={self.is_enabled})>"
        )


class ScheduledTaskRunModel(Base):
    """Execution log for a scheduled task run.

    Records each execution with status, session link, duration, and a
    snapshot of the prompt at execution time (so edits to the task prompt
    don't retroactively change history).
    """

    __tablename__ = "scheduled_task_run"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id = Column(
        UUID(as_uuid=True),
        ForeignKey("scheduled_task.id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id = Column(String, nullable=False, index=True)

    # Execution
    status = Column(String(20), nullable=False)  # running, success, failed
    session_id = Column(UUID(as_uuid=True), nullable=True)  # Link to ChatSession
    error_message = Column(Text, nullable=True)
    prompt_snapshot = Column(Text, nullable=False)  # Snapshot of prompt at exec time

    # Timing
    started_at = Column(TIMESTAMP(timezone=True), nullable=False)
    completed_at = Column(TIMESTAMP(timezone=True), nullable=True)
    duration_seconds = Column(Integer, nullable=True)

    # Relationships
    task = relationship("ScheduledTaskModel", back_populates="runs")

    __table_args__ = (
        Index("ix_sched_run_task", "task_id"),
        Index("ix_sched_run_started", "started_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<ScheduledTaskRun(id={self.id!r}, task_id={self.task_id!r}, status={self.status!r})>"
        )
