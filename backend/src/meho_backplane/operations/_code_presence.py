# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tell when the code of a connector is not in this MEHO (#5585).

A typed or composite operation row points at a Python function through
``handler_ref``. A row with no package name (``owner_package IS NULL``)
was written by an image that had the connector built in. When a later
image does not have that code, the row is still in the database. The
package rule (:func:`~meho_backplane.operations._lookup.installed_owner_clause`)
lets such a row pass, because it has no package name.

This module finds such a connector. Its code is missing when all of
these are true:

* it has at least one visible typed or composite row with a
  ``handler_ref`` and no package name;
* it has no visible row of an installed connector package;
* none of the handlers of those rows imports;
* no connector class resolves for its product and version.

Families with no class (``net.*``, ``secret.*``) are not hit: their
handlers import. A connector with a class is not hit either.

Nothing is stored. The answer is worked out in each MEHO process,
because two releases can share one database
(:mod:`~meho_backplane.operations.dead_handler_sweep`). Whether a
handler imports is kept per process (:func:`handler_imports`), so a
check does not import every handler again on each call.
"""

from __future__ import annotations

import functools
from collections.abc import Iterable
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.resolver import (
    AmbiguousConnectorResolution,
    NoMatchingConnector,
    resolve_connector,
)
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations._lookup import installed_owner_clause, visible_scope_clause
from meho_backplane.operations.dead_handler_sweep import handler_ref_resolves

__all__ = [
    "CODE_NOT_INSTALLED_ERROR",
    "code_missing_connectors",
    "connector_code_missing",
    "handler_imports",
    "result_connector_code_not_installed",
    "row_code_missing",
]

#: The stable ``error_code`` of a call or preview on such a connector.
CODE_NOT_INSTALLED_ERROR = "connector_code_not_installed"

_CODE_SHIPPED = ("typed", "composite")

ConnectorKey = tuple[UUID | None, str, str, str]


@functools.cache
def handler_imports(handler_ref: str) -> bool:
    """Return whether *handler_ref* imports in this process; the answer is kept.

    The code of a process does not change while it runs, so the answer
    is kept for the life of the process. Tests that bring code back
    call ``handler_imports.cache_clear()``.
    """
    return handler_ref_resolves(handler_ref)


def _class_resolves(product: str, version: str) -> bool:
    """Return whether a connector class resolves for *(product, version)*.

    A tie between classes counts as a class: the code is there.
    """
    target = SimpleNamespace(
        product=product, version=version, fingerprint=None, preferred_impl_id=None
    )
    try:
        resolve_connector(target)
    except NoMatchingConnector:
        return False
    except AmbiguousConnectorResolution:
        return True
    return True


def _shipped_rows() -> tuple[ColumnElement[bool], ...]:
    """Typed and composite rows with a handler that the package rule does not hide.

    A row with no ``handler_ref`` points at no code, so it says nothing
    about whether the code is here.
    """
    return (
        EndpointDescriptor.source_kind.in_(_CODE_SHIPPED),
        EndpointDescriptor.handler_ref.is_not(None),
        installed_owner_clause(),
    )


def _code_missing(product: str, version: str, rows: Iterable[Any]) -> bool:
    """Decide for one connector from its ``(handler_ref, owner_package)`` rows."""
    rows = list(rows)
    if not rows:
        return False
    if any(owner is not None for _ref, owner in rows):
        return False
    if any(ref and handler_imports(ref) for ref, _owner in rows):
        return False
    return not _class_resolves(product, version)


async def connector_code_missing(
    session: AsyncSession,
    *,
    tenant_id: UUID | None,
    product: str,
    version: str,
    impl_id: str,
) -> bool:
    """Return whether the code of the connector is not in this MEHO.

    The rows are the ones *tenant_id* can see
    (:func:`~meho_backplane.operations._lookup.visible_scope_clause`) and
    that the package rule does not hide. A first ``LIMIT 1`` read covers
    the usual case: a row with no package name whose handler imports.
    Only when that handler does not import are all the rows read.
    """
    where = (
        visible_scope_clause(
            EndpointDescriptor,
            tenant_id=tenant_id,
            product=product,
            version=version,
            impl_id=impl_id,
        ),
        EndpointDescriptor.product == product,
        EndpointDescriptor.version == version,
        EndpointDescriptor.impl_id == impl_id,
        *_shipped_rows(),
    )
    sample = (
        await session.execute(
            select(EndpointDescriptor.handler_ref)
            .where(*where, EndpointDescriptor.owner_package.is_(None))
            .limit(1)
        )
    ).first()
    if sample is None or (sample.handler_ref and handler_imports(sample.handler_ref)):
        return False
    rows = (
        await session.execute(
            select(EndpointDescriptor.handler_ref, EndpointDescriptor.owner_package)
            .where(*where)
            .distinct()
        )
    ).all()
    return _code_missing(product, version, rows)


async def row_code_missing(
    session: AsyncSession, *, tenant_id: UUID | None, row: EndpointDescriptor
) -> bool:
    """Return whether *row* belongs to a connector whose code is not in this MEHO.

    Free for the usual row: a row with a package name, an ingested row,
    or a row whose handler imports is never hit and needs no read.
    """
    if row.owner_package is not None or row.source_kind not in _CODE_SHIPPED:
        return False
    if row.handler_ref is None or handler_imports(row.handler_ref):
        return False
    return await connector_code_missing(
        session,
        tenant_id=tenant_id,
        product=row.product,
        version=row.version,
        impl_id=row.impl_id,
    )


async def code_missing_connectors(
    session: AsyncSession, *, operator_tenant_id: UUID
) -> set[ConnectorKey]:
    """Return the ``(tenant_id, product, version, impl_id)`` keys whose code is missing.

    For the connector list, keyed like its other counts: built-in rows and
    the operator's own tenant rows. One grouped read gives one handler
    with no package name per connector and the number of package rows. A
    connector is read in full only when that handler does not import.
    """
    no_package_ref = func.min(
        case((EndpointDescriptor.owner_package.is_(None), EndpointDescriptor.handler_ref))
    )
    key_columns = (
        EndpointDescriptor.tenant_id,
        EndpointDescriptor.product,
        EndpointDescriptor.version,
        EndpointDescriptor.impl_id,
    )
    tenant_rows = (EndpointDescriptor.tenant_id.is_(None)) | (
        EndpointDescriptor.tenant_id == operator_tenant_id
    )
    shipped = _shipped_rows()
    summary = await session.execute(
        select(*key_columns, no_package_ref, func.count(EndpointDescriptor.owner_package))
        .where(tenant_rows, *shipped)
        .group_by(*key_columns)
    )
    missing: set[ConnectorKey] = set()
    for tenant_uuid, product, version, impl_id, ref, package_rows in summary.all():
        if ref is None or package_rows or handler_imports(ref):
            continue
        rows = await session.execute(
            select(EndpointDescriptor.handler_ref, EndpointDescriptor.owner_package)
            .where(
                EndpointDescriptor.tenant_id.is_(None)
                if tenant_uuid is None
                else EndpointDescriptor.tenant_id == tenant_uuid,
                EndpointDescriptor.product == product,
                EndpointDescriptor.version == version,
                EndpointDescriptor.impl_id == impl_id,
                *shipped,
            )
            .distinct()
        )
        if _code_missing(product, version, rows.all()):
            missing.add((tenant_uuid, product, version, impl_id))
    return missing


def result_connector_code_not_installed(
    op_id: str, connector_id: str, duration_ms: float
) -> OperationResult:
    """The op belongs to a connector whose code is not in this MEHO (#5585).

    The row has no package name, so the text cannot name a package. The
    row stays in the database and works again when the code is back.
    """
    return OperationResult(
        status="error",
        op_id=op_id,
        error=(
            f"{CODE_NOT_INSTALLED_ERROR}: the code of connector {connector_id!r} is not "
            f"in this MEHO, so operation {op_id!r} cannot run. Its operations were "
            "written by a MEHO that had this connector built in. Run a MEHO image that "
            "has this connector, or install the connector package that ships it."
        ),
        duration_ms=duration_ms,
        extras={"error_code": CODE_NOT_INSTALLED_ERROR, "connector_id": connector_id},
    )
