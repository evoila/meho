# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Service-principal read scope: store it, and check it at call time (#4992).

A service principal (a machine login with its own secret) runs every
``safe`` operation at once, on every target of its tenant. A standing
grant can only let it do more. The read scope is the one thing that lets
a person narrow its reads: a list of targets (ids or selectors) plus an
optional "no target" entry.

* :class:`ServiceReadScopeService` -- set / get / clear, one row per
  ``(tenant, principal_sub)`` in
  :class:`~meho_backplane.db.models.ServicePrincipalReadScope`.
* :func:`read_scope_denial` -- the call-time check the policy gate runs
  (:func:`meho_backplane.operations._validate._non_agent_verdict`) for a
  service principal on a call that would otherwise run at once. Returns
  the deny reason, which starts with the stable error code
  ``read_scope_denied``, or ``None`` when the call may run.

The check never writes anything, so the preview path may run it too.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from typing import Any

import structlog
from sqlalchemy import select

from meho_backplane.auth.operator import Operator
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ServicePrincipalReadScope
from meho_backplane.operations._errors import READ_SCOPE_DENIED_CODE
from meho_backplane.operations.service_read_scope_schemas import (
    ReadScopeEntry,
    ServiceReadScopeRead,
    ServiceReadScopeSet,
)

__all__ = [
    "ReadScopeValidationError",
    "ServiceReadScopeService",
    "read_scope_denial",
    "stored_entries",
]

_log = structlog.get_logger(__name__)

#: How a person changes the scope; named in every deny reason.
_HOW_TO_CHANGE = "a person can change it with `meho service-principals read-scope set`"


class ReadScopeValidationError(Exception):
    """The request is well-formed but not allowed (maps to HTTP 422)."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


def stored_entries(payload: ServiceReadScopeSet) -> list[dict[str, object]]:
    """The entries as JSON-ready dicts, the form the row and the audit row store."""
    return [entry.model_dump(mode="json", exclude_defaults=True) for entry in payload.entries]


def _read_entry(row: ServicePrincipalReadScope) -> ServiceReadScopeRead:
    return ServiceReadScopeRead(
        tenant_id=row.tenant_id,
        principal_sub=row.principal_sub,
        entries=[ReadScopeEntry.model_validate(entry) for entry in row.entries],
        set_by_sub=row.set_by_sub,
        set_at=row.set_at,
    )


class ServiceReadScopeService:
    """Tenant-scoped set / get / clear of the read scope.

    Stateless and async; each method opens its own session. Callers own
    the role and human-principal gates; the service enforces neither.
    """

    async def set(
        self,
        tenant_id: uuid.UUID,
        principal_sub: str,
        set_by_sub: str,
        payload: ServiceReadScopeSet,
    ) -> ServiceReadScopeRead:
        """Replace the scope of *principal_sub* (insert or update one row).

        ``principal_sub`` must be one exact value: a ``*`` or ``?`` is
        refused, the same rule a standing grant applies.
        """
        if "*" in principal_sub or "?" in principal_sub:
            raise ReadScopeValidationError(
                f"principal_sub must be an exact value; wildcards are not permitted "
                f"(got {principal_sub!r})"
            )
        entries = stored_entries(payload)
        now = datetime.now(UTC)
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            row = await session.scalar(
                select(ServicePrincipalReadScope).where(
                    ServicePrincipalReadScope.tenant_id == tenant_id,
                    ServicePrincipalReadScope.principal_sub == principal_sub,
                )
            )
            if row is None:
                row = ServicePrincipalReadScope(
                    tenant_id=tenant_id,
                    principal_sub=principal_sub,
                    entries=entries,
                    set_by_sub=set_by_sub,
                    set_at=now,
                )
                session.add(row)
            else:
                row.entries = entries
                row.set_by_sub = set_by_sub
                row.set_at = now
            await session.commit()
            await session.refresh(row)
            entry = _read_entry(row)
        _log.info(
            "service_read_scope_set",
            tenant_id=str(tenant_id),
            principal_sub=principal_sub,
            entry_count=len(entries),
            set_by_sub=set_by_sub,
        )
        return entry

    async def get(self, tenant_id: uuid.UUID, principal_sub: str) -> ServiceReadScopeRead | None:
        """Return the scope of *principal_sub*, or ``None`` when none is set."""
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            row = await session.scalar(
                select(ServicePrincipalReadScope).where(
                    ServicePrincipalReadScope.tenant_id == tenant_id,
                    ServicePrincipalReadScope.principal_sub == principal_sub,
                )
            )
            return None if row is None else _read_entry(row)

    async def clear(self, tenant_id: uuid.UUID, principal_sub: str, cleared_by_sub: str) -> bool:
        """Delete the scope of *principal_sub*. ``False`` when none was set."""
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            row = await session.scalar(
                select(ServicePrincipalReadScope).where(
                    ServicePrincipalReadScope.tenant_id == tenant_id,
                    ServicePrincipalReadScope.principal_sub == principal_sub,
                )
            )
            cleared = row is not None
            if row is not None:
                await session.delete(row)
                await session.commit()
        _log.info(
            "service_read_scope_cleared",
            tenant_id=str(tenant_id),
            principal_sub=principal_sub,
            cleared=cleared,
            cleared_by_sub=cleared_by_sub,
        )
        return cleared


# ---------------------------------------------------------------------------
# Enforcement (call-time)
# ---------------------------------------------------------------------------


def _entry_matches(
    entry: ReadScopeEntry,
    *,
    target_id: uuid.UUID | None,
    target_product: str | None,
    target_name: str | None,
) -> bool:
    """Whether one scope entry covers a target (never the no-target case)."""
    if entry.no_target:
        return False
    if entry.target_id is not None:
        return entry.target_id == target_id
    product_ok = entry.target_product is None or entry.target_product == target_product
    name_ok = entry.target_name_pattern is None or (
        target_name is not None and fnmatchcase(target_name, entry.target_name_pattern)
    )
    return product_ok and name_ok


def _target_fields(target: Any) -> tuple[uuid.UUID | None, str | None, str | None]:
    """``(id, product, name)`` of a call target, each ``None`` when absent."""
    raw_id = getattr(target, "id", None)
    product = getattr(target, "product", None)
    name = getattr(target, "name", None)
    return (
        raw_id if isinstance(raw_id, uuid.UUID) else None,
        product if isinstance(product, str) else None,
        name if isinstance(name, str) else None,
    )


async def read_scope_denial(*, operator: Operator, target: Any) -> str | None:
    """Return the deny reason when the caller's read scope does not cover *target*.

    ``None`` when the principal has no scope, or when an entry covers the
    target (or, for a call without a target, when the scope has a
    ``no_target`` entry). The reason starts with ``read_scope_denied:`` so
    :func:`meho_backplane.operations._errors.result_denied` reports that
    stable error code. Reads one row; writes nothing.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = await session.scalar(
            select(ServicePrincipalReadScope).where(
                ServicePrincipalReadScope.tenant_id == operator.tenant_id,
                ServicePrincipalReadScope.principal_sub == operator.sub,
            )
        )
        if row is None:
            return None
        entries = [ReadScopeEntry.model_validate(entry) for entry in row.entries]

    if target is None:
        if any(entry.no_target for entry in entries):
            return None
        return (
            f"{READ_SCOPE_DENIED_CODE}: this call has no target, and the read scope of "
            f"service principal {operator.sub!r} has no 'no target' entry; {_HOW_TO_CHANGE}"
        )

    target_id, target_product, target_name = _target_fields(target)
    if any(
        _entry_matches(
            entry, target_id=target_id, target_product=target_product, target_name=target_name
        )
        for entry in entries
    ):
        return None
    return (
        f"{READ_SCOPE_DENIED_CODE}: target {target_name!r} ({target_id}) is outside the read "
        f"scope of service principal {operator.sub!r}; {_HOW_TO_CHANGE}"
    )
