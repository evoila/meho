# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Remove built-in typed/composite rows whose handler no longer exists.

Typed and composite ops are code-shipped. Their ``endpoint_descriptor``
row points at a Python function through ``handler_ref``. When a release
removes or renames that function, the registrars stop writing the row,
but the row from the older release stays in the database. Discovery
(``search_operations`` / ``list_operation_groups``) keeps advertising it,
and every call fails with ``handler_unreachable``.

:func:`retire_dead_handler_rows` runs at boot, right after every
registrar has written its rows. It deletes each built-in
(``tenant_id IS NULL``) ``typed`` / ``composite`` row whose
``handler_ref`` does not resolve through the dispatcher's own import walk
(:func:`~meho_backplane.operations._handler_resolve.import_handler`).
Rows whose handler resolves are not touched, so operator edits on them
(``custom_description``, ``is_enabled``, safety level) stay as they are.

Why delete and not disable: the registrars own built-in typed/composite
rows. If a handler comes back in a later release, its registrar writes a
fresh row. A disabled row would stay disabled, because the registrar
upsert does not change ``is_enabled``.

Ingested rows (``handler_ref IS NULL``, dispatched over HTTP) and
tenant-scoped rows are out of scope.
"""

from __future__ import annotations

import structlog
from sqlalchemy import delete, select

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations._handler_resolve import import_handler

__all__ = ["handler_ref_resolves", "retire_dead_handler_rows"]

_log = structlog.get_logger(__name__)


def handler_ref_resolves(handler_ref: str | None) -> bool:
    """Return True when *handler_ref* resolves to a callable the dispatcher can run."""
    if not handler_ref:
        return False
    try:
        import_handler(handler_ref)
    except (ImportError, TypeError):
        return False
    return True


async def retire_dead_handler_rows() -> list[str]:
    """Delete built-in typed/composite rows whose handler cannot be imported.

    Returns the retired ``op_id`` values, sorted, so callers and tests can
    see what was removed. Logs one ``dead_handler_op_retired`` warning per
    row with the connector key and the dead ``handler_ref``.
    """
    async with get_sessionmaker()() as session:
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
        dead = [row for row in rows if not handler_ref_resolves(row.handler_ref)]
        if not dead:
            return []
        await session.execute(
            delete(EndpointDescriptor).where(EndpointDescriptor.id.in_([row.id for row in dead]))
        )
        await session.commit()

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
