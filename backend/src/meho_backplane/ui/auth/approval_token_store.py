# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The console approval sign-in token on the session row (#6282).

The console signs a person in once more with its own approval client
before an approve
(:mod:`meho_backplane.ui.auth.approval_sign_in_routes`). The access token
of that sign-in stays on the person's ``web_session`` row, encrypted with
the same key as the session's own tokens
(``UI_SESSION_ENCRYPTION_KEY``). The browser never holds it.

:func:`approval_operator` reads it back and checks it again through the
JWT chain, so an expired or changed token is never a proof. The approve
check itself is
:func:`~meho_backplane.auth.web_approval_sign_in.check_web_approval_sign_in`.
"""

from __future__ import annotations

import uuid

import structlog
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.jwt import verify_jwt_for_audience
from meho_backplane.auth.operator import Operator
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import WebSession
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth.session_store import _decrypt, _encrypt

__all__ = ["approval_operator", "load_approval_token", "store_approval_token"]


async def store_approval_token(session: AsyncSession, session_id: uuid.UUID, token: str) -> bool:
    """Keep *token* on the active session *session_id*; whether the session was there.

    A revoked session keeps nothing. A new approval sign-in replaces the
    token of an earlier one. Flushes; the caller commits.
    """
    row = await session.get(WebSession, session_id)
    if row is None or row.revoked_at is not None:
        return False
    row.approval_token = _encrypt(token)
    await session.flush()
    return True


async def load_approval_token(session: AsyncSession, session_id: uuid.UUID) -> str | None:
    """The approval sign-in token of the active session *session_id*, or ``None``."""
    row = await session.get(WebSession, session_id)
    if row is None or row.revoked_at is not None or row.approval_token is None:
        return None
    return _decrypt(row.approval_token)


async def approval_operator(session_id: uuid.UUID) -> Operator | None:
    """The checked approval sign-in of the session, or ``None``.

    ``None`` when the session has no approval sign-in, or when its token no
    longer passes the JWT chain (it expired, or the keys changed).
    """
    async with get_sessionmaker()() as db_session:
        token = await load_approval_token(db_session, session_id)
    if token is None:
        return None
    try:
        operator = await verify_jwt_for_audience(
            f"Bearer {token}",
            expected_audience=get_settings().keycloak_audience,
            allow_unchosen=True,
        )
    except HTTPException as exc:
        structlog.get_logger(__name__).info(
            "ui_approval_sign_in_token_unusable", status_code=exc.status_code
        )
        return None
    # The proof of a console approve by the person, in this moment.
    return operator.model_copy(update={"in_code_kind": "person"})
