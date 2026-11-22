# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The re-embed command and the start check of the embedding record.

No model is downloaded: a fake encoder gives every text one fixed vector.
Checks that:

* the command embeds every ``documents`` row, every knowledge passage and
  every operation row (also one with no vector yet), and stores the set
  model in the record;
* knowledge passages follow the window of the new model, and the entry
  keeps its revision and metadata;
* no row gets a new ``created_at`` or ``updated_at``;
* an entry edited or deleted during the run is not written back with old
  content;
* a second run, and a dry run, change nothing;
* a start with another model than the record logs one warning, adds the
  readiness check and starts, and a start with the same model logs none,
  adds no check and embeds nothing.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import select
from structlog.testing import capture_logs

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Document, EmbeddingState, EndpointDescriptor, KbRevision
from meho_backplane.kb import revisions
from meho_backplane.kb.revisions import delete_entry_rows, write_entry
from meho_backplane.kb.schemas import KB_PASSAGE_SOURCE, KB_SOURCE
from meho_backplane.retrieval.embedding_state import (
    check_embedding_model_at_start,
    embedding_model_probe,
    read_embedding_state,
    write_embedding_state,
)
from meho_backplane.retrieval.indexer import index_document
from meho_backplane.retrieval.reembed import ReembedCounts, reembed_all
from meho_backplane.settings import get_settings
from tests._kb_embedding_stub import fake_embedding_service

OLD_VECTOR = [0.1] * 384
NEW_VECTOR = [0.7] * 384
OLD_MODEL = "BAAI/bge-small-en-v1.5"
NEW_MODEL = "example/made-up-multilingual-model"
TENANT = uuid.uuid4()
BODY = "# Title\n\n" + "\n\n".join(f"Paragraph {n}: " + "word " * 12 for n in range(6))
METADATA = {"product": "example", "owner": "someone"}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("KB_INGEST_ROOT", str(tmp_path))
    monkeypatch.delenv("RETRIEVAL_EMBEDDING_MODEL", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _use_model(monkeypatch: pytest.MonkeyPatch, model: str) -> None:
    monkeypatch.setenv("RETRIEVAL_EMBEDDING_MODEL", model)
    get_settings.cache_clear()


async def _seed_endpoint(op_id: str, embedding: list[float] | None) -> uuid.UUID:
    row_id = uuid.uuid4()
    async with get_sessionmaker()() as session:
        session.add(
            EndpointDescriptor(
                id=row_id,
                tenant_id=None,
                product="example",
                version="1.x",
                impl_id="example",
                op_id=op_id,
                source_kind="typed",
                summary=f"Summary of {op_id}.",
                description="Does a made-up thing.",
                tags=["alpha", "beta"],
                parameter_schema={"type": "object"},
                safety_level="safe",
                requires_approval=False,
                is_enabled=True,
                embedding=embedding,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        await session.commit()
    return row_id


async def _seed() -> dict[str, uuid.UUID]:
    """Seed with the old model: a memory row, a knowledge entry and two operations."""
    old = fake_embedding_service(OLD_VECTOR, window=512)
    with patch("meho_backplane.retrieval.indexer.get_embedding_service", return_value=old):
        await index_document(TENANT, "memory", "note-1", "memory", "a made-up note", {})
        doc, _revision, _new = await write_entry(
            tenant_id=TENANT, slug="guide", body=BODY, metadata_for=lambda _m: dict(METADATA)
        )
    async with get_sessionmaker()() as session:
        await write_embedding_state(session, OLD_MODEL)
        await session.commit()
    return {
        "entry": doc.id,
        "with_vector": await _seed_endpoint("with-vector", OLD_VECTOR),
        "without_vector": await _seed_endpoint("without-vector", None),
    }


async def _snapshot() -> dict[str, object]:
    async with get_sessionmaker()() as session:
        docs = (
            await session.execute(
                select(
                    Document.source,
                    Document.source_id,
                    Document.embedding,
                    Document.doc_metadata,
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
                    EndpointDescriptor.updated_at,
                ).order_by(EndpointDescriptor.op_id)
            )
        ).all()
        revisions = (await session.execute(select(KbRevision.id))).scalars().all()
        state = await read_embedding_state(session)
    return {
        "docs": [(d.source, d.source_id, [round(x, 4) for x in d.embedding]) for d in docs],
        "doc_metadata": [dict(d.doc_metadata) for d in docs if d.source == KB_SOURCE],
        "ops": [(o.op_id, None if o.embedding is None else round(o.embedding[0], 4)) for o in ops],
        "doc_times": {
            (d.source, d.source_id): (d.created_at, d.updated_at)
            for d in docs
            if d.source != KB_PASSAGE_SOURCE
        },
        "passage_times": sorted(
            {(d.created_at, d.updated_at) for d in docs if d.source == KB_PASSAGE_SOURCE}
        ),
        "op_times": [(o.op_id, o.updated_at) for o in ops],
        "revisions": sorted(revisions),
        "state": state,
    }


def _passages(snapshot: dict[str, object]) -> list[tuple[str, str, list[float]]]:
    docs = snapshot["docs"]
    assert isinstance(docs, list)
    return [d for d in docs if d[0] == KB_PASSAGE_SOURCE]


async def _run(**kwargs: Any) -> tuple[ReembedCounts, list[str]]:
    new = fake_embedding_service(NEW_VECTOR, window=12)
    lines: list[str] = []
    with patch("meho_backplane.retrieval.indexer.get_embedding_service", return_value=new):
        counts = await reembed_all(service=new, say=lines.append, **kwargs)
    return counts, lines


@pytest.mark.asyncio
async def test_every_vector_is_embedded_again_and_the_record_shows_the_new_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed()
    before = await _snapshot()
    assert len(_passages(before)) >= 1
    _use_model(monkeypatch, NEW_MODEL)

    counts, lines = await _run(batch_size=1)

    after = await _snapshot()
    assert (counts.documents, counts.kb_entries, counts.endpoints) == (2, 1, 2)
    assert any(line.startswith("documents: 2/2") for line in lines)
    assert any(line.startswith("operations: 2/2") for line in lines)
    for source, _source_id, vector in after["docs"]:
        assert vector == NEW_VECTOR, source
    # Every operation gets the new vector, also the one that had none.
    assert after["ops"] == [("with-vector", 0.7), ("without-vector", 0.7)]
    assert after["state"] == (NEW_MODEL, 384)


@pytest.mark.asyncio
async def test_passages_follow_the_new_window_and_the_entry_keeps_revision_and_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed()
    before = await _snapshot()
    _use_model(monkeypatch, NEW_MODEL)

    await _run()

    after = await _snapshot()
    assert len(_passages(after)) > len(_passages(before))
    assert {p[1] for p in _passages(after)}.isdisjoint({p[1] for p in _passages(before)})
    assert after["revisions"] == before["revisions"]
    assert after["doc_metadata"] == before["doc_metadata"] == [METADATA]


@pytest.mark.asyncio
async def test_a_second_run_gives_the_same_rows_and_vectors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed()
    _use_model(monkeypatch, NEW_MODEL)
    await _run()
    first = await _snapshot()

    await _run()

    assert await _snapshot() == first


@pytest.mark.asyncio
@pytest.mark.parametrize("model", [OLD_MODEL, NEW_MODEL])
async def test_no_row_gets_new_times(monkeypatch: pytest.MonkeyPatch, model: str) -> None:
    await _seed()
    before = await _snapshot()
    assert len(before["passage_times"]) == 1  # type: ignore[arg-type]
    _use_model(monkeypatch, model)

    await _run()

    after = await _snapshot()
    assert after["doc_times"] == before["doc_times"]
    assert after["op_times"] == before["op_times"]
    created, updated = before["passage_times"][0]  # type: ignore[index]
    assert after["passage_times"] == [(created, updated)]


async def _entry_body(slug: str) -> str | None:
    async with get_sessionmaker()() as session:
        return await session.scalar(
            select(Document.body).where(Document.source == KB_SOURCE, Document.source_id == slug)
        )


@pytest.mark.asyncio
async def test_an_entry_edited_during_the_run_keeps_the_edit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed()
    _use_model(monkeypatch, NEW_MODEL)
    edited = BODY + "\n\nA paragraph added by another writer."
    calls: list[str] = []

    async def write_after_an_edit(**kwargs: Any) -> Any:
        if not calls:
            calls.append("edit")
            await revisions.write_entry(
                tenant_id=TENANT, slug="guide", body=edited, metadata_for=lambda m: dict(m or {})
            )
        calls.append("reembed")
        return await revisions.write_entry(**kwargs)

    with patch("meho_backplane.retrieval.reembed.write_entry", write_after_an_edit):
        await _run()

    assert calls == ["edit", "reembed", "reembed"]
    assert await _entry_body("guide") == edited
    async with get_sessionmaker()() as session:
        latest = await session.scalar(
            select(KbRevision.body).order_by(KbRevision.revision_number.desc()).limit(1)
        )
    assert latest == edited
    after = await _snapshot()
    assert all(p[2] == NEW_VECTOR for p in _passages(after))


@pytest.mark.asyncio
async def test_an_entry_deleted_during_the_run_stays_deleted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed()
    _use_model(monkeypatch, NEW_MODEL)

    async def delete_then_write(**kwargs: Any) -> Any:
        await delete_entry_rows(TENANT, "guide")
        return await revisions.write_entry(**kwargs)

    with patch("meho_backplane.retrieval.reembed.write_entry", delete_then_write):
        _counts, lines = await _run()

    assert await _entry_body("guide") is None
    assert _passages(await _snapshot()) == []
    assert any("guide: skipped" in line for line in lines)
    async with get_sessionmaker()() as session:
        assert await read_embedding_state(session) == (NEW_MODEL, 384)


@pytest.mark.asyncio
async def test_dry_run_writes_nothing_and_prints_the_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed()
    before = await _snapshot()
    _use_model(monkeypatch, NEW_MODEL)

    counts, lines = await _run(dry_run=True)

    assert await _snapshot() == before
    assert (counts.documents, counts.kb_entries, counts.endpoints) == (2, 1, 2)
    assert "would embed: 2 documents, 1 knowledge entries, 2 operations" in lines[-1]


@pytest.mark.asyncio
async def test_an_empty_database_runs_and_stores_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_model(monkeypatch, NEW_MODEL)

    counts, _lines = await _run()

    assert (counts.documents, counts.kb_entries, counts.endpoints) == (0, 0, 0)
    async with get_sessionmaker()() as session:
        assert await read_embedding_state(session) == (NEW_MODEL, 384)


@pytest.mark.asyncio
async def test_a_failed_encode_leaves_the_record_on_the_old_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed()
    _use_model(monkeypatch, NEW_MODEL)
    broken = fake_embedding_service(NEW_VECTOR)
    broken.encode.side_effect = RuntimeError("encoder is down")

    with pytest.raises(RuntimeError, match="encoder is down"):
        await reembed_all(service=broken, say=lambda _line: None)

    async with get_sessionmaker()() as session:
        assert await read_embedding_state(session) == (OLD_MODEL, 384)


@pytest.mark.asyncio
async def test_a_batch_size_below_one_is_refused() -> None:
    with pytest.raises(ValueError, match="batch size"):
        await reembed_all(batch_size=0, say=lambda _line: None)


@pytest.mark.asyncio
async def test_start_with_another_model_warns_once_and_shows_it_in_the_health_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed()
    _use_model(monkeypatch, NEW_MODEL)

    with capture_logs() as logs:
        assert await check_embedding_model_at_start() is True

    warnings = [e for e in logs if e["event"] == "embedding_model_mismatch"]
    assert len(warnings) == 1
    assert warnings[0]["stored_model"] == OLD_MODEL
    assert warnings[0]["configured_model"] == NEW_MODEL
    probe = embedding_model_probe()
    assert probe.ok is True
    assert OLD_MODEL in (probe.detail or "")
    assert "reembed" in (probe.detail or "")


@pytest.mark.asyncio
async def test_start_with_the_same_model_logs_nothing_and_embeds_nothing() -> None:
    await _seed()
    before = await _snapshot()
    service = fake_embedding_service(NEW_VECTOR)

    with (
        capture_logs() as logs,
        patch("meho_backplane.retrieval.embedding.get_embedding_service", return_value=service),
        patch("meho_backplane.retrieval.indexer.get_embedding_service", return_value=service),
    ):
        # False: the start adds no readiness check, so the health answer stays as it was.
        assert await check_embedding_model_at_start() is False

    assert [e for e in logs if e["event"] == "embedding_model_mismatch"] == []
    service.encode.assert_not_called()
    service.encode_one.assert_not_called()
    assert await _snapshot() == before
    assert embedding_model_probe().detail == f"model={OLD_MODEL}"


@pytest.mark.asyncio
async def test_start_without_a_record_still_starts() -> None:
    with capture_logs() as logs:
        assert await check_embedding_model_at_start() is False

    assert [e for e in logs if e["event"] == "embedding_model_mismatch"] == []
    assert embedding_model_probe().ok is True


@pytest.mark.asyncio
async def test_the_record_is_one_row_and_a_second_write_updates_it() -> None:
    async with get_sessionmaker()() as session:
        await write_embedding_state(session, OLD_MODEL)
        await session.commit()
    async with get_sessionmaker()() as session:
        await write_embedding_state(session, NEW_MODEL)
        await session.commit()
    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(EmbeddingState.model))).scalars().all()
    assert rows == [NEW_MODEL]
