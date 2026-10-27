# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared parts of the source adapters (the ``<product>.migration.inventory`` reads).

Every adapter is a multi-step operation on its own connector. It reads the
source only through that connector's own operations (child calls through
``dispatch_child``), builds one :class:`~.contracts.InventoryEnvelope`, and
stores it. This module holds what all adapters share:

* :func:`read_child` runs one child read and returns its **full** answer.
  A large child answer reaches the parent as a short summary plus a handle;
  the full answer is taken from the dispatcher's capture, the same way the
  sensor runner reads it.
* :class:`InventoryBuilder` collects records and omissions and builds the
  envelope. A failure for one item becomes an omission; the read goes on.
* :func:`record_with_reasons` fills ``missing`` for every null field that
  has no reason yet, so each adapter names only its special cases.
* :func:`store_inventory` stores the envelope whole and returns the short
  answer with the handle id. The inventory is always stored, also when it
  is small, so every adapter answer has a handle that
  ``migration.inventory.csv`` and the later assessment steps can read.

Text from source systems (names, OS names, labels) is data. It goes into
record fields and into child-call parameters, where each child operation
checks it with its own rules. It is never used as an instruction.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final
from uuid import uuid4

from meho_backplane.connectors.migration.contracts import (
    InventoryCounts,
    InventoryEnvelope,
    InventorySource,
    MissingReason,
    Omission,
    RecordKind,
    _null_paths,
    _Record,
    missing_reason,
)
from meho_backplane.connectors.result_handle_store import get_result_handle_store
from meho_backplane.operations.reducer import (
    FullResultCapture,
    full_result_capture_var,
    handle_owner_sub_var,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.operations.composite import DispatchChild

__all__ = [
    "CHILD_CONCURRENCY",
    "INVENTORY_TTL_SECONDS",
    "ChildReadError",
    "InventoryBuilder",
    "bytes_to_gb",
    "bytes_to_mb",
    "gather_limited",
    "read_child",
    "record_with_reasons",
    "rows_of",
    "store_inventory",
    "target_name",
]

#: Child reads one adapter runs at the same time.
CHILD_CONCURRENCY: Final[int] = 4

#: How long a stored inventory lasts: one hour, the same as any stored result.
INVENTORY_TTL_SECONDS: Final[int] = 3600

_GIB: Final[int] = 1024**3
_MIB: Final[int] = 1024**2


class ChildReadError(Exception):
    """A child read did not answer. ``reason`` is the inventory's missing reason."""

    def __init__(self, op_id: str, reason: MissingReason) -> None:
        super().__init__(f"{op_id} did not answer ({reason})")
        self.op_id = op_id
        self.reason: MissingReason = reason


async def read_child(
    dispatch_child: DispatchChild,
    *,
    connector_id: str,
    op_id: str,
    params: dict[str, Any] | None = None,
) -> Any:
    """Run one child read and return its full answer.

    Raises :class:`ChildReadError` when the child was denied
    (``no_permission``) or did not answer (``collection_failed``).
    """
    capture = FullResultCapture()
    token = full_result_capture_var.set(capture)
    try:
        result = await dispatch_child(connector_id=connector_id, op_id=op_id, params=params or {})
    finally:
        full_result_capture_var.reset(token)
    if result.status == "denied":
        raise ChildReadError(op_id, "no_permission")
    if result.status != "ok":
        raise ChildReadError(op_id, "collection_failed")
    return capture.payload if capture.captured else result.result


def rows_of(payload: Any) -> list[dict[str, Any]]:
    """The ``rows`` of a ``{rows, total}`` answer, object rows only."""
    rows = payload.get("rows") if isinstance(payload, dict) else None
    return [row for row in rows or [] if isinstance(row, dict)]


async def gather_limited[T](
    items: Iterable[T],
    work: Callable[[T], Awaitable[Any]],
    *,
    limit: int = CHILD_CONCURRENCY,
) -> list[Any]:
    """Run ``work(item)`` for every item, at most *limit* at a time, in item order."""
    gate = asyncio.Semaphore(limit)

    async def _one(item: T) -> Any:
        async with gate:
            return await work(item)

    return list(await asyncio.gather(*(_one(item) for item in items)))


def bytes_to_gb(value: Any) -> float | None:
    """Bytes to GB (1024^3), rounded to two places; ``None`` for no number."""
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        return None
    return round(value / _GIB, 2)


def bytes_to_mb(value: Any) -> int | None:
    """Bytes to whole MB (1024^2); ``None`` for no number."""
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        return None
    return int(value // _MIB)


def target_name(target: Any) -> str:
    """The name of the target the adapter read."""
    name = getattr(target, "name", None)
    return str(name) if name else str(target)


def record_with_reasons[R: _Record](
    model: type[R],
    fields: dict[str, Any],
    *,
    reasons: dict[str, MissingReason] | None = None,
    default: MissingReason = "not_reported_by_source",
) -> R:
    """Build a record; every null field without a reason in *reasons* gets *default*.

    Nested values must already be models (``Disk(...)``), so the null paths
    below them are found.
    """
    missing: dict[str, MissingReason] = dict(reasons or {})
    draft = model.model_construct(**fields)
    for path in _null_paths(draft):
        if missing_reason(missing, path) is None:
            missing[path] = default
    return model(**fields, missing=missing)


class InventoryBuilder:
    """Collects the records and omissions of one adapter run."""

    def __init__(self, *, connector_id: str, target: str) -> None:
        self.connector_id = connector_id
        self.target = target
        self.records: list[Any] = []
        self.omissions: list[Omission] = []
        self.source_totals: dict[RecordKind, int] = {}
        self.omitted_items: dict[RecordKind, int] = {}

    def add(self, record: Any) -> None:
        """Add one record."""
        self.records.append(record)

    def omit(
        self,
        what: str,
        reason: MissingReason,
        *,
        detail: str | None = None,
        item_kind: RecordKind | None = None,
    ) -> None:
        """Record what was not collected and why.

        Pass *item_kind* when a whole item of the source (a VM, a workload) is
        left out, so the counts still add up to the source's total.
        """
        self.omissions.append(Omission(what=what, reason=reason, detail=detail))
        if item_kind is not None:
            self.omitted_items[item_kind] = self.omitted_items.get(item_kind, 0) + 1

    def envelope(self) -> InventoryEnvelope:
        """The inventory envelope. Raises when a record breaks the shape's rules."""
        counts = {"server": 0, "application": 0, "kubernetes_workload": 0}
        for record in self.records:
            counts[record.kind] += 1
        return InventoryEnvelope(
            schema_version="1",
            source=InventorySource(
                connector_id=self.connector_id,
                target=self.target,
                collected_at=datetime.now(UTC),
            ),
            records=self.records,
            omissions=self.omissions,
            counts=InventoryCounts(**counts, source_totals=self.source_totals),
        )

    def counts_match(self, envelope: InventoryEnvelope) -> bool:
        """``True`` when records plus left-out items equal each total the source reports."""
        for kind, total in self.source_totals.items():
            found = getattr(envelope.counts, kind) + self.omitted_items.get(kind, 0)
            if found != total:
                return False
        return True


async def store_inventory(
    operator: Operator,
    builder: InventoryBuilder,
    *,
    op_id: str,
) -> dict[str, Any]:
    """Store the builder's envelope whole and return the adapter's short answer.

    The envelope is a document record of the result handle store, the same
    record kind the JSONFlux reducer writes for a large single object, so
    ``migration.inventory.csv`` and ``result_query`` read it. Raises
    :class:`RuntimeError` when the store did not keep it.
    """
    envelope = builder.envelope()
    document = envelope.model_dump(mode="json")
    handle_id = uuid4()
    stored = await get_result_handle_store().spill_document(
        tenant_id=operator.tenant_id,
        operator_sub=handle_owner_sub_var.get() or operator.sub,
        handle_id=handle_id,
        op_id=op_id,
        document=document,
        ttl_seconds=INVENTORY_TTL_SECONDS,
        metadata={
            "root_type": "object",
            "document_bytes": len(json.dumps(document, separators=(",", ":"))),
        },
    )
    if not stored:
        raise RuntimeError(
            "MEHO could not store the inventory: the result store is not reachable "
            "or the inventory is over the size limit of one stored result"
        )
    counts = envelope.counts
    return {
        "inventory_handle": str(handle_id),
        "expires_at": (datetime.now(UTC) + timedelta(seconds=INVENTORY_TTL_SECONDS)).isoformat(),
        "connector_id": envelope.source.connector_id,
        "target": envelope.source.target,
        "collected_at": envelope.source.collected_at.isoformat(),
        "record_counts": {
            "server": counts.server,
            "application": counts.application,
            "kubernetes_workload": counts.kubernetes_workload,
        },
        "source_totals": dict(counts.source_totals),
        "left_out_items": dict(builder.omitted_items),
        "counts_match": builder.counts_match(envelope),
        "omission_count": len(envelope.omissions),
        "omissions": [omission.model_dump(mode="json") for omission in envelope.omissions],
    }
