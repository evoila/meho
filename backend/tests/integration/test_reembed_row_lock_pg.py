# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The re-embed command on PostgreSQL: the row lock, and a run that changes nothing.

The unit tests (``tests/test_retrieval_reembed.py``) run on SQLite, which
has no row lock. This module checks what only PostgreSQL can show:

* before it writes a vector, the command reads the row again with
  ``FOR UPDATE``, in the documents group and in the operations group;
* a run with the same model leaves every vector, ``created_at``,
  ``updated_at`` and knowledge revision as it was.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import delete, event, select

from meho_backplane.db.engine import get_engine, get_sessionmaker
from meho_backplane.db.models import Document, EndpointDescriptor, KbRevision
from meho_backplane.kb.revisions import write_entry
from meho_backplane.retrieval.indexer import index_document
from meho_backplane.retrieval.reembed import reembed_all
from tests._kb_embedding_stub import fake_embedding_service

# ``pg_engine`` (imported for its side effect of being a discoverable
# fixture) points the process engine at the testcontainer and seeds the two
# pinned tenant rows.
from .conftest import DOCKER_AVAILABLE, SKIP_REASON, pg_engine  # noqa: F401

pytestmark = pytest.mark.skipif(not DOCKER_AVAILABLE, reason=SKIP_REASON)

#: One of the two tenants ``pg_engine`` seeds on entry.
_TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
_VECTOR = [0.25] * 384
_BODY = "# Title\n\n" + "\n\n".join(f"Paragraph {n}: " + "word " * 12 for n in range(4))


async def _seed() -> None:
    service = fake_embedding_service(_VECTOR)
    with patch("meho_backplane.retrieval.indexer.get_embedding_service", return_value=service):
        await index_document(_TENANT, "memory", "note-1", "memory", "a made-up note", {})
        await write_entry(tenant_id=_TENANT, slug="guide", body=_BODY, metadata_for=lambda _m: {})
    now = datetime.now(UTC)
    async with get_sessionmaker()() as session:
        await session.execute(delete(EndpointDescriptor))
        session.add(
            EndpointDescriptor(
                id=uuid.uuid4(),
                tenant_id=None,
                product="example",
                version="1.x",
                impl_id="example",
                op_id="made-up-op",
                source_kind="typed",
                summary="Summary of a made-up op.",
                description="Does a made-up thing.",
                tags=["alpha"],
                parameter_schema={"type": "object"},
                safety_level="safe",
                requires_approval=False,
                is_enabled=True,
                embedding=_VECTOR,
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()


def _vector(value: Any) -> list[float]:
    return [round(float(x), 4) for x in value]


async def _snapshot() -> dict[str, object]:
    async with get_sessionmaker()() as session:
        docs = (
            await session.execute(
                select(
                    Document.source,
                    Document.source_id,
                    Document.embedding,
                    Document.created_at,
                    Document.updated_at,
                ).order_by(Document.source, Document.source_id)
            )
        ).all()
        ops = (
            await session.execute(
                select(
                    EndpointDescriptor.op_id,
                    EndpointDescriptor.embedding,
                    EndpointDescriptor.created_at,
                    EndpointDescriptor.updated_at,
                ).order_by(EndpointDescriptor.op_id)
            )
        ).all()
        revisions = (await session.execute(select(KbRevision.id))).scalars().all()
    return {
        "docs": [
            (d.source, d.source_id, _vector(d.embedding), d.created_at, d.updated_at) for d in docs
        ],
        "ops": [(o.op_id, _vector(o.embedding), o.created_at, o.updated_at) for o in ops],
        "revisions": sorted(revisions),
    }


def _locked_reads(statements: list[str], table: str) -> list[str]:
    """The command's read before a write: id and ``updated_at`` first, with a lock.

    The knowledge write path locks ``documents`` rows too, so the check
    matches the column list of the command's own query.
    """
    start = f"SELECT {table}.id, {table}.updated_at,"
    return [s for s in statements if s.lstrip().startswith(start) and "FOR UPDATE" in s]


@pytest.mark.usefixtures("pg_engine")
async def test_rows_are_read_again_with_a_lock_and_the_same_model_changes_nothing() -> None:
    await _seed()
    before = await _snapshot()
    statements: list[str] = []

    def _record(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        statements.append(statement)

    sync_engine = get_engine().sync_engine
    event.listen(sync_engine, "before_cursor_execute", _record)
    service = fake_embedding_service(_VECTOR)
    try:
        with patch("meho_backplane.retrieval.indexer.get_embedding_service", return_value=service):
            counts = await reembed_all(service=service, say=lambda _line: None)
    finally:
        event.remove(sync_engine, "before_cursor_execute", _record)

    assert (counts.documents, counts.kb_entries, counts.endpoints) == (2, 1, 1)
    assert _locked_reads(statements, "documents"), "the documents group reads without a lock"
    assert _locked_reads(statements, "endpoint_descriptor"), (
        "the operations group reads without a lock"
    )
    assert await _snapshot() == before
