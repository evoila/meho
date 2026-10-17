# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""An in-memory result-handle store for tests that drive the read cores.

The read cores (:mod:`meho_backplane.operations.result_query`) fetch the
raw record on the event loop and decode it on the worker pool (#3640), so
a test double must hold real encoded records. :class:`MemoryResultStore`
is the production :class:`ResultHandleStore` over a dict-backed client;
``seed`` / ``seed_document`` write records under the ``v2`` key the reader
checks first, and the client can hold a ``GET`` (``gate``, ``delay``) or
serve one record for any key (``fallback``) so a test can read under a
random handle.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import msgspec

from meho_backplane.connectors.result_handle_store import (
    ResultHandleStore,
    _StoredPayload,
    _v2_key,
)

__all__ = ["MemoryResultStore", "MemoryValkey"]


class MemoryValkey:
    """Dict-backed async Valkey double: ``get`` and ``set`` only.

    ``gate`` and ``delay`` make every ``GET`` wait, so a test can hold reads
    in flight or run one past a deadline. ``get_calls`` counts the ``GET``
    calls, which is how a test shows a refused read never reached the
    store. ``fallback`` is served for a key that was never set.
    """

    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.fallback: bytes | None = None
        self.gate: asyncio.Event | None = None
        self.delay = 0.0
        self.get_calls = 0

    async def set(self, name: str, value: Any, ex: int | None = None) -> None:
        del ex
        self.values[name] = value if isinstance(value, bytes) else str(value).encode()

    async def get(self, name: str) -> bytes | None:
        self.get_calls += 1
        if self.gate is not None:
            await self.gate.wait()
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.values.get(name, self.fallback)


def _rows_record(
    *,
    operator_sub: str,
    rows: list[dict[str, Any]],
    total_rows: int | None,
    document: dict[str, Any] | None,
) -> bytes:
    return msgspec.json.encode(
        _StoredPayload(
            operator_sub=operator_sub,
            op_id="test.op",
            rows=rows,
            total_rows=len(rows) if total_rows is None else total_rows,
            stored_rows=len(rows),
            created_at=datetime.now(UTC).isoformat(),
            document=document,
        )
    )


def _document_record(*, operator_sub: str, document: Any) -> bytes:
    return msgspec.json.encode(
        _StoredPayload(
            operator_sub=operator_sub,
            op_id="test.op",
            rows=[],
            total_rows=0,
            stored_rows=0,
            created_at=datetime.now(UTC).isoformat(),
            record_kind="document",
            document=document,
        )
    )


class MemoryResultStore(ResultHandleStore):
    """The production store over :class:`MemoryValkey`, with seeding helpers."""

    def __init__(self) -> None:
        self.client = MemoryValkey()
        super().__init__(self.client)  # type: ignore[arg-type]

    def seed(
        self,
        *,
        tenant_id: UUID,
        operator_sub: str,
        handle_id: UUID,
        rows: list[dict[str, Any]],
        total_rows: int | None = None,
        document: dict[str, Any] | None = None,
    ) -> None:
        """Store a rows record under the handle key."""
        self.client.values[_v2_key(tenant_id, handle_id)] = _rows_record(
            operator_sub=operator_sub, rows=rows, total_rows=total_rows, document=document
        )

    def seed_document(
        self, *, tenant_id: UUID, operator_sub: str, handle_id: UUID, document: Any
    ) -> None:
        """Store a document record under the handle key."""
        self.client.values[_v2_key(tenant_id, handle_id)] = _document_record(
            operator_sub=operator_sub, document=document
        )

    def seed_any_handle(
        self, *, operator_sub: str, rows: list[dict[str, Any]], total_rows: int | None = None
    ) -> None:
        """Serve one rows record for every handle a test asks for."""
        self.client.fallback = _rows_record(
            operator_sub=operator_sub, rows=rows, total_rows=total_rows, document=None
        )
