# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed and multi-step operations first in discovery, behind a switch (#4712).

A connector can hold two kinds of operations in the same groups:

* typed and multi-step operations (``source_kind`` ``typed`` and
  ``composite``), written in code, with a checked risk level and a summary;
* raw operations (``source_kind`` ``ingested``), made from a vendor API
  description. Many have no summary, and their risk level is guessed from
  the HTTP method.

On a big connector the raw operations outnumber the typed ones many times,
so a search often finds a raw call first, even when a safer typed or
multi-step operation exists.

The switch ``SEARCH_TYPED_FIRST``
(:attr:`~meho_backplane.settings.Settings.search_typed_first`, off by
default) changes what ``search_operations`` and ``list_operation_groups``
show:

* By default, only typed and multi-step operations. The caller asks for
  raw operations with ``include_raw``.
* A connector with no typed or multi-step operation shows its raw
  operations without the flag, with a note that says why.
* A search whose typed hits are not a clear match says how many raw
  operations would also match, so the caller is not stuck.

Only what discovery shows changes. Exact-op mode (``op_id``), calling,
policy and approval are the same: a raw operation can still be called by
its id. With the switch off, both tools answer as before, and
``include_raw`` changes nothing.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any, Final

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.models import EndpointDescriptor, OperationGroup
from meho_backplane.operations._lookup import (
    enabled_for_tenant_clause,
    installed_owner_clause,
    visible_scope_clause,
)
from meho_backplane.settings import get_settings

__all__ = [
    "INCLUDE_RAW_LINE",
    "TYPED_KINDS",
    "connector_has_typed_operations",
    "group_has_typed_operation_clause",
    "groups_raw_block",
    "raw_op_count",
    "search_raw_block",
    "search_typed_first",
    "typed_only_for_caller",
]

#: The ``source_kind`` values search shows by default while the switch is on.
TYPED_KINDS: Final[tuple[str, ...]] = ("typed", "composite")

#: The line the two tool descriptions end with. It names the flag.
INCLUDE_RAW_LINE: Final[str] = (
    " By default you see only typed and multi-step operations; set "
    "`include_raw: true` to also see raw operations made from the vendor "
    "API description."
)

_NO_TYPED_NOTE: Final[str] = (
    "This connector has no typed or multi-step operations, so raw operations "
    "(made from the vendor API description) are shown."
)


def search_typed_first() -> bool:
    """Return the state of the ``SEARCH_TYPED_FIRST`` switch."""
    return get_settings().search_typed_first


def _typed_row_clause(tenant_id: uuid.UUID) -> tuple[ColumnElement[bool], ...]:
    """The rows a typed-first search can show: typed, on for the tenant, installed."""
    return (
        EndpointDescriptor.source_kind.in_(TYPED_KINDS),
        enabled_for_tenant_clause(tenant_id=tenant_id),
        installed_owner_clause(),
    )


async def connector_has_typed_operations(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    product: str,
    version: str,
    impl_id: str,
) -> bool:
    """Return ``True`` when the caller can see a typed or multi-step operation.

    Same tenant scope, enabled rule and package rule as the search, so the
    answer matches what a typed-first search can return.
    """
    stmt = select(
        select(EndpointDescriptor.id)
        .where(
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
            *_typed_row_clause(tenant_id),
        )
        .exists()
    )
    return bool((await session.execute(stmt)).scalar())


async def typed_only_for_caller(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    product: str,
    version: str,
    impl_id: str,
    include_raw: bool,
) -> tuple[bool, bool] | None:
    """Return ``(typed_only, has_typed)`` for one discovery call, or ``None``.

    ``None`` while the switch is off: the call runs as before. Else
    *typed_only* is ``True`` when raw operations are left out: the caller
    did not set *include_raw*, and the connector has a typed operation
    (*has_typed*).
    """
    if not search_typed_first():
        return None
    has_typed = await connector_has_typed_operations(
        session, tenant_id=tenant_id, product=product, version=version, impl_id=impl_id
    )
    return has_typed and not include_raw, has_typed


def group_has_typed_operation_clause(tenant_id: uuid.UUID) -> ColumnElement[bool]:
    """Return the predicate "this group holds a typed operation the tenant can use"."""
    return (
        select(EndpointDescriptor.id)
        .where(EndpointDescriptor.group_id == OperationGroup.id, *_typed_row_clause(tenant_id))
        .exists()
    )


async def raw_op_count(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    product: str,
    version: str,
    impl_id: str,
    op_ids: Sequence[str],
) -> int:
    """Count the raw operations among *op_ids*, in the caller's tenant scope."""
    if not op_ids:
        return 0
    stmt = select(func.count(EndpointDescriptor.id)).where(
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
        EndpointDescriptor.op_id.in_(list(op_ids)),
        EndpointDescriptor.source_kind.not_in(TYPED_KINDS),
    )
    return int((await session.execute(stmt)).scalar() or 0)


def search_raw_block(*, included: bool, has_typed: bool, hidden_matches: int) -> dict[str, Any]:
    """Return the ``raw_operations`` part of a ranked search answer.

    *included* is ``True`` when raw operations were searched: the caller
    asked, or the connector has no typed operation (*has_typed* ``False``).
    *hidden_matches* counts the raw operations that would also be hits.
    """
    note: str | None = None
    if not has_typed:
        note = _NO_TYPED_NOTE
    elif hidden_matches > 0:
        note = (
            f"{hidden_matches} raw operation(s) made from the vendor API description "
            "also match. They are hidden by default. Search again with "
            "`include_raw: true` to see them."
        )
    return {"included": included, "hidden_matches": hidden_matches, "note": note}


def groups_raw_block(*, included: bool, has_typed: bool, hidden_groups: int) -> dict[str, Any]:
    """Return the ``raw_operations`` part of a group listing.

    *hidden_groups* counts the groups left out because they hold no typed
    or multi-step operation.
    """
    note: str | None = None
    if not has_typed:
        note = _NO_TYPED_NOTE
    elif hidden_groups > 0:
        note = (
            f"{hidden_groups} group(s) hold only raw operations made from the "
            "vendor API description. They are hidden by default. Call again with "
            "`include_raw: true` to see them."
        )
    return {"included": included, "hidden_groups": hidden_groups, "note": note}
