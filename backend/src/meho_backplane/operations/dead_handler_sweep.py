# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Remove built-in typed/composite rows for ops this code base retired.

Typed and composite ops are code-shipped. Their ``endpoint_descriptor``
row points at a Python function through ``handler_ref``. When a release
removes or renames that function, the registrars stop writing the row,
but the row from the older release stays in the database. Discovery
(``search_operations`` / ``list_operation_groups``) keeps advertising it,
and every call fails with ``handler_unreachable``.

:func:`retire_dead_handler_rows` runs at boot, right after every
registrar has written its rows. It deletes each built-in
(``tenant_id IS NULL``) ``typed`` / ``composite`` row that is listed in
:data:`RETIRED_OPS`. A release that removes a handler adds the op to that
list in the same change.

Why an explicit list and not "the handler does not import": two releases
can share one database during a rolling upgrade. On an older server, the
handlers of ops the newer release added do not import either. Deleting
on that signal alone would remove valid rows, and the newer server would
write them back with ``is_enabled=True``, losing an operator's choice to
disable them. The list makes the older server keep every op it does not
know about. Rows whose handler does not import and that are not listed
are kept and logged as ``dead_handler_op_kept``.

Why delete and not disable: the registrars own built-in typed/composite
rows and no registrar writes a retired op anymore. A retired ``op_id`` is
not reused by a later release.

Rows whose handler resolves are not touched, so operator edits on them
(``custom_description``, ``is_enabled``, safety level) stay as they are.
Ingested rows (``handler_ref IS NULL``, dispatched over HTTP) and
tenant-scoped rows are out of scope.
"""

from __future__ import annotations

from typing import Any, NamedTuple

import structlog
from sqlalchemy import Row, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations._handler_resolve import import_handler

__all__ = ["RETIRED_OPS", "RetiredOp", "handler_ref_resolves", "retire_dead_handler_rows"]

_log = structlog.get_logger(__name__)


class RetiredOp(NamedTuple):
    """Natural key of a built-in op that no release ships anymore."""

    product: str
    version: str
    impl_id: str
    op_id: str


RETIRED_OPS: frozenset[RetiredOp] = frozenset(
    {
        # Re-shipped as the typed ops vmware.host.network_uplinks and
        # vmware.host.vsan_health; the composite handlers were removed.
        RetiredOp("vmware", "9.0", "vmware-rest", "vmware.composite.host.network_uplinks"),
        RetiredOp("vmware", "9.0", "vmware-rest", "vmware.composite.host.vsan_health"),
    }
)
"""Built-in typed/composite ops the sweep deletes at boot.

Add an entry when a change removes a typed or composite handler for good.
Never remove an entry to bring an op back under the same ``op_id``; pick a
new ``op_id`` instead, so an older server still running the old list
cannot delete the new row.
"""


def handler_ref_resolves(handler_ref: str | None) -> bool:
    """Return True when *handler_ref* resolves to a callable the dispatcher can run."""
    if not handler_ref:
        return False
    try:
        import_handler(handler_ref)
    except (ImportError, TypeError):
        return False
    return True


async def retire_dead_handler_rows(session: AsyncSession | None = None) -> list[str]:
    """Delete built-in typed/composite rows listed in :data:`RETIRED_OPS`.

    Returns the retired ``op_id`` values, sorted, so callers and tests can
    see what was removed. Logs one ``dead_handler_op_retired`` warning per
    deleted row. A row whose handler does not import but that is not
    listed is kept and logged once as ``dead_handler_op_kept``: it is
    either an op a newer release owns or a retirement missing from the
    list.

    With *session*, the delete runs in that transaction and the caller
    commits (the start-up registration does, #5019). Without it, the
    function opens its own session and commits.
    """
    if session is not None:
        dead = await _delete_retired_rows(session)
    else:
        async with get_sessionmaker()() as owned_session:
            dead = await _delete_retired_rows(owned_session)
            if dead:
                await owned_session.commit()

    for row in dead:
        _log.warning(
            "dead_handler_op_retired",
            product=row.product,
            version=row.version,
            impl_id=row.impl_id,
            op_id=row.op_id,
            handler_ref=row.handler_ref,
        )
    return sorted(row.op_id for row in dead)


async def _delete_retired_rows(session: AsyncSession) -> list[Row[Any]]:
    """Delete the listed rows in *session* without committing; return them."""
    rows = (
        await session.execute(
            select(
                EndpointDescriptor.id,
                EndpointDescriptor.product,
                EndpointDescriptor.version,
                EndpointDescriptor.impl_id,
                EndpointDescriptor.op_id,
                EndpointDescriptor.handler_ref,
            ).where(
                EndpointDescriptor.tenant_id.is_(None),
                EndpointDescriptor.source_kind.in_(("typed", "composite")),
            )
        )
    ).all()
    dead = []
    for row in rows:
        key = RetiredOp(row.product, row.version, row.impl_id, row.op_id)
        if key in RETIRED_OPS:
            dead.append(row)
        elif not handler_ref_resolves(row.handler_ref):
            _log.warning(
                "dead_handler_op_kept",
                product=row.product,
                version=row.version,
                impl_id=row.impl_id,
                op_id=row.op_id,
                handler_ref=row.handler_ref,
            )
    if dead:
        await session.execute(
            delete(EndpointDescriptor).where(EndpointDescriptor.id.in_([row.id for row in dead]))
        )
    return dead
