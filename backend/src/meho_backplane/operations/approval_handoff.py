"""Encrypted, one-time custody for credential-write approval inputs."""

from __future__ import annotations

import json
import uuid
from typing import Any, cast

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import ForeignKey, LargeBinary, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from meho_backplane.db.models import Base
from meho_backplane.settings import get_settings


class ApprovalHandoffError(Exception):
    pass


class ApprovalHandoffKeyError(ApprovalHandoffError):
    pass


class ApprovalHandoffMissingError(ApprovalHandoffError):
    pass


class ApprovalExecutionPayload(Base):
    __tablename__ = "approval_execution_payload"
    handle: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    request_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("approval_request.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)


def _fernet() -> Fernet:
    key = get_settings().approval_handoff_encryption_key
    if not key:
        raise ApprovalHandoffKeyError("APPROVAL_HANDOFF_ENCRYPTION_KEY is not set")
    try:
        return Fernet(key.encode("ascii"))
    except ValueError as exc:
        raise ApprovalHandoffKeyError("APPROVAL_HANDOFF_ENCRYPTION_KEY is malformed") from exc


def store(
    session: AsyncSession, request_id: uuid.UUID, tenant_id: uuid.UUID, payload: dict[str, Any]
) -> uuid.UUID:
    handle = uuid.uuid4()
    data = json.dumps(
        {"request_id": str(request_id), "tenant_id": str(tenant_id), "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    session.add(
        ApprovalExecutionPayload(
            handle=handle,
            request_id=request_id,
            tenant_id=tenant_id,
            ciphertext=_fernet().encrypt(data),
        )
    )
    return handle


async def consume(
    session: AsyncSession, handle: uuid.UUID, request_id: uuid.UUID, tenant_id: uuid.UUID
) -> dict[str, Any]:
    row = (
        await session.execute(
            select(ApprovalExecutionPayload)
            .where(
                ApprovalExecutionPayload.handle == handle,
                ApprovalExecutionPayload.request_id == request_id,
                ApprovalExecutionPayload.tenant_id == tenant_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise ApprovalHandoffMissingError("approval execution payload is unavailable")
    try:
        payload = json.loads(_fernet().decrypt(row.ciphertext))
    except (InvalidToken, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApprovalHandoffError("approval execution payload cannot be decrypted") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("request_id") != str(request_id)
        or payload.get("tenant_id") != str(tenant_id)
        or not isinstance(payload.get("payload"), dict)
    ):
        raise ApprovalHandoffError("approval execution payload has an invalid shape")
    await session.delete(row)
    return cast(dict[str, Any], payload["payload"])


async def discard(session: AsyncSession, request_id: uuid.UUID, tenant_id: uuid.UUID) -> None:
    """Remove a never-executed custody record in its terminal transaction."""
    row = (
        await session.execute(
            select(ApprovalExecutionPayload).where(
                ApprovalExecutionPayload.request_id == request_id,
                ApprovalExecutionPayload.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if row is not None:
        await session.delete(row)
