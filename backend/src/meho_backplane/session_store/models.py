# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Index tables of the agent session store (migration ``0152``).

* :class:`AgentSessionRecord` -- one row per session and tenant.
  ``files`` maps each file path to ``{"size", "sha256"}`` of the bytes
  uploaded so far, before the secret check. A new upload is compared with
  it to find the new tail of each file.
* :class:`AgentSessionUpload` -- one row per stored upload. ``files`` maps
  each path to ``{"offset", "added", "size", "sha256"}``: where the stored
  tail starts, how many bytes it adds, and the size and hash of the whole
  file after this upload. A retry of an older upload matches one of these
  states and changes nothing.
* :class:`AgentSessionEvent` -- one saved event of a hosted agent run,
  in order (:mod:`.hosted_runs`).
* :class:`AgentSessionRetention` -- the retention period a tenant set.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Final

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from meho_backplane.db.models import Base

__all__ = [
    "SESSION_STATES",
    "AgentSessionEvent",
    "AgentSessionRecord",
    "AgentSessionRetention",
    "AgentSessionUpload",
]

#: The states of a session: it may still grow, an end was seen, or it
#: stopped without an end (for example after a crash).
SESSION_STATES: Final[tuple[str, ...]] = ("open", "ended", "unfinished")

_JSON = JSON().with_variant(JSONB(), "postgresql")


class AgentSessionRecord(Base):
    """One stored agent session of one tenant."""

    __tablename__ = "agent_session_record"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False)
    session_id: Mapped[str] = mapped_column(Text, nullable=False)
    uploaded_by_sub: Mapped[str] = mapped_column(Text, nullable=False)
    project: Mapped[str] = mapped_column(Text, nullable=False)
    app: Mapped[str] = mapped_column(Text, nullable=False)
    app_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[str | None] = mapped_column(Text, nullable=True)
    files: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    upload_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AgentSessionUpload(Base):
    """One stored upload (one archive in the object storage) of a session."""

    __tablename__ = "agent_session_upload"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    record_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("agent_session_record.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    archive_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    archive_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    quarantined: Mapped[bool] = mapped_column(Boolean, nullable=False)
    redactions: Mapped[int] = mapped_column(Integer, nullable=False)
    files: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AgentSessionEvent(Base):
    """One saved event of a hosted agent run (migration ``0162``).

    ``record_id`` is the run's session record; its id is the run id.
    ``seq`` counts from 1 in the order the run made the events. ``data``
    is the event after the secret check.
    """

    __tablename__ = "agent_session_event"

    record_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("agent_session_record.id", ondelete="CASCADE"), primary_key=True
    )
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    quarantined: Mapped[bool] = mapped_column(Boolean, nullable=False)
    redactions: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AgentSessionRetention(Base):
    """The retention period one tenant set for its sessions."""

    __tablename__ = "agent_session_retention"

    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True)
    retention_days: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_by_sub: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
