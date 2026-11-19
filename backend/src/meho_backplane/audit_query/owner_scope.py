# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Which audit rows a caller may read: a service login reads only its own.

A person reads every audit row of the tenant. A service login (an agent, a
runner, an add-on, any client-credentials login) reads only the rows of its
own calls: the rows whose ``operator_sub`` is the login's own ``sub``. The
rule holds on every audit read (list, count, filter, single row, session
tree), whatever role the login carries.

* :func:`own_rows_sub` -- the ``sub`` a caller's audit reads are limited
  to, or ``None`` for a person.
* :func:`session_has_rows_of` -- whether a session holds a row of one
  login. The ``shape="tree"`` replay of ``query_audit`` uses it for a
  person: the session must hold a row of the person's own calls. The check
  reads the stored rows; it never trusts a session id the client sends in
  a header. A service login needs no such check: its tree holds only its
  own rows, so the tree of a session of another login is empty, the same
  as for a session that does not exist.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, is_human_principal
from meho_backplane.db.models import AuditLog

__all__ = ["own_rows_sub", "session_has_rows_of"]


def own_rows_sub(operator: Operator) -> str | None:
    """The login id *operator*'s audit reads are limited to, or ``None``.

    ``None`` for a person (``principal_kind=user``): a person reads every
    row of the tenant. The login's own ``sub`` for every machine kind
    (agent, service, runner), so the read returns only its own rows.
    """
    if is_human_principal(operator):
        return None
    return operator.sub


async def session_has_rows_of(
    session_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID,
    principal_sub: str,
    session: AsyncSession,
) -> bool:
    """``True`` when the stored rows of the session hold a row of *principal_sub*."""
    stmt = sa.select(
        sa.exists().where(
            AuditLog.agent_session_id == session_id,
            AuditLog.tenant_id == tenant_id,
            AuditLog.operator_sub == principal_sub,
        ),
    )
    return bool(await session.scalar(stmt))
