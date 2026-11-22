# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The admin block list: people MEHO refuses on every request.

MEHO checks sign-in tokens itself, so a person disabled in the login
server keeps working until the token ends. An admin can also block a
person in MEHO. A blocked login is refused within
:data:`BLOCK_CACHE_SECONDS` on every MEHO process.

Scope of a block:

* A platform admin blocks a person in every tenant (scope ``*``).
* A tenant admin blocks a person in their own tenant (scope = the tenant
  id). The block never reaches another tenant.
* Nobody blocks their own login.

Where it is checked (all through :func:`is_principal_blocked`):

* every REST and MCP request, right after the token is checked
  (:func:`~meho_backplane.auth.jwt.verify_jwt_for_audience`);
* every page request of the web console
  (:mod:`meho_backplane.ui.auth.middleware`);
* every top-level operation call, which covers hosted agent runs and
  scheduled runs (:func:`~meho_backplane.operations.dispatcher.dispatch`);
* an approved call, which does not run while the person who asked is
  blocked (:func:`~meho_backplane.operations.approval_queue.refuse_resume_if_asker_blocked`).

The reader keeps the whole list in memory for a few seconds per process.
When the list cannot be read, the check raises
:class:`PrincipalBlockUnavailableError`. It never answers "not blocked"
for an unknown state.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meho_backplane.auth.operator import Operator
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import PrincipalBlock
from meho_backplane.session_store.audit import add_audit_row

__all__ = [
    "BLOCK_CACHE_SECONDS",
    "BLOCK_SCOPE_ALL",
    "PRINCIPAL_BLOCKED_MESSAGE",
    "REASON_MAX_LENGTH",
    "BlockedPerson",
    "CannotBlockSelfError",
    "PrincipalBlockUnavailableError",
    "PrincipalNotBlockedError",
    "block_principal",
    "is_principal_blocked",
    "list_blocked",
    "reset_block_cache",
    "unblock_principal",
]

#: How long one process keeps the block list. A block reaches every
#: process within this time.
BLOCK_CACHE_SECONDS: Final[float] = 5.0

#: ``scope`` of a block that holds in every tenant.
BLOCK_SCOPE_ALL: Final[str] = "*"

#: What a blocked person reads. It names no admin and no reason.
PRINCIPAL_BLOCKED_MESSAGE: Final[str] = "This person is blocked in MEHO. Ask an admin."

#: Longest reason an admin can type.
REASON_MAX_LENGTH: Final[int] = 500


class PrincipalBlockUnavailableError(Exception):
    """The block list could not be read, so MEHO cannot say if a login is blocked."""


class CannotBlockSelfError(Exception):
    """An admin tried to block their own login."""


class PrincipalNotBlockedError(Exception):
    """There is no block to remove."""


@dataclass(frozen=True)
class BlockedPerson:
    """One row of the block list."""

    sub: str
    scope: str
    reason: str
    blocked_by_sub: str
    blocked_at: datetime


@dataclass
class _Cache:
    loaded_at: float
    owner: object
    entries: frozenset[tuple[str, str]]


_cache: _Cache | None = None
_log = structlog.get_logger(__name__)


def reset_block_cache() -> None:
    """Forget the cached list, so the next check reads the table."""
    global _cache
    _cache = None


async def _load_entries(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> frozenset[tuple[str, str]]:
    async with sessionmaker() as session:
        rows = (await session.execute(select(PrincipalBlock.sub, PrincipalBlock.scope))).all()
    return frozenset((str(sub), str(scope)) for sub, scope in rows)


async def _entries() -> frozenset[tuple[str, str]]:
    """Return the cached list, or read the table when it is older than the limit."""
    global _cache
    sessionmaker = get_sessionmaker()
    cached = _cache
    now = time.monotonic()
    if (
        cached is not None
        and cached.owner is sessionmaker
        and 0 <= now - cached.loaded_at < BLOCK_CACHE_SECONDS
    ):
        return cached.entries
    try:
        entries = await _load_entries(sessionmaker)
    except Exception as exc:
        _cache = None
        _log.warning("principal_block_read_failed", exc_info=True)
        raise PrincipalBlockUnavailableError(str(type(exc).__name__)) from exc
    _cache = _Cache(loaded_at=now, owner=sessionmaker, entries=entries)
    return entries


async def is_principal_blocked(sub: str, tenant_id: uuid.UUID | None) -> bool:
    """True when *sub* is blocked everywhere or in *tenant_id*.

    Raises :class:`PrincipalBlockUnavailableError` when the list cannot be read.
    """
    entries = await _entries()
    if (sub, BLOCK_SCOPE_ALL) in entries:
        return True
    return tenant_id is not None and (sub, str(tenant_id)) in entries


def _scope_of(operator: Operator) -> str:
    """The scope an admin's block gets: everywhere for a platform admin, else their tenant."""
    return BLOCK_SCOPE_ALL if operator.platform_admin else str(operator.tenant_id)


async def block_principal(operator: Operator, sub: str, reason: str) -> BlockedPerson:
    """Block *sub*. The block row and its audit row commit together.

    Blocking a login that is already blocked in the same scope updates
    the reason. Raises :class:`CannotBlockSelfError` for the admin's own login.
    """
    if sub == operator.sub:
        raise CannotBlockSelfError
    scope = _scope_of(operator)
    now = datetime.now(UTC)
    async with get_sessionmaker()() as session:
        row = await session.get(PrincipalBlock, (sub, scope))
        if row is None:
            row = PrincipalBlock(sub=sub, scope=scope)
            session.add(row)
        row.tenant_id = None if scope == BLOCK_SCOPE_ALL else operator.tenant_id
        row.reason = reason
        row.blocked_by_sub = operator.sub
        row.blocked_at = now
        add_audit_row(
            session,
            operator,
            op_id="principal.block",
            method="POST",
            path="/api/v1/principals/blocked",
            payload={"blocked_sub": sub, "scope": scope, "reason": reason},
        )
        await session.commit()
    reset_block_cache()
    return BlockedPerson(sub, scope, reason, operator.sub, now)


async def unblock_principal(operator: Operator, sub: str) -> int:
    """Remove the blocks of *sub* the admin may remove, and return how many.

    A platform admin removes every block of the login. A tenant admin
    removes the block of their own tenant only. The audit row names each
    removed block with its scope and reason. Raises
    :class:`PrincipalNotBlockedError` when there is none.
    """
    condition = PrincipalBlock.sub == sub
    if not operator.platform_admin:
        condition = condition & (PrincipalBlock.scope == str(operator.tenant_id))
    async with get_sessionmaker()() as session:
        query = select(PrincipalBlock.scope, PrincipalBlock.reason).where(condition)
        removed = sorted((await session.execute(query)).tuples())
        if not removed:
            raise PrincipalNotBlockedError
        await session.execute(delete(PrincipalBlock).where(condition))
        add_audit_row(
            session,
            operator,
            op_id="principal.unblock",
            method="DELETE",
            path=f"/api/v1/principals/blocked/{sub}",
            payload={
                "unblocked_sub": sub,
                "scopes": [scope for scope, _reason in removed],
                "removed_blocks": [{"scope": scope, "reason": reason} for scope, reason in removed],
            },
        )
        await session.commit()
    reset_block_cache()
    return len(removed)


async def list_blocked(operator: Operator) -> list[BlockedPerson]:
    """The blocks the admin may see: all for a platform admin, else their tenant's."""
    stmt = select(PrincipalBlock).order_by(PrincipalBlock.blocked_at.desc())
    if not operator.platform_admin:
        stmt = stmt.where(PrincipalBlock.scope == str(operator.tenant_id))
    async with get_sessionmaker()() as session:
        rows = (await session.execute(stmt)).scalars().all()
    return [BlockedPerson(r.sub, r.scope, r.reason, r.blocked_by_sub, r.blocked_at) for r in rows]
