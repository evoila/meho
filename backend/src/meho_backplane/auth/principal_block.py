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
* every top-level operation call
  (:func:`~meho_backplane.operations.dispatcher.dispatch`);
* every tool call and model request of a hosted agent run
  (:class:`~meho_backplane.agent.principal_block_gate.PrincipalBlockGate`).
  A scheduled run acts as the agent's own login, so it is refused only
  when that login is blocked;
* an approved call, which does not run while the person who asked is
  blocked (:func:`~meho_backplane.operations.approval_queue.refuse_resume_if_asker_blocked`);
* each step of a multi-step operation;
* a running call, at fixed points while it runs (:func:`raise_if_send_refused`).
  A call that reaches such a point while its login is blocked ends with
  the block refusal, and the answer says whether a part of it may have
  run. A request that is already on its way to a system runs to its end.
  The logins it checks are the login of the running call and every login
  bound with :func:`checking_logins_for_steps`: the operator of a
  multi-step operation, and on an approved run also the person who asked.

The reader keeps the whole list in memory for a few seconds per process.
When the list cannot be read, the check raises
:class:`PrincipalBlockUnavailableError`. It never answers "not blocked"
for an unknown state.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, cast

import structlog
from sqlalchemy import Insert, delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meho_backplane.auth.operator import Operator
from meho_backplane.auth.principal_block_run import (
    mark_step_refused,
    run_login,
    step_refused_in_run,
)
from meho_backplane.auth.sign_in_use import SIGN_IN_LOCK_SCOPE, earlier_sign_in_uses
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
    "OwnBlockStaysError",
    "PrincipalBlockUnavailableError",
    "PrincipalBlockedStepError",
    "PrincipalNotBlockedError",
    "SignInAlreadyUsedError",
    "block_principal",
    "checking_logins_for_steps",
    "first_blocked_step_login",
    "is_principal_blocked",
    "list_blocked",
    "raise_if_send_refused",
    "refusal_of_step",
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


class PrincipalBlockedStepError(Exception):
    """A step of a multi-step operation stopped before its request was sent.

    ``unavailable`` is true when the block list could not be read, false
    when a login of the step is blocked.
    """

    def __init__(self, *, unavailable: bool) -> None:
        super().__init__("block list unreadable" if unavailable else PRINCIPAL_BLOCKED_MESSAGE)
        self.unavailable = unavailable


class CannotBlockSelfError(Exception):
    """An admin tried to block their own login."""


class PrincipalNotBlockedError(Exception):
    """There is no block to remove."""


class OwnBlockStaysError(Exception):
    """An admin tried to remove a block on their own login that they may not remove."""


class SignInAlreadyUsedError(Exception):
    """The fresh sign-in of an own-login unblock already approved a request."""


class BlockSetChangedError(Exception):
    """The blocks of the login are not the blocks the admin was shown."""


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


async def _entries(max_age: float = BLOCK_CACHE_SECONDS) -> frozenset[tuple[str, str]]:
    """Return the cached list, or read the table when it is older than *max_age*."""
    global _cache
    sessionmaker = get_sessionmaker()
    cached = _cache
    now = time.monotonic()
    if (
        cached is not None
        and cached.owner is sessionmaker
        and 0 <= now - cached.loaded_at < max_age
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


async def is_principal_blocked(
    sub: str, tenant_id: uuid.UUID | None, *, max_age: float = BLOCK_CACHE_SECONDS
) -> bool:
    """True when *sub* is blocked everywhere or in *tenant_id*.

    The list copy may be *max_age* seconds old at most (default
    :data:`BLOCK_CACHE_SECONDS`).

    Raises :class:`PrincipalBlockUnavailableError` when the list cannot be read.
    """
    entries = await _entries(max_age)
    if (sub, BLOCK_SCOPE_ALL) in entries:
        return True
    return tenant_id is not None and (sub, str(tenant_id)) in entries


#: The logins each step of the running multi-step operation checks, besides
#: its own operator: ``(sub, tenant_id)`` pairs. Empty outside a multi-step
#: operation and outside an approved run.
_step_logins_var: ContextVar[tuple[tuple[str, uuid.UUID | None], ...]] = ContextVar(
    "principal_block_step_logins", default=()
)


@contextmanager
def checking_logins_for_steps(sub: str, tenant_id: uuid.UUID | None) -> Iterator[None]:
    """Make every step and direct request in this block also check *sub*.

    Bound for the body of a multi-step operation with its operator, and for
    an approved run with the person who asked, so a block made while the
    run goes on stops its next step, also when the approver runs it.
    """
    token = _step_logins_var.set((*_step_logins_var.get(), (sub, tenant_id)))
    try:
        yield
    finally:
        _step_logins_var.reset(token)


async def first_blocked_step_login(
    sub: str, tenant_id: uuid.UUID | None, *, only_in_steps: bool = False
) -> str | None:
    """The first blocked login of a step: *sub*, then each bound login.

    Returns ``None`` when none is blocked. With *only_in_steps*, returns
    ``None`` at once when no login is bound (not inside a multi-step
    operation or an approved run). Raises
    :class:`PrincipalBlockUnavailableError` when the list cannot be read.
    """
    bound = _step_logins_var.get()
    if only_in_steps and not bound:
        return None
    for login, login_tenant in ((sub, tenant_id), *bound):
        if await is_principal_blocked(login, login_tenant):
            return login
    return None


async def refusal_of_step(
    operator: Operator, *, only_in_steps: bool = False
) -> PrincipalBlockedStepError | None:
    """The refusal of a step or request about to be sent, or ``None`` when it may go.

    An earlier refusal of the same run comes first: once a step or request
    of a run was refused, no later one is sent, also when the list can be
    read again. Otherwise the logins of the step decide
    (:func:`first_blocked_step_login`, with *only_in_steps*). The run is
    looked at once more after the list was read: a parallel step may have
    been refused in that time. The refusal is noted for the running
    operation, so the whole operation stops with it.
    """
    return await _refusal_for_login(operator.sub, operator.tenant_id, only_in_steps=only_in_steps)


async def _refusal_for_login(
    sub: str, tenant_id: uuid.UUID | None, *, only_in_steps: bool
) -> PrincipalBlockedStepError | None:
    refusal = _earlier_refusal()
    if refusal is None:
        try:
            blocked = await first_blocked_step_login(sub, tenant_id, only_in_steps=only_in_steps)
        except PrincipalBlockUnavailableError:
            refusal = PrincipalBlockedStepError(unavailable=True)
        else:
            refusal = (
                PrincipalBlockedStepError(unavailable=False)
                if blocked is not None
                else _earlier_refusal()
            )
    if refusal is not None:
        mark_step_refused(refusal)
    return refusal


def _earlier_refusal() -> PrincipalBlockedStepError | None:
    return cast("PrincipalBlockedStepError | None", step_refused_in_run())


async def raise_if_send_refused() -> None:
    """The check MEHO runs at fixed points while a call runs.

    A call that reaches it while its login is blocked ends with the block
    refusal. A request that is already on its way to a system runs to its
    end. The login is the one the dispatcher noted for the running call
    (:func:`~meho_backplane.auth.principal_block_run.tracking_run`), plus
    every login bound with :func:`checking_logins_for_steps`.

    Raises :class:`PrincipalBlockedStepError` when an earlier step of the
    run was refused, when a login is blocked, or when the list cannot be
    read (:func:`refusal_of_step`). It reads the list copy kept for
    :data:`BLOCK_CACHE_SECONDS`, so a send reads the database at most once
    in that time.
    """
    login = run_login()
    if login is None:
        return
    refusal = await _refusal_for_login(login[0], login[1], only_in_steps=False)
    if refusal is not None:
        _log.info("principal_block_send_refused", operator_sub=login[0])
        raise refusal


def _scope_of(operator: Operator) -> str:
    """The scope an admin's block gets: everywhere for a platform admin, else their tenant."""
    return BLOCK_SCOPE_ALL if operator.platform_admin else str(operator.tenant_id)


async def block_principal(
    operator: Operator,
    sub: str,
    reason: str,
    *,
    route: tuple[str, str] = ("POST", "/api/v1/principals/blocked"),
) -> BlockedPerson:
    """Block *sub*. The block row and its audit row commit together.

    *route* is the method and path the audit row names. The default is the
    REST route; the console page passes its own route.

    Blocking a login that is already blocked in the same scope updates
    the reason. The row is written in one statement (insert, or update
    when the row is there). While an unblock of the login holds the row,
    the statement waits; when the unblock removed the row, the statement
    writes a new one. So a block is always written. Raises
    :class:`CannotBlockSelfError` for the admin's own login, and
    :class:`PrincipalBlockUnavailableError` (503
    ``principal_block_check_unavailable``) when the database answers the
    write with an error; then nothing is written.
    """
    if sub == operator.sub:
        raise CannotBlockSelfError
    scope = _scope_of(operator)
    now = datetime.now(UTC)
    values = {
        "sub": sub,
        "scope": scope,
        "tenant_id": None if scope == BLOCK_SCOPE_ALL else operator.tenant_id,
        "reason": reason,
        "blocked_by_sub": operator.sub,
        "blocked_at": now,
    }
    changed = {k: v for k, v in values.items() if k not in ("sub", "scope")}
    try:
        async with get_sessionmaker()() as session:
            conn = await session.connection()
            stmt: Insert
            if conn.dialect.name == "postgresql":
                pg = pg_insert(PrincipalBlock).values(**values)
                stmt = pg.on_conflict_do_update(index_elements=["sub", "scope"], set_=changed)
            else:
                lite = sqlite_insert(PrincipalBlock).values(**values)
                stmt = lite.on_conflict_do_update(index_elements=["sub", "scope"], set_=changed)
            await session.execute(stmt)
            add_audit_row(
                session,
                operator,
                op_id="principal.block",
                method=route[0],
                path=route[1],
                payload={"blocked_sub": sub, "scope": scope, "reason": reason},
            )
            await session.commit()
    except SQLAlchemyError as exc:
        _log.warning(
            "principal_block_write_failed", operation="block", error_type=type(exc).__name__
        )
        raise PrincipalBlockUnavailableError(str(type(exc).__name__)) from exc
    reset_block_cache()
    return BlockedPerson(sub, scope, reason, operator.sub, now)


def block_set_digest(blocks: Iterable[BlockedPerson]) -> str:
    """A short text that names a set of blocks.

    It changes when a block is added or removed, and when a block is set
    again (a new reason, admin or time).
    """
    rows = sorted(
        [b.sub, b.scope, b.reason, b.blocked_by_sub, b.blocked_at.isoformat()] for b in blocks
    )
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()


async def _read_blocks(session: AsyncSession, sub: str) -> list[BlockedPerson]:
    """Every block of *sub*, sorted by scope, read from the table.

    The rows stay locked until the transaction ends, so a block that is
    set again in the meantime waits, and the delete removes the rows read.
    The rows are locked in the order of their scope, so two unblocks of
    one login wait for each other and never for each other in a circle.
    Raises :class:`PrincipalBlockUnavailableError` when the table cannot be read.
    """
    query = (
        select(PrincipalBlock)
        .where(PrincipalBlock.sub == sub)
        .order_by(PrincipalBlock.scope)
        .with_for_update()
    )
    try:
        rows = (await session.execute(query)).scalars().all()
    except Exception as exc:
        _log.warning("principal_block_read_failed", exc_info=True)
        raise PrincipalBlockUnavailableError(str(type(exc).__name__)) from exc
    blocks = [BlockedPerson(r.sub, r.scope, r.reason, r.blocked_by_sub, r.blocked_at) for r in rows]
    return sorted(blocks, key=lambda b: b.scope)


async def _delete_read_blocks(session: AsyncSession, blocks: list[BlockedPerson]) -> int:
    """Delete each block only while it is still the block that was read; return how many."""
    deleted = 0
    for block in blocks:
        result = await session.execute(
            delete(PrincipalBlock).where(
                PrincipalBlock.sub == block.sub,
                PrincipalBlock.scope == block.scope,
                PrincipalBlock.reason == block.reason,
                PrincipalBlock.blocked_by_sub == block.blocked_by_sub,
                PrincipalBlock.blocked_at == block.blocked_at,
            )
        )
        deleted += int(result.rowcount or 0)  # type: ignore[attr-defined]
    return deleted


async def _refuse_used_sign_in(
    session: AsyncSession, operator: Operator, sign_in: dict[str, Any]
) -> None:
    """Raise :class:`SignInAlreadyUsedError` when an approve already used the sign-in.

    Raises :class:`PrincipalBlockUnavailableError` when the lock or the
    audit rows cannot be read, so nothing is removed.
    """
    # Imported here: the approval queue imports this module.
    from meho_backplane.operations import approval_queue

    key = str(sign_in["sign_in_key"])
    try:
        await approval_queue.take_approval_lock(session, SIGN_IN_LOCK_SCOPE, key)
        uses = await earlier_sign_in_uses(
            session, sub=operator.sub, sign_in_key=key, auth_time=int(sign_in["auth_time"])
        )
    except Exception as exc:
        _log.warning("principal_unblock_sign_in_check_failed", exc_info=True)
        raise PrincipalBlockUnavailableError(str(type(exc).__name__)) from exc
    if any(use.kind == "approval" for use in uses):
        _log.warning("principal_unblock_sign_in_reused", operator_sub=operator.sub)
        raise SignInAlreadyUsedError


async def unblock_principal(
    operator: Operator,
    sub: str,
    *,
    reason: str | None = None,
    sign_in: dict[str, Any] | None = None,
    shown: str | None = None,
    route: tuple[str, str] | None = None,
) -> int:
    """Remove the blocks of *sub* the admin may remove, and return how many.

    A platform admin removes every block of the login. A tenant admin
    removes the block of their own tenant only. On their own login an
    admin removes only the block of the current tenant. The table is read
    for it, not the cached list: a block in every tenant, or for a tenant
    admin a block in this tenant, raises :class:`OwnBlockStaysError` and
    nothing is removed. The audit row names each removed block with its
    scope and reason. Raises :class:`PrincipalNotBlockedError` when there
    is none, and :class:`PrincipalBlockUnavailableError` when the table,
    the lock of the sign-in or the audit rows cannot be read, or the table
    cannot be written; then nothing is removed. *reason* and *sign_in*
    (the proof of a fresh sign-in) go into the audit row when they are
    given. A *sign_in* that an ``approval.decision`` audit row already
    names is used up: the call raises :class:`SignInAlreadyUsedError` and
    removes nothing. The scan and the removal run under the advisory lock
    of the sign-in (:mod:`meho_backplane.auth.sign_in_use`). *route* is
    the method and path the audit row names; the default is the REST
    route. When *shown* is given (the :func:`block_set_digest` of the
    blocks the admin was shown), MEHO removes the blocks only when they
    are still exactly those blocks, and else raises
    :class:`BlockSetChangedError` and removes nothing. The delete also
    removes a block only while it is still the block that was read (same
    reason, admin and time). When one was changed in the meantime, MEHO
    removes nothing and raises :class:`BlockSetChangedError`.
    """
    tenant_scope = str(operator.tenant_id)
    own_login = sub == operator.sub
    only_this_tenant = own_login or not operator.platform_admin
    try:
        async with get_sessionmaker()() as session:
            if sign_in is not None:
                await _refuse_used_sign_in(session, operator, sign_in)
            blocks = await _read_blocks(session, sub)
            scopes = {b.scope for b in blocks}
            if own_login and (
                BLOCK_SCOPE_ALL in scopes
                or (not operator.platform_admin and tenant_scope in scopes)
            ):
                raise OwnBlockStaysError
            removed = [b for b in blocks if b.scope == tenant_scope] if only_this_tenant else blocks
            if not removed:
                raise PrincipalNotBlockedError
            if shown is not None and shown != block_set_digest(removed):
                raise BlockSetChangedError
            if await _delete_read_blocks(session, removed) != len(removed):
                await session.rollback()
                raise BlockSetChangedError
            removed_scopes = [b.scope for b in removed]
            payload: dict[str, Any] = {
                "unblocked_sub": sub,
                "scopes": removed_scopes,
                "removed_blocks": [{"scope": b.scope, "reason": b.reason} for b in removed],
            }
            if reason is not None:
                payload["reason"] = reason
            if sign_in is not None:
                payload["sign_in"] = sign_in
            add_audit_row(
                session,
                operator,
                op_id="principal.unblock",
                method=route[0] if route else "DELETE",
                path=route[1] if route else f"/api/v1/principals/blocked/{sub}",
                payload=payload,
            )
            await session.commit()
    except SQLAlchemyError as exc:
        _log.warning(
            "principal_unblock_write_failed", operation="unblock", error_type=type(exc).__name__
        )
        raise PrincipalBlockUnavailableError(str(type(exc).__name__)) from exc
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
