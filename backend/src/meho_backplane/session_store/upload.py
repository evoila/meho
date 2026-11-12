# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Store one uploaded session archive.

The steps of :func:`upload_session`:

1. Load and lock the stored session. An upload of a session that another
   person started is refused (``409``).
2. In a worker process (:mod:`.worker`, :mod:`.prepare`): unpack the
   archive (:mod:`.archive`), compare it with the stored session and find
   the new tail of each file (:mod:`.plan`), check each tail for secrets
   (:mod:`.secret_check`) and pack the checked tails into one archive.
   A tail holds whole lines only. A changed stored part is a conflict
   (``409``).
3. With no new bytes: change only the state (open to ended or
   unfinished), or change nothing at all. A retry of the same bytes is
   safe.
4. With new bytes: write the archive of the checked tails to the object
   storage (under ``quarantine/`` when the check was not sure), then
   commit the index row, the upload row and the audit row in one
   transaction. When the commit fails, the archive is deleted again, so a
   session is stored complete or not at all.

Every upload, also a refused or unchanged one, writes an audit row.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.session_store.audit import add_audit_row, write_audit_row
from meho_backplane.session_store.config import SessionStoreConfig
from meho_backplane.session_store.hosted_runs import HOSTED_RUN_APP
from meho_backplane.session_store.models import AgentSessionRecord, AgentSessionUpload
from meho_backplane.session_store.object_store import ObjectStore
from meho_backplane.session_store.prepare import CheckedArchive, PreparedUpload, prepare_upload
from meho_backplane.session_store.schemas import SessionUploadResult
from meho_backplane.session_store.worker import run_in_worker

__all__ = ["SessionConflictError", "UploadMeta", "object_key", "upload_session"]

_log = structlog.get_logger(__name__)

_OP_ID: Final[str] = "sessions.upload"


class SessionConflictError(Exception):
    """The upload does not fit the stored session. ``reason`` is the API detail."""

    def __init__(self, reason: str, paths: list[str] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.paths = paths or []


@dataclass(frozen=True)
class UploadMeta:
    """What the uploader says about the session."""

    session_id: str
    project: str
    app: str
    app_version: str | None
    started_at: datetime | None
    ended_at: datetime | None
    state: str
    outcome: str | None


def object_key(tenant_id: uuid.UUID, record_id: uuid.UUID, seq: int, *, quarantined: bool) -> str:
    """Return a new object key for one stored upload.

    The random part makes each key unique, so a failed commit deletes only
    its own archive, never the archive of another upload with the same seq.
    """
    key = f"tenants/{tenant_id}/sessions/{record_id}/{seq:06d}-{uuid.uuid4().hex}.tar.gz"
    return f"quarantine/{key}" if quarantined else key


@dataclass(frozen=True)
class _Stored:
    """The checked archive of one upload, already in the object storage."""

    seq: int
    key: str
    sha256: str
    size: int
    quarantined: bool
    redactions: int
    files: dict[str, Any]


def _next_state(current: str, requested: str, *, new_bytes: bool) -> str:
    """New bytes take the uploader's state. Without them only an end is recorded."""
    if new_bytes:
        return requested
    if current == "open" and requested in ("ended", "unfinished"):
        return requested
    return current


async def _store_archive(
    call: _Call, record_id: uuid.UUID, seq: int, archive: CheckedArchive
) -> _Stored:
    key = object_key(call.operator.tenant_id, record_id, seq, quarantined=archive.quarantined)
    await call.store.put(key, archive.data)
    return _Stored(
        seq,
        key,
        archive.sha256,
        len(archive.data),
        archive.quarantined,
        archive.redactions,
        archive.files,
    )


def _apply_meta(record: AgentSessionRecord, meta: UploadMeta) -> None:
    record.project = meta.project
    record.app = meta.app
    if meta.app_version is not None:
        record.app_version = meta.app_version
    if meta.started_at is not None:
        record.started_at = meta.started_at
    if meta.ended_at is not None:
        record.ended_at = meta.ended_at
    if meta.outcome is not None:
        record.outcome = meta.outcome


def _new_record(operator: Operator, meta: UploadMeta, now: datetime) -> AgentSessionRecord:
    return AgentSessionRecord(
        id=uuid.uuid4(),
        tenant_id=operator.tenant_id,
        session_id=meta.session_id,
        uploaded_by_sub=operator.sub,
        project=meta.project,
        app=meta.app,
        state="open",
        files={},
        upload_count=0,
        created_at=now,
        last_seen_at=now,
    )


@dataclass(frozen=True)
class _Call:
    """One upload request: who, what, where it came in, and the storage."""

    operator: Operator
    meta: UploadMeta
    path: str
    store: ObjectStore

    def audit(self, session: AsyncSession, payload: dict[str, Any]) -> None:
        add_audit_row(
            session,
            self.operator,
            op_id=_OP_ID,
            method="POST",
            path=self.path,
            payload={"session_id": self.meta.session_id, **payload},
        )

    async def audit_refusal(self, exc: SessionConflictError) -> None:
        await write_audit_row(
            self.operator,
            op_id=_OP_ID,
            method="POST",
            path=self.path,
            status_code=409,
            payload={"session_id": self.meta.session_id, "result": exc.reason, "paths": exc.paths},
        )


async def _load(
    session: AsyncSession, call: _Call
) -> tuple[AgentSessionRecord | None, list[AgentSessionUpload]]:
    # Lock the row: a second upload of the same session waits here until
    # this one commits, then plans against the stored state.
    record = await session.scalar(
        select(AgentSessionRecord)
        .where(
            AgentSessionRecord.tenant_id == call.operator.tenant_id,
            AgentSessionRecord.session_id == call.meta.session_id,
        )
        .with_for_update()
    )
    if record is None:
        return None, []
    uploads = await session.scalars(
        select(AgentSessionUpload).where(AgentSessionUpload.record_id == record.id)
    )
    return record, list(uploads)


async def _prepare_or_refuse(
    call: _Call,
    record: AgentSessionRecord | None,
    uploads: list[AgentSessionUpload],
    data: bytes,
    config: SessionStoreConfig,
) -> PreparedUpload:
    try:
        if record is not None and record.app == HOSTED_RUN_APP:
            raise SessionConflictError("session_is_a_hosted_run")
        if record is not None and record.uploaded_by_sub != call.operator.sub:
            raise SessionConflictError("session_owned_by_another_person")
        prepared = await run_in_worker(
            prepare_upload,
            data,
            max_files=config.max_files,
            max_unpacked_bytes=config.max_unpacked_bytes,
            stored=record.files if record is not None else {},
            earlier=[u.files for u in uploads],
            final=call.meta.state in ("ended", "unfinished"),
        )
        if prepared.conflicts:
            raise SessionConflictError("session_conflict", prepared.conflicts)
    except SessionConflictError as exc:
        await call.audit_refusal(exc)
        raise
    return prepared


def _add_upload_row(
    session: AsyncSession, record: AgentSessionRecord, stored: _Stored, now: datetime
) -> None:
    session.add(
        AgentSessionUpload(
            record_id=record.id,
            tenant_id=record.tenant_id,
            seq=stored.seq,
            object_key=stored.key,
            archive_sha256=stored.sha256,
            archive_bytes=stored.size,
            quarantined=stored.quarantined,
            redactions=stored.redactions,
            files=stored.files,
            uploaded_at=now,
        )
    )


def _result(
    meta: UploadMeta, state: str, outcome: str, prepared: PreparedUpload, stored: _Stored | None
) -> SessionUploadResult:
    return SessionUploadResult(
        session_id=meta.session_id,
        state=state,  # type: ignore[arg-type]
        outcome=outcome,  # type: ignore[arg-type]
        seq=stored.seq if stored else None,
        new_bytes=prepared.new_bytes,
        quarantined=stored.quarantined if stored else False,
        redactions=stored.redactions if stored else 0,
    )


async def _commit(session: AsyncSession, call: _Call, stored: _Stored | None) -> None:
    """Commit; on failure delete the new archive, so nothing half-stored stays."""
    try:
        await session.commit()
    except Exception as exc:
        if stored is not None:
            await _forget_archive(call.store, stored.key)
        if isinstance(exc, IntegrityError):
            conflict = SessionConflictError("session_upload_in_progress")
            await call.audit_refusal(conflict)
            raise conflict from exc
        raise


async def upload_session(
    operator: Operator,
    meta: UploadMeta,
    data: bytes,
    *,
    store: ObjectStore,
    config: SessionStoreConfig,
    path: str,
) -> SessionUploadResult:
    """Store one upload. Raises :class:`SessionConflictError` or ``ArchiveError``."""
    call = _Call(operator, meta, path, store)
    now = datetime.now(UTC)
    async with get_sessionmaker()() as session:
        record, uploads = await _load(session, call)
        prepared = await _prepare_or_refuse(call, record, uploads, data, config)
        archive = prepared.archive
        created = record is None
        if record is None:
            record = _new_record(call.operator, meta, now)
            session.add(record)
        new_state = _next_state(record.state, meta.state, new_bytes=archive is not None)
        if archive is None and not created and new_state == record.state:
            call.audit(session, {"result": "unchanged"})
            await session.commit()
            return _result(meta, record.state, "unchanged", prepared, None)

        stored: _Stored | None = None
        if archive is not None:
            stored = await _store_archive(call, record.id, record.upload_count + 1, archive)
            _add_upload_row(session, record, stored, now)
            record.files = {p: state.as_json() for p, state in prepared.states.items()}
            record.upload_count = stored.seq
        _apply_meta(record, meta)
        record.state = new_state
        record.last_seen_at = now
        outcome = "created" if created else ("appended" if stored else "state_changed")
        call.audit(
            session,
            {
                "result": outcome,
                "state": new_state,
                "seq": stored.seq if stored else None,
                "new_bytes": prepared.new_bytes,
                "archive_sha256": stored.sha256 if stored else None,
                "quarantined": stored.quarantined if stored else False,
                "redactions": stored.redactions if stored else 0,
            },
        )
        await _commit(session, call, stored)
    return _result(meta, new_state, outcome, prepared, stored)


async def _forget_archive(store: ObjectStore, key: str) -> None:
    """Delete an archive whose index rows did not commit. Logs, never raises."""
    try:
        await store.delete([key])
    except Exception:
        _log.warning("session_store_orphan_archive_left", object_key=key, exc_info=True)
