# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""List, read and delete stored sessions, and set the retention period.

Every function works only in the caller's tenant: a session of another
tenant is "not found". Every call writes an audit row
(:mod:`.audit`). The rights are checked by the routes
(:mod:`.rights`); :func:`read_upload` also checks the quarantine right,
because only the stored row knows whether an upload is in quarantine.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditEventClass
from meho_backplane.session_store.audit import add_audit_row, write_audit_row
from meho_backplane.session_store.config import SessionStoreConfig
from meho_backplane.session_store.models import (
    AgentSessionRecord,
    AgentSessionRetention,
    AgentSessionUpload,
)
from meho_backplane.session_store.object_store import ObjectStore
from meho_backplane.session_store.rights import SessionRight, has_right
from meho_backplane.session_store.schemas import (
    SessionDetail,
    SessionFileState,
    SessionListResponse,
    SessionRetention,
    summary_of,
    upload_part_of,
)

__all__ = [
    "QuarantinedError",
    "SessionNotFoundError",
    "delete_record_rows",
    "delete_session",
    "get_retention",
    "list_sessions",
    "read_session",
    "read_upload",
    "retention_days_for",
    "set_retention",
]


class SessionNotFoundError(Exception):
    """No such session (or upload) in the caller's tenant."""


class QuarantinedError(Exception):
    """The upload is in quarantine and the caller is not an admin."""


async def _record(
    session: AsyncSession, operator: Operator, session_id: str, *, lock: bool = False
) -> AgentSessionRecord:
    query = select(AgentSessionRecord).where(
        AgentSessionRecord.tenant_id == operator.tenant_id,
        AgentSessionRecord.session_id == session_id,
    )
    record = await session.scalar(query.with_for_update() if lock else query)
    if record is None:
        raise SessionNotFoundError(session_id)
    return record


async def list_sessions(
    operator: Operator,
    *,
    path: str,
    project: str | None,
    state: str | None,
    limit: int,
    offset: int,
) -> SessionListResponse:
    """Return one page of the tenant's sessions, newest upload first."""
    query = select(AgentSessionRecord).where(AgentSessionRecord.tenant_id == operator.tenant_id)
    if project is not None:
        query = query.where(AgentSessionRecord.project == project)
    if state is not None:
        query = query.where(AgentSessionRecord.state == state)
    query = query.order_by(AgentSessionRecord.last_seen_at.desc(), AgentSessionRecord.id).offset(
        offset
    )
    async with get_sessionmaker()() as session:
        rows = list(await session.scalars(query.limit(limit + 1)))
        page = rows[:limit]
        add_audit_row(
            session,
            operator,
            op_id="sessions.list",
            method="GET",
            path=path,
            payload={"project": project, "state": state, "offset": offset, "count": len(page)},
        )
        await session.commit()
    more = len(rows) > limit
    return SessionListResponse(
        items=[summary_of(row) for row in page],
        next_cursor=str(offset + limit) if more else None,
    )


async def read_session(operator: Operator, session_id: str, *, path: str) -> SessionDetail:
    """Return one session with its file hashes and uploads."""
    async with get_sessionmaker()() as session:
        record = await _record(session, operator, session_id)
        uploads = list(
            await session.scalars(
                select(AgentSessionUpload)
                .where(AgentSessionUpload.record_id == record.id)
                .order_by(AgentSessionUpload.seq)
            )
        )
        add_audit_row(
            session,
            operator,
            op_id="sessions.read",
            method="GET",
            path=path,
            payload={"session_id": session_id},
        )
        await session.commit()
    return SessionDetail(
        **summary_of(record).model_dump(),
        files={p: SessionFileState(**state) for p, state in record.files.items()},
        uploads=[upload_part_of(upload) for upload in uploads],
    )


async def read_upload(
    operator: Operator, session_id: str, seq: int, *, store: ObjectStore, path: str
) -> bytes:
    """Return the stored archive of one upload.

    Raises :class:`QuarantinedError` (and audits the refusal) when the
    upload is in quarantine and the caller has no right to open it.
    """
    async with get_sessionmaker()() as session:
        record = await _record(session, operator, session_id)
        upload = await session.scalar(
            select(AgentSessionUpload).where(
                AgentSessionUpload.record_id == record.id, AgentSessionUpload.seq == seq
            )
        )
        if upload is None:
            raise SessionNotFoundError(f"{session_id}/{seq}")
        payload = {"session_id": session_id, "seq": seq, "quarantined": upload.quarantined}
        if upload.quarantined and not has_right(operator, SessionRight.READ_QUARANTINE):
            add_audit_row(
                session,
                operator,
                op_id="sessions.download",
                method="GET",
                path=path,
                status_code=403,
                event_class=AuditEventClass.POLICY_DECISION,
                payload=payload,
            )
            await session.commit()
            raise QuarantinedError(session_id)
        key = upload.object_key
    data = await store.get(key)
    await write_audit_row(
        operator,
        op_id="sessions.download",
        method="GET",
        path=path,
        payload={**payload, "bytes": len(data)},
    )
    return data


async def delete_session(
    operator: Operator, session_id: str, *, store: ObjectStore, path: str
) -> None:
    """Delete one session: its files first, then its rows and the audit row.

    When the object storage fails, nothing in the index changes and a retry
    is safe. A file that is already gone is not an error. The session row
    is locked first, like an upload locks it, so an upload at the same
    time either ends before the files are listed or waits until the rows
    are gone.
    """
    async with get_sessionmaker()() as session:
        record = await _record(session, operator, session_id, lock=True)
        keys = list(
            await session.scalars(
                select(AgentSessionUpload.object_key).where(
                    AgentSessionUpload.record_id == record.id
                )
            )
        )
        await store.delete(keys)
        await delete_record_rows(session, record.id)
        add_audit_row(
            session,
            operator,
            op_id="sessions.delete",
            method="DELETE",
            path=path,
            payload={"session_id": session_id, "objects": len(keys)},
        )
        await session.commit()


async def delete_record_rows(session: AsyncSession, record_id: uuid.UUID) -> None:
    """Delete one record and its upload rows in *session* (the caller commits)."""
    await session.execute(
        delete(AgentSessionUpload).where(AgentSessionUpload.record_id == record_id)
    )
    await session.execute(delete(AgentSessionRecord).where(AgentSessionRecord.id == record_id))


async def retention_days_for(
    session: AsyncSession, tenant_id: uuid.UUID, config: SessionStoreConfig
) -> SessionRetention:
    """Return the tenant's own retention period, or the default."""
    days = await session.scalar(
        select(AgentSessionRetention.retention_days).where(
            AgentSessionRetention.tenant_id == tenant_id
        )
    )
    if days is None:
        return SessionRetention(retention_days=config.retention_days_default, source="default")
    return SessionRetention(retention_days=days, source="tenant")


async def get_retention(operator: Operator, config: SessionStoreConfig) -> SessionRetention:
    """Return the retention period of the caller's tenant."""
    async with get_sessionmaker()() as session:
        return await retention_days_for(session, operator.tenant_id, config)


async def set_retention(
    operator: Operator, days: int | None, config: SessionStoreConfig, *, path: str
) -> SessionRetention:
    """Set (or clear, with ``None``) the retention period of the caller's tenant."""
    async with get_sessionmaker()() as session:
        before = await retention_days_for(session, operator.tenant_id, config)
        row = await session.get(AgentSessionRetention, operator.tenant_id)
        if days is None:
            if row is not None:
                await session.delete(row)
        elif row is None:
            session.add(
                AgentSessionRetention(
                    tenant_id=operator.tenant_id,
                    retention_days=days,
                    updated_by_sub=operator.sub,
                    updated_at=datetime.now(UTC),
                )
            )
        else:
            row.retention_days = days
            row.updated_by_sub = operator.sub
            row.updated_at = datetime.now(UTC)
        add_audit_row(
            session,
            operator,
            op_id="sessions.retention.set",
            method="PUT",
            path=path,
            event_class=AuditEventClass.CONFIG_CHANGE,
            payload={"before": before.model_dump(), "retention_days": days},
        )
        await session.commit()
        return await retention_days_for(session, operator.tenant_id, config)
