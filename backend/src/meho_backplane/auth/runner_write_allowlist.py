# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Per-runner remote-write capability allowlist — grant / list / load (#3190).

Mechanism 2 of the satellite write-path composed gate (Initiative #2901,
design ``docs/research/2901-satellite-write-path.md`` §3, decision
``docs/decisions/satellite-write-path.md``). The companion of the runner
principal (:mod:`meho_backplane.auth.runner_principals`): a runner principal is
an *identity*; the rows this service manages are that identity's **write blast
radius** — the enumerated ``(op_pattern, target_scope)`` capabilities it may
ride as remote writes.

Two provisioning methods (operator-facing, over the ``tenant_admin``-gated
REST route) plus one mint-side reader:

* :meth:`RunnerWriteAllowlistService.grant` — the **separate human step** a
  write capability requires. Enrollment (``RunnerPrincipalService.register``)
  grants **nothing**; a write capability is added only here, by a human
  operator, recording ``created_by_sub`` as the issuance binding (decision
  recommendation 2 / threat T7: programmatic enrollment can never grant write
  capability at birth, and a runner's read-only, route-caged token cannot reach
  this path — so the runner cannot widen its own allowlist).
* :meth:`RunnerWriteAllowlistService.list_` — the operator read-back.
* :func:`load_runner_allowlist` — the central mint reads a runner's rows into
  the shared, DB-free :class:`~meho_backplane.runner.satellite_tier.RemoteWriteAllowEntry`
  matcher input.

Idempotent grant: the unique ``(runner_principal_id, op_pattern,
target_scope)`` index makes a re-grant of the same capability return the
existing row rather than duplicate or error.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import structlog
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.runner_principals import RunnerPrincipalNotFoundError
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import RunnerPrincipal, RunnerWriteAllowlistEntry
from meho_backplane.runner.satellite_tier import RemoteWriteAllowEntry

__all__ = [
    "RemoteWriteCapabilityGrant",
    "RunnerWriteAllowlistEntryRead",
    "RunnerWriteAllowlistService",
    "load_runner_allowlist",
]

#: Bounds on a granted pattern/scope. An ``op_pattern`` is a dotted op-glob and
#: a ``target_scope`` is ``*`` or a uuid string; neither is ever near this cap,
#: which exists only to reject pathological input at the API boundary.
_FIELD_MAX_LENGTH = 512


def _is_unique_violation(exc: IntegrityError) -> bool:
    """Return whether *exc* is a unique-constraint violation (Postgres / SQLite)."""
    orig = getattr(exc, "orig", None)
    sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    return sqlstate == "23505" or "UNIQUE constraint failed" in str(orig or exc)


class RemoteWriteCapabilityGrant(BaseModel):
    """Input shape for :meth:`RunnerWriteAllowlistService.grant`."""

    model_config = ConfigDict(extra="forbid")

    #: A glob over ``op_id`` (``fnmatch``): an exact op-class for a minimal
    #: Stage-1 allowlist, or a ``*`` prefix.
    op_pattern: str = Field(min_length=1, max_length=_FIELD_MAX_LENGTH)
    #: The target-scope cap: ``*`` (any target in the tenant) or a concrete
    #: ``str(target.id)``. Defaults to ``*`` for ergonomics; the operator
    #: narrows it to bound the blast radius.
    target_scope: str = Field(default="*", min_length=1, max_length=_FIELD_MAX_LENGTH)


class RunnerWriteAllowlistEntryRead(BaseModel):
    """One granted remote-write capability, as returned by the accessors."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    runner_principal_id: uuid.UUID
    op_pattern: str
    target_scope: str
    created_by_sub: str
    created_at: datetime


class RunnerWriteAllowlistService:
    """Tenant-scoped grant / list of a runner principal's write capabilities.

    Stateless; instantiate per request and call freely.
    """

    def __init__(self) -> None:
        self._log = structlog.get_logger()

    async def _resolve_runner_id(
        self, session: AsyncSession, *, tenant_id: uuid.UUID, runner_name: str
    ) -> uuid.UUID:
        """Return the (non-revoked) runner principal id for ``(tenant, name)``.

        Raises :class:`RunnerPrincipalNotFoundError` when no live principal
        matches — a revoked or unknown runner gets no new write capability, and
        the lookup adds no existence oracle beyond the name the operator already
        supplied.
        """
        runner_pk = await session.scalar(
            select(RunnerPrincipal.id).where(
                RunnerPrincipal.tenant_id == tenant_id,
                RunnerPrincipal.name == runner_name,
                RunnerPrincipal.revoked.is_(False),
            )
        )
        if runner_pk is None:
            raise RunnerPrincipalNotFoundError(runner_name)
        return runner_pk

    async def grant(
        self,
        tenant_id: uuid.UUID,
        runner_name: str,
        created_by_sub: str,
        payload: RemoteWriteCapabilityGrant,
    ) -> RunnerWriteAllowlistEntryRead:
        """Grant one remote-write capability to a runner (the human step).

        Resolves the live runner principal, inserts one
        ``runner_write_allowlist`` row, and returns it. Idempotent: a re-grant
        of the same ``(op_pattern, target_scope)`` returns the existing row
        (the unique index absorbs the duplicate) rather than erroring.

        Raises
        ------
        RunnerPrincipalNotFoundError
            No live runner principal matches ``(tenant_id, runner_name)``.
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            runner_principal_id = await self._resolve_runner_id(
                session, tenant_id=tenant_id, runner_name=runner_name
            )
            row = RunnerWriteAllowlistEntry(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                runner_principal_id=runner_principal_id,
                op_pattern=payload.op_pattern,
                target_scope=payload.target_scope,
                created_by_sub=created_by_sub,
            )
            session.add(row)
            try:
                await session.flush()
            except IntegrityError as exc:
                await session.rollback()
                if not _is_unique_violation(exc):
                    raise
                existing = await self._get_entry(
                    session,
                    runner_principal_id=runner_principal_id,
                    op_pattern=payload.op_pattern,
                    target_scope=payload.target_scope,
                )
                self._log.info(
                    "runner_write_allowlist_grant_idempotent",
                    tenant_id=str(tenant_id),
                    runner=runner_name,
                    op_pattern=payload.op_pattern,
                    target_scope=payload.target_scope,
                )
                return existing
            await session.refresh(row)
            entry = RunnerWriteAllowlistEntryRead.model_validate(row)
            await session.commit()
        self._log.info(
            "runner_write_allowlist_granted",
            tenant_id=str(tenant_id),
            runner=runner_name,
            op_pattern=payload.op_pattern,
            target_scope=payload.target_scope,
            created_by_sub=created_by_sub,
        )
        return entry

    async def _get_entry(
        self,
        session: AsyncSession,
        *,
        runner_principal_id: uuid.UUID,
        op_pattern: str,
        target_scope: str,
    ) -> RunnerWriteAllowlistEntryRead:
        """Fetch the single row for a fully-scoped capability (idempotent-grant path)."""
        row = (
            await session.execute(
                select(RunnerWriteAllowlistEntry).where(
                    RunnerWriteAllowlistEntry.runner_principal_id == runner_principal_id,
                    RunnerWriteAllowlistEntry.op_pattern == op_pattern,
                    RunnerWriteAllowlistEntry.target_scope == target_scope,
                )
            )
        ).scalar_one()
        return RunnerWriteAllowlistEntryRead.model_validate(row)

    async def list_(
        self,
        tenant_id: uuid.UUID,
        runner_name: str,
    ) -> list[RunnerWriteAllowlistEntryRead]:
        """Return a runner's granted capabilities, op-sorted.

        Raises :class:`RunnerPrincipalNotFoundError` when the runner is unknown
        or revoked in this tenant (so listing and granting agree on existence).
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            runner_principal_id = await self._resolve_runner_id(
                session, tenant_id=tenant_id, runner_name=runner_name
            )
            rows = (
                (
                    await session.execute(
                        select(RunnerWriteAllowlistEntry)
                        .where(RunnerWriteAllowlistEntry.runner_principal_id == runner_principal_id)
                        .order_by(
                            RunnerWriteAllowlistEntry.op_pattern,
                            RunnerWriteAllowlistEntry.target_scope,
                        )
                    )
                )
                .scalars()
                .all()
            )
        return [RunnerWriteAllowlistEntryRead.model_validate(row) for row in rows]


async def load_runner_allowlist(
    session: AsyncSession, *, tenant_id: uuid.UUID, runner_name: str
) -> tuple[RemoteWriteAllowEntry, ...]:
    """Read a runner's remote-write allowlist for the central mint (#3190).

    One indexed join on the ``(tenant_id, name)`` runner row → its capability
    rows, projected into the shared, DB-free
    :class:`~meho_backplane.runner.satellite_tier.RemoteWriteAllowEntry` the mint
    feeds to :func:`~meho_backplane.runner.satellite_tier.evaluate_remote_write_gate`
    — the *same* matcher the edge re-runs against its own config mirror. Runs in
    the caller's open mint session. An unknown runner (no row) yields the empty
    tuple, so the gate fails closed (unprovisioned) exactly as for a runner with
    no granted capability.
    """
    rows = (
        await session.execute(
            select(
                RunnerWriteAllowlistEntry.op_pattern,
                RunnerWriteAllowlistEntry.target_scope,
            )
            .join(
                RunnerPrincipal,
                RunnerWriteAllowlistEntry.runner_principal_id == RunnerPrincipal.id,
            )
            .where(
                RunnerPrincipal.tenant_id == tenant_id,
                RunnerPrincipal.name == runner_name,
                RunnerWriteAllowlistEntry.tenant_id == tenant_id,
            )
        )
    ).all()
    return tuple(
        RemoteWriteAllowEntry(op_pattern=op_pattern, target_scope=target_scope)
        for op_pattern, target_scope in rows
    )
