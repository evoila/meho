# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Request and response shapes of the session store API."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from meho_backplane.session_store.models import AgentSessionRecord, AgentSessionUpload

__all__ = [
    "SessionDetail",
    "SessionFileState",
    "SessionListResponse",
    "SessionRetention",
    "SessionRetentionUpdate",
    "SessionState",
    "SessionSummary",
    "SessionUploadFile",
    "SessionUploadPart",
    "SessionUploadResult",
    "summary_of",
    "upload_part_of",
]

SessionState = Literal["open", "ended", "unfinished"]
UploadOutcome = Literal["created", "appended", "state_changed", "unchanged"]


class SessionFileState(BaseModel):
    """The size and the SHA-256 hash of one uploaded file (before the secret check)."""

    size: int
    sha256: str


class SessionSummary(BaseModel):
    """The index row of one session."""

    session_id: str
    project: str
    app: str
    app_version: str | None
    uploaded_by_sub: str
    started_at: datetime | None
    ended_at: datetime | None
    state: SessionState
    outcome: str | None
    created_at: datetime
    last_seen_at: datetime = Field(description="When MEHO last got an upload for this session.")
    upload_count: int


class SessionUploadFile(SessionFileState):
    """One file of one upload: where its stored tail starts and how many bytes it adds.

    ``size`` and ``sha256`` are those of the whole file after this upload.
    """

    offset: int
    added: int


class SessionUploadPart(BaseModel):
    """One stored upload of a session."""

    seq: int
    uploaded_at: datetime
    archive_bytes: int
    archive_sha256: str
    quarantined: bool = Field(description="Only an admin can open a quarantined upload.")
    redactions: int = Field(description="How many known secrets the check replaced.")
    files: dict[str, SessionUploadFile]


class SessionDetail(SessionSummary):
    """One session with its file hashes and its uploads."""

    files: dict[str, SessionFileState]
    uploads: list[SessionUploadPart]


class SessionListResponse(BaseModel):
    """A page of sessions, newest upload first."""

    items: list[SessionSummary]
    next_cursor: str | None


class SessionUploadResult(BaseModel):
    """What one upload did."""

    session_id: str
    state: SessionState
    outcome: UploadOutcome = Field(
        description="created: a new session. appended: new bytes were added. "
        "state_changed: no new bytes, but the state changed. unchanged: nothing changed."
    )
    seq: int | None = Field(description="The number of the stored upload, or null when none.")
    new_bytes: int
    quarantined: bool
    redactions: int


class SessionRetention(BaseModel):
    """The retention period of the caller's tenant."""

    retention_days: int
    source: Literal["tenant", "default"]


class SessionRetentionUpdate(BaseModel):
    """Set the tenant's retention period, or ``null`` to use the default."""

    model_config = ConfigDict(extra="forbid")

    retention_days: int | None = Field(ge=1, le=3650)


def summary_of(record: AgentSessionRecord) -> SessionSummary:
    """Build the summary of *record*."""
    return SessionSummary(
        session_id=record.session_id,
        project=record.project,
        app=record.app,
        app_version=record.app_version,
        uploaded_by_sub=record.uploaded_by_sub,
        started_at=record.started_at,
        ended_at=record.ended_at,
        state=record.state,  # type: ignore[arg-type]
        outcome=record.outcome,
        created_at=record.created_at,
        last_seen_at=record.last_seen_at,
        upload_count=record.upload_count,
    )


def upload_part_of(upload: AgentSessionUpload) -> SessionUploadPart:
    """Build the API shape of one stored upload."""
    return SessionUploadPart(
        seq=upload.seq,
        uploaded_at=upload.uploaded_at,
        archive_bytes=upload.archive_bytes,
        archive_sha256=upload.archive_sha256,
        quarantined=upload.quarantined,
        redactions=upload.redactions,
        files={path: SessionUploadFile(**part) for path, part in upload.files.items()},
    )
