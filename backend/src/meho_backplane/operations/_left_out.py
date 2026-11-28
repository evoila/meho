# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The operations that registration left out in this start, kept in memory.

Registration leaves an operation out when a mark does not fit its risk
level (``_MARK_FIT_CHECKS`` in :mod:`meho_backplane.operations.typed_register`).
MEHO then writes and deletes nothing for it. A row that an earlier start
stored under that key stays as it is, with the operator's settings on it.

Instead, the start keeps the key of each left-out operation here: the
connector triple and op id. For a left-out key, every stored built-in
typed or multi-step row under that key is hidden, unless a registrar wrote
that key in this same start (:func:`without_written_keys`). A tenant's row
and an imported row are not touched. The lookups hide the row
(:func:`left_out_row_clause`, applied by
:func:`~meho_backplane.operations._lookup.installed_owner_clause`), and a
call answers ``operation_left_out``.

A start collects its keys while it runs (:func:`collect_left_out_ops`) and
publishes them before anything can call an operation
(:func:`publish_left_out_ops`). So a later start without the fault shows
the stored row again. With no left-out operation, every clause here is
true and changes no answer.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, NamedTuple

import sqlalchemy as sa
from sqlalchemy import ColumnElement, and_, or_

from meho_backplane.db.models import EndpointDescriptor

__all__ = [
    "LeftOutOp",
    "collect_left_out_ops",
    "left_out_op",
    "left_out_ops",
    "left_out_row_clause",
    "left_out_sql_keys",
    "note_left_out_op",
    "note_registered_op",
    "publish_left_out_ops",
    "row_is_left_out",
    "without_written_keys",
]

#: The kinds of row that registration writes; only these rows are hidden.
_REGISTERED_KINDS = ("typed", "composite")

#: Separator of the parts of one key in :func:`left_out_sql_keys`; the
#: raw-SQL twin in :mod:`meho_backplane.operations._search` joins the row's
#: columns with the same character (``chr(31)``).
_SQL_KEY_SEPARATOR = "\x1f"


class LeftOutOp(NamedTuple):
    """One operation that registration left out, with the package of its code.

    The key is the connector triple and op id; ``owner_package`` only names
    the package in the log line and the call's answer.
    """

    product: str
    version: str
    impl_id: str
    op_id: str
    owner_package: str | None


_LEFT_OUT: frozenset[LeftOutOp] = frozenset()

_LEFT_OUT_IN_START: ContextVar[set[LeftOutOp] | None] = ContextVar(
    "meho_left_out_ops_in_start", default=None
)


def left_out_ops() -> frozenset[LeftOutOp]:
    """The left-out operations of the last start."""
    return _LEFT_OUT


def publish_left_out_ops(ops: Iterable[LeftOutOp]) -> None:
    """Make *ops* the left-out operations, replacing those of an earlier start."""
    global _LEFT_OUT
    _LEFT_OUT = frozenset(ops)


@contextmanager
def collect_left_out_ops() -> Iterator[set[LeftOutOp]]:
    """Collect the operations left out while the block runs (one start)."""
    found: set[LeftOutOp] = set()
    token = _LEFT_OUT_IN_START.set(found)
    try:
        yield found
    finally:
        _LEFT_OUT_IN_START.reset(token)


def without_written_keys(
    ops: Iterable[LeftOutOp], written: Collection[tuple[str, str, str, str]]
) -> set[LeftOutOp]:
    """*ops* without those whose key a registrar wrote in the same start.

    *written* holds ``(product, version, impl_id, op_id)`` of each operation
    a registrar wrote (``collect_code_safety_levels``). The row under such
    a key is that registrar's row, so it is shown and decides its own calls.
    """
    return {op for op in ops if _key(op) not in written}


def _key(op: LeftOutOp) -> tuple[str, str, str, str]:
    return (op.product, op.version, op.impl_id, op.op_id)


def note_left_out_op(op: LeftOutOp) -> None:
    """Record *op* as left out: in the running start, else at once."""
    found = _LEFT_OUT_IN_START.get()
    if found is not None:
        found.add(op)
    else:
        publish_left_out_ops(_LEFT_OUT | {op})


def note_registered_op(op: LeftOutOp) -> None:
    """Outside a start, a registration without the fault shows its key again."""
    if _LEFT_OUT_IN_START.get() is None:
        publish_left_out_ops(without_written_keys(_LEFT_OUT, {_key(op)}))


def left_out_op(
    *, product: str, version: str, impl_id: str, op_id: str, ops: Iterable[LeftOutOp] | None = None
) -> LeftOutOp | None:
    """The left-out operation with this connector triple and op id, of any code."""
    for op in left_out_ops() if ops is None else ops:
        if (op.product, op.version, op.impl_id, op.op_id) == (product, version, impl_id, op_id):
            return op
    return None


def row_is_left_out(row: Any) -> bool:
    """Whether *row* (an ``endpoint_descriptor`` row) is hidden as left out."""
    return (
        row.tenant_id is None
        and row.source_kind in _REGISTERED_KINDS
        and left_out_op(
            product=row.product, version=row.version, impl_id=row.impl_id, op_id=row.op_id
        )
        is not None
    )


def left_out_row_clause() -> ColumnElement[bool]:
    """The predicate "this ``endpoint_descriptor`` row is not hidden as left out"."""
    if not _LEFT_OUT:
        return sa.true()
    matches = [
        and_(
            EndpointDescriptor.product == op.product,
            EndpointDescriptor.version == op.version,
            EndpointDescriptor.impl_id == op.impl_id,
            EndpointDescriptor.op_id == op.op_id,
        )
        for op in sorted(_LEFT_OUT, key=lambda op: tuple(str(part) for part in op))
    ]
    return ~and_(
        EndpointDescriptor.tenant_id.is_(None),
        EndpointDescriptor.source_kind.in_(_REGISTERED_KINDS),
        or_(*matches),
    )


def left_out_sql_keys() -> list[str]:
    """The left-out keys for the raw-SQL twin of :func:`left_out_row_clause`."""
    return sorted({_SQL_KEY_SEPARATOR.join(_key(op)) for op in _LEFT_OUT})
