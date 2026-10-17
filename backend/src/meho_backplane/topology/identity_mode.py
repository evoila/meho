# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Which node identity rules the topology writers follow (#3749).

The graph runs in one of two modes, stored in the single row of
``topology_identity_state`` (migration ``0110``):

* ``legacy`` -- the old unique key ``(tenant_id, kind, name)`` is in
  place. Writers leave ``graph_node.source_scope`` empty and a refresh
  may take over a row another target wrote. Every install starts here.
* ``scoped`` -- the operator ran the scope cut-over
  (:mod:`meho_backplane.topology.scope_repair`). The unique key is
  ``(tenant_id, source_scope, kind, name)`` and every writer sets
  ``source_scope``: a refresh writes ``target:<target id>`` (``global``
  for the target's own ``kind='target'`` node) and a manual seed writes
  ``global``.

Writers read the mode with :func:`read_identity_mode` and
``lock=True``. On PostgreSQL that is a shared row lock
(``SELECT ... FOR SHARE``) held until the writer commits. Staging and
the cut-over take an update lock on the same row first
(:func:`lock_identity_mode`), so a writer either finishes before they
start or starts after they commit. A writer never mixes the two modes,
and no refresh lands while staging collects discovery.

Readers do not need the mode. ``legacy:<uuid>`` rows only exist after
the cut-over, and readers skip them in both modes (see
:data:`NOT_LEGACY_SCOPE`).
"""

from __future__ import annotations

import uuid
from typing import Final, Literal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import QueryableAttribute

from meho_backplane.db.models import SOURCE_SCOPE_GLOBAL, GraphNode

__all__ = [
    "IDENTITY_STATE",
    "NOT_LEGACY_SCOPE",
    "IdentityMode",
    "lock_identity_mode",
    "not_legacy_scope",
    "read_identity_mode",
    "scope_for_discovered_node",
]

IdentityMode = Literal["legacy", "scoped"]

#: Core table for the single ``topology_identity_state`` row. No ORM
#: model: only this module and the cut-over touch it.
IDENTITY_STATE: Final = sa.Table(
    "topology_identity_state",
    sa.MetaData(),
    sa.Column("id", sa.Integer(), primary_key=True),
    sa.Column("mode", sa.Text(), nullable=False),
    sa.Column("manifest_id", sa.Uuid(), nullable=True),
    sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
)


def not_legacy_scope(column: QueryableAttribute[str | None]) -> sa.ColumnElement[bool]:
    """Row filter that drops ``legacy:<uuid>`` rows for *column*.

    A legacy row is a pre-cut-over row the repair could not give a
    verified source. It stays for history and repair reports, but it is
    never an answer: name lookups and graph walks skip it. ``NULL`` (not
    resolved yet, legacy mode) still passes, so nothing changes before
    the cut-over. Takes the column so aliased ``graph_node`` joins can
    use it too.
    """
    return sa.or_(column.is_(None), sa.not_(column.like("legacy:%")))


#: :func:`not_legacy_scope` for the plain :class:`GraphNode` entity.
NOT_LEGACY_SCOPE: Final = not_legacy_scope(GraphNode.source_scope)


async def read_identity_mode(session: AsyncSession, *, lock: bool = False) -> IdentityMode:
    """Return the current mode; ``legacy`` when the state row is missing.

    ``lock=True`` adds ``FOR SHARE`` (PostgreSQL; ignored on SQLite) so
    the mode cannot change before the caller's transaction ends. Call it
    inside the writer's own transaction.
    """
    stmt = sa.select(IDENTITY_STATE.c.mode).where(IDENTITY_STATE.c.id == 1)
    if lock:
        stmt = stmt.with_for_update(read=True)
    mode = (await session.execute(stmt)).scalar_one_or_none()
    return "scoped" if mode == "scoped" else "legacy"


async def lock_identity_mode(session: AsyncSession) -> str | None:
    """Take the update lock on the state row; return its raw ``mode``.

    ``None`` means the row is missing. The lock waits for every writer
    that holds the shared lock, and every new writer waits until the
    caller's transaction ends. Call it first in a transaction that must
    not overlap with graph writers (staging, cut-over).
    """
    stmt = sa.select(IDENTITY_STATE.c.mode).where(IDENTITY_STATE.c.id == 1).with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


def scope_for_discovered_node(target_id: uuid.UUID, kind: str) -> str:
    """The ``source_scope`` a target's discovery gives one node.

    The target's own node (``kind='target'``, named after the target) is
    ``global``: live target names are unique per tenant, and other
    targets' edges may point at it. Every other node a target reports is
    only known to be unique inside that target, so it is
    ``target:<target id>``.
    """
    if kind == "target":
        return SOURCE_SCOPE_GLOBAL
    return f"target:{target_id}"
