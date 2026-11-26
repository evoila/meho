# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The admin block list: logins MEHO refuses on every request.

MEHO checks sign-in tokens itself, so a person disabled in the login
server keeps working until the token ends. An admin can also block a
login in MEHO. A blocked login is refused within
:data:`BLOCK_CACHE_SECONDS` on every MEHO process.

What a block names (:mod:`meho_backplane.auth.principal_block_agents`):

* A text that starts with ``agent:`` names an agent: the
  ``keycloak_client_id`` of the agent list. It must name an agent
  registered in the admin's reach, else MEHO refuses it
  (:class:`UnknownLoginError`). The block stops every token of that
  agent: the token check compares the token's client id when it starts
  with ``agent:`` (:func:`login_name`).
* Every other text is a login id (the token's ``sub``). It stops only
  the login with that id.

A check that knows only a login id finds a block of an agent name by:

* the login id behind the agent, which MEHO reads from Keycloak at block
  time and stores with the block (``principal_block.login_sub``);
* each login id that MEHO saw with that agent name
  (``machine_login.client_id``);
* while a block of an agent name is in scope: a machine login (any login
  that is not a person) whose agent name MEHO does not know is refused. A
  machine login whose agent name MEHO knows is refused when that name is
  blocked. This holds also when the block has a stored login id.

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
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, cast

import structlog
from sqlalchemy import Insert, delete, func, select, union
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meho_backplane.auth.operator import Operator, PrincipalKind, is_human_principal
from meho_backplane.auth.principal_block_agents import (
    AGENT_NAME_PREFIX,
    UnknownLoginError,
    agent_login_id,
    agent_names,
    agent_of_block_text,
    is_agent_name,
    keep_refused_agent_link,
)
from meho_backplane.auth.principal_block_run import (
    CheckedLogin,
    mark_step_refused,
    run_login,
    step_refused_in_run,
)
from meho_backplane.auth.sign_in_use import SIGN_IN_LOCK_SCOPE, earlier_sign_in_uses
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import MachineLogin, PrincipalBlock
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
    "UnknownLoginError",
    "block_principal",
    "checking_logins_for_steps",
    "checking_operator_for_steps",
    "first_blocked_step_login",
    "is_principal_blocked",
    "keep_agent_link_if_refused_by_name",
    "list_blocked",
    "login_name",
    "may_be_agent",
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

#: What a blocked login reads. It names no admin and no reason.
PRINCIPAL_BLOCKED_MESSAGE: Final[str] = "This login is blocked in MEHO. Ask an admin."

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
    #: The agent name (``agent:<name>``) of the blocked login, when it is an agent.
    agent: str | None = None


@dataclass(frozen=True)
class _Entries:
    """The block list as one process keeps it.

    ``blocked`` holds every blocked ``(login, scope)``. ``agent_scopes``
    holds the scope of each block of an agent name.
    """

    blocked: frozenset[tuple[str, str]]
    agent_scopes: frozenset[str] = frozenset()


@dataclass
class _Cache:
    loaded_at: float
    owner: object
    entries: _Entries


_cache: _Cache | None = None
_log = structlog.get_logger(__name__)


def reset_block_cache() -> None:
    """Forget the cached list, so the next check reads the table."""
    global _cache
    _cache = None


async def _load_entries(sessionmaker: async_sessionmaker[AsyncSession]) -> _Entries:
    """Read the block list.

    A block of an agent name also adds the login id stored with it and
    each login id that MEHO saw with that agent name, so a check by login
    id alone refuses it too. Only a block text that starts with
    ``agent:`` is compared with agent names.
    """
    by_agent_name = func.substr(PrincipalBlock.sub, 1, len(AGENT_NAME_PREFIX)) == AGENT_NAME_PREFIX
    blocks = select(PrincipalBlock.sub, PrincipalBlock.scope)
    linked = select(PrincipalBlock.login_sub, PrincipalBlock.scope).where(
        by_agent_name, PrincipalBlock.login_sub.is_not(None)
    )
    seen = (
        select(MachineLogin.sub, PrincipalBlock.scope)
        .select_from(MachineLogin)
        .join(PrincipalBlock, PrincipalBlock.sub == MachineLogin.client_id)
        .where(by_agent_name)
    )
    agent_scopes = select(PrincipalBlock.scope).where(by_agent_name)
    async with sessionmaker() as session:
        rows: list[tuple[str | None, str]] = [
            (sub, scope)
            for sub, scope in (await session.execute(union(blocks, linked, seen))).all()
        ]
        scopes = (await session.execute(agent_scopes)).scalars().all()
    return _Entries(
        blocked=frozenset((str(sub), str(scope)) for sub, scope in rows),
        agent_scopes=frozenset(str(scope) for scope in scopes),
    )


async def _entries(max_age: float = BLOCK_CACHE_SECONDS) -> _Entries:
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


def login_name(operator: Operator) -> str | None:
    """The agent name of an agent's token (``agent:<name>``), or ``None``.

    It is the client id of the token when it starts with ``agent:``. A
    person's token, and a machine token with another client id, give
    ``None``: their client id is no agent name.
    """
    if is_human_principal(operator) or not is_agent_name(operator.client_id):
        return None
    return operator.client_id


def may_be_agent(principal_kind: str | None) -> bool:
    """True when a login of *principal_kind* is a machine login (``None`` = not recorded).

    The same rule as the token check (:func:`login_name`): every login that
    is not a person may carry an agent name. An agent token without the
    ``principal_kind`` claim can be recorded as a service.
    """
    return principal_kind != PrincipalKind.USER.value


async def _known_client_ids(sub: str, tenant_id: uuid.UUID | None) -> list[str]:
    """The client ids MEHO keeps for the login id *sub* (``machine_login.client_id``)."""
    query = select(MachineLogin.client_id).where(
        MachineLogin.sub == sub, MachineLogin.client_id.is_not(None)
    )
    if tenant_id is not None:
        query = query.where(MachineLogin.tenant_id == tenant_id)
    try:
        async with get_sessionmaker()() as session:
            return [str(c) for c in (await session.execute(query)).scalars()]
    except Exception as exc:
        _log.warning("principal_block_read_failed", exc_info=True)
        raise PrincipalBlockUnavailableError(str(type(exc).__name__)) from exc


async def _unknown_machine_login_blocked(
    entries: _Entries, sub: str, tenant_id: uuid.UUID | None, scopes: list[str]
) -> bool:
    """The check of a machine login by its login id alone, while an agent block is in scope.

    MEHO reads the client id it keeps for *sub*. The login is refused when
    there is none, and when one is an agent name that is blocked in the
    same list copy *entries*.
    """
    client_ids = await _known_client_ids(sub, tenant_id)
    if not client_ids:
        return True
    return any(
        (name, scope) in entries.blocked
        for name in client_ids
        if is_agent_name(name)
        for scope in scopes
    )


async def is_principal_blocked(
    sub: str,
    tenant_id: uuid.UUID | None,
    *,
    client_id: str | None = None,
    maybe_agent: bool = False,
    max_age: float = BLOCK_CACHE_SECONDS,
) -> bool:
    """True when *sub*, or the agent name *client_id*, is blocked everywhere or in *tenant_id*.

    Pass :func:`login_name` as *client_id* when the token is at hand. A
    *client_id* that does not start with ``agent:`` is not compared. Set
    *maybe_agent* when the login is a machine login and its agent name is
    not at hand (:func:`may_be_agent`): then, while a block of an agent
    name is in scope, the login is refused unless MEHO keeps a client id
    for *sub* that is not a blocked agent name
    (:func:`_unknown_machine_login_blocked`). The list copy may be
    *max_age* seconds old at most (default :data:`BLOCK_CACHE_SECONDS`).

    Raises :class:`PrincipalBlockUnavailableError` when the list cannot be read.
    """
    entries = await _entries(max_age)
    scopes = [BLOCK_SCOPE_ALL] if tenant_id is None else [BLOCK_SCOPE_ALL, str(tenant_id)]
    name = client_id if is_agent_name(client_id) else None
    logins = [sub] if name is None else [sub, name]
    if any((login, scope) in entries.blocked for login in logins for scope in scopes):
        return True
    if client_id is None and maybe_agent and entries.agent_scopes.intersection(scopes):
        return await _unknown_machine_login_blocked(entries, sub, tenant_id, scopes)
    return False


async def keep_agent_link_if_refused_by_name(
    operator: Operator, tenant_id: uuid.UUID | None
) -> None:
    """Keep the link of an agent token's login id to its agent name, once.

    Called when the token check refused the token. When the login id alone
    is not blocked yet, the block matched the agent name: MEHO then writes
    the link (``machine_login.client_id``) and reads the list again, so
    every later check by the login id alone finds the block.
    """
    if await is_principal_blocked(operator.sub, tenant_id):
        return
    await keep_refused_agent_link(operator)
    reset_block_cache()


#: The logins each step of the running multi-step operation checks, besides
#: its own operator: ``(sub, tenant_id)`` pairs. Empty outside a multi-step
#: operation and outside an approved run.
_step_logins_var: ContextVar[tuple[CheckedLogin, ...]] = ContextVar(
    "principal_block_step_logins", default=()
)


@contextmanager
def checking_logins_for_steps(
    sub: str,
    tenant_id: uuid.UUID | None,
    *,
    client_id: str | None = None,
    maybe_agent: bool = False,
) -> Iterator[None]:
    """Make every step and direct request in this block also check *sub*.

    Bound for the body of a multi-step operation with its operator, and for
    an approved run with the login who asked, so a block made while the
    run goes on stops its next step, also when the approver runs it.
    *client_id* and *maybe_agent* are as in :func:`is_principal_blocked`.
    """
    login = CheckedLogin(sub, tenant_id, client_id, maybe_agent)
    token = _step_logins_var.set((*_step_logins_var.get(), login))
    try:
        yield
    finally:
        _step_logins_var.reset(token)


def checking_operator_for_steps(operator: Operator) -> AbstractContextManager[None]:
    """:func:`checking_logins_for_steps` for *operator*, with its agent name."""
    return checking_logins_for_steps(
        operator.sub,
        operator.tenant_id,
        client_id=login_name(operator),
        maybe_agent=operator.principal_kind is PrincipalKind.AGENT,
    )


async def first_blocked_step_login(
    sub: str,
    tenant_id: uuid.UUID | None,
    *,
    only_in_steps: bool = False,
    client_id: str | None = None,
    maybe_agent: bool = False,
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
    for login in (CheckedLogin(sub, tenant_id, client_id, maybe_agent), *bound):
        if await is_principal_blocked(
            login.sub, login.tenant_id, client_id=login.client_id, maybe_agent=login.maybe_agent
        ):
            return login.sub
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
    login = CheckedLogin(
        operator.sub,
        operator.tenant_id,
        login_name(operator),
        operator.principal_kind is PrincipalKind.AGENT,
    )
    return await _refusal_for_login(login, only_in_steps=only_in_steps)


async def _refusal_for_login(
    login: CheckedLogin, *, only_in_steps: bool
) -> PrincipalBlockedStepError | None:
    refusal = _earlier_refusal()
    if refusal is None:
        try:
            blocked = await first_blocked_step_login(
                login.sub,
                login.tenant_id,
                only_in_steps=only_in_steps,
                client_id=login.client_id,
                maybe_agent=login.maybe_agent,
            )
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
    refusal = await _refusal_for_login(login, only_in_steps=False)
    if refusal is not None:
        _log.info("principal_block_send_refused", operator_sub=login.sub)
        raise refusal


def _scope_of(operator: Operator) -> str:
    """The scope an admin's block gets: everywhere for a platform admin, else their tenant."""
    return BLOCK_SCOPE_ALL if operator.platform_admin else str(operator.tenant_id)


async def block_principal(operator: Operator, sub: str, reason: str) -> BlockedPerson:
    """Block *sub*: a login id or an agent name. The block row and its audit row commit together.

    A text that starts with ``agent:`` names an agent; every other text is
    a login id and is always stored. For an agent, the login id behind it
    is read from Keycloak and stored with the block
    (:func:`~meho_backplane.auth.principal_block_agents.agent_login_id`).
    Blocking a login that is already blocked in the same scope updates
    the reason. The row is written in one statement (insert, or update
    when the row is there). While an unblock of the login holds the row,
    the statement waits; when the unblock removed the row, the statement
    writes a new one. So a block is always written. A repeat block keeps
    the stored login id when Keycloak cannot be read at that moment.
    Raises :class:`CannotBlockSelfError` for the admin's own login, and
    :class:`UnknownLoginError` for an agent name MEHO does not know.
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
    async with get_sessionmaker()() as session:
        agent_row = await agent_of_block_text(session, operator, sub)
        login_sub = await agent_login_id(agent_row) if agent_row is not None else None
        agent = sub if agent_row is not None else None
        if agent is None:
            agent = (await agent_names(session, operator, {sub})).get(sub)
        values["login_sub"] = login_sub
        changed = {k: v for k, v in values.items() if k not in ("sub", "scope")}
        if login_sub is None:
            # Keycloak could not be read now: keep the login id stored before.
            del changed["login_sub"]
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
            method="POST",
            path="/api/v1/principals/blocked",
            payload={"blocked_sub": sub, "scope": scope, "reason": reason},
        )
        await session.commit()
    reset_block_cache()
    return BlockedPerson(sub, scope, reason, operator.sub, now, agent)


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
    the lock of the sign-in or the audit rows cannot be read. *reason* and
    *sign_in* (the proof of a fresh sign-in) go into the audit row when
    they are given. A *sign_in* that an
    ``approval.decision`` audit row already names is used up: the call
    raises :class:`SignInAlreadyUsedError` and removes nothing. The scan
    and the removal run under the advisory lock of the sign-in
    (:mod:`meho_backplane.auth.sign_in_use`). When *shown* is given (the
    :func:`block_set_digest` of the blocks the admin was shown), MEHO
    removes the blocks only when they are still exactly those blocks, and
    else raises :class:`BlockSetChangedError` and removes nothing. The
    delete also removes a block only while it is still the block that
    was read (same reason, admin and time). When one was changed in the
    meantime, MEHO removes nothing and raises :class:`BlockSetChangedError`.
    """
    tenant_scope = str(operator.tenant_id)
    own_login = sub == operator.sub
    only_this_tenant = own_login or not operator.platform_admin
    async with get_sessionmaker()() as session:
        if sign_in is not None:
            await _refuse_used_sign_in(session, operator, sign_in)
        blocks = await _read_blocks(session, sub)
        scopes = {b.scope for b in blocks}
        if own_login and (
            BLOCK_SCOPE_ALL in scopes or (not operator.platform_admin and tenant_scope in scopes)
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
            method="DELETE",
            path=f"/api/v1/principals/blocked/{sub}",
            payload=payload,
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
        agents = await agent_names(session, operator, {r.sub for r in rows})
    return [
        BlockedPerson(r.sub, r.scope, r.reason, r.blocked_by_sub, r.blocked_at, agents.get(r.sub))
        for r in rows
    ]
