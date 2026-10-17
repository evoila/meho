# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""KB passage index and cited passage search (#3761).

Covers, with no model download (a stub counter, or a tiny tokenizer
built in the test):

* the Markdown heading / sentence chunker: stable order, offsets, ids
  and chunker / model provenance, and every passage fits the window;
* the token counter: a separate tokenizer from the same model files,
  with no cut and no padding, special tokens counted, and the model's
  own tokenizer left unchanged;
* the one-use prepared index batch;
* publishing: only the current revision's passages exist, a failed
  prepare or publish keeps the old head and passages, a new chunker
  or model re-chunks the same revision, delete removes passages;
* search: one hit per entry with whole passages and exact locators,
  middle and end evidence of a long body without the full body, and
  the shared wire bound on ``search_knowledge``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select
from tokenizers import Tokenizer, models, pre_tokenizers, processors

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Document, KbRevision
from meho_backplane.kb import passages as passages_module
from meho_backplane.kb.passages import (
    CHUNKER_VERSION,
    chunk_markdown,
    index_generation,
    passage_items,
)
from meho_backplane.kb.schemas import KB_PASSAGE_SOURCE, KB_SOURCE, META_KB_PASSAGE
from meho_backplane.kb.service import KbService
from meho_backplane.mcp.tools.knowledge import _bounded_search_response
from meho_backplane.operations.delivery_budget import measure_delivered_wire_bytes
from meho_backplane.retrieval import retriever
from meho_backplane.retrieval.embedding import EmbeddingService, PassageTokenizer
from meho_backplane.retrieval.indexer import IndexItem, prepare_index_batch
from meho_backplane.retrieval.retriever import RetrievalHit
from meho_backplane.settings import get_settings
from meho_backplane.untrusted_text import BLOCK_START
from tests._kb_embedding_stub import StubPassageTokenizer, fake_embedding_service

_WINDOW = 40


def _count(text: str) -> int:
    return StubPassageTokenizer().count(text)


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("KB_INGEST_ROOT", str(tmp_path))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def fake_embed() -> Iterator[AsyncMock]:
    fake = fake_embedding_service(window=_WINDOW)
    with patch("meho_backplane.retrieval.indexer.get_embedding_service", return_value=fake):
        yield fake


def _long_body() -> str:
    filler = " ".join(f"word{i}" for i in range(12)) + "."
    intro = "\n\n".join(f"Intro paragraph {i}. {filler}" for i in range(6))
    middle = "\n\n".join(f"Middle paragraph {i}. {filler}" for i in range(6))
    end = "\n\n".join(f"Closing paragraph {i}. {filler}" for i in range(6))
    return (
        f"# Runbook\n\n{intro}\n\n"
        f"## Restart\n\n{middle}\n\nThe zebracode marker sits in the middle.\n\n"
        f"### Verify\n\n{end}\n\nThe quokkacode marker sits at the end.\n"
    )


# ---------------------------------------------------------------------------
# Chunker
# ---------------------------------------------------------------------------


def test_chunker_is_deterministic_and_offsets_are_exact() -> None:
    body = _long_body()
    first = chunk_markdown(body, _count, _WINDOW)
    second = chunk_markdown(body, _count, _WINDOW)
    assert first == second
    assert len(first) > 3
    assert [p.ordinal for p in first] == list(range(len(first)))
    starts = [p.start for p in first]
    assert starts == sorted(starts)
    for passage in first:
        assert passage.text == body[passage.start : passage.end]
        assert passage.text == passage.text.strip()
        assert passage.tokens == _count(passage.embed_text) <= _WINDOW
        assert passage.embed_text.endswith(passage.text)


def test_chunker_follows_heading_paths_and_skips_fenced_headings() -> None:
    body = (
        "Preamble text.\n\n# Top\n\nTop text.\n\n## Child\n\n```\n# not a heading\n```\n\n"
        "### Grandchild\n\nDeep text.\n\n## Sibling\n\nSibling text.\n"
    )
    found = chunk_markdown(body, _count, 512)
    paths = [p.heading_path for p in found]
    assert paths == [
        (),
        ("Top",),
        ("Top", "Child"),
        ("Top", "Child", "Grandchild"),
        ("Top", "Sibling"),
    ]
    assert "# not a heading" in found[2].text
    assert found[3].embed_text.startswith("Top > Child > Grandchild\n\n")
    assert found[0].embed_text == "Preamble text."


def test_chunker_falls_back_to_sentences_words_and_characters() -> None:
    sentences = "One two three. Four five six. Seven eight nine. Ten eleven twelve."
    by_sentence = chunk_markdown(sentences, _count, 6)
    assert [p.text for p in by_sentence] == [
        "One two three.",
        "Four five six.",
        "Seven eight nine.",
        "Ten eleven twelve.",
    ]
    words = "alpha beta gamma delta epsilon"
    assert [p.text for p in chunk_markdown(words, _count, 4)] == [
        "alpha beta",
        "gamma delta",
        "epsilon",
    ]

    def per_char(text: str) -> int:
        return len(text) + 2

    assert [p.text for p in chunk_markdown("abcdefg", per_char, 5)] == ["abc", "def", "g"]


def test_chunker_drops_a_header_that_would_crowd_out_the_text() -> None:
    title = " ".join(["long"] * 30)
    found = chunk_markdown(f"# {title}\n\nShort body.", _count, _WINDOW)
    assert all(p.embed_text == p.text for p in found)
    assert all(p.tokens <= _WINDOW for p in found)


def test_passage_items_carry_ids_locator_and_provenance() -> None:
    body = _long_body()
    chunks = chunk_markdown(body, _count, _WINDOW)
    entry, revision = uuid.uuid4(), uuid.uuid4()
    model = "BAAI/bge-small-en-v1.5"
    items = passage_items(
        passages=chunks,
        entry_id=entry,
        revision_id=revision,
        slug="runbook",
        metadata={"source_kind": "evoila-distilled", META_KB_PASSAGE: "caller value"},
        embedding_model=model,
    )
    again = passage_items(
        passages=chunks,
        entry_id=entry,
        revision_id=revision,
        slug="runbook",
        metadata={"source_kind": "evoila-distilled"},
        embedding_model=model,
    )
    assert [i.source_id for i in items] == [i.source_id for i in again]
    assert len({i.source_id for i in items}) == len(items)
    assert all(i.source_id.startswith(f"{revision}:") for i in items)
    other_model = passage_items(
        passages=chunks,
        entry_id=entry,
        revision_id=revision,
        slug="runbook",
        metadata={},
        embedding_model="another/model",
    )
    assert {i.source_id for i in other_model}.isdisjoint({i.source_id for i in items})

    locator = items[1].metadata[META_KB_PASSAGE]
    assert items[1].metadata["source_kind"] == "evoila-distilled"
    assert locator["entry_id"] == str(entry)
    assert locator["revision_id"] == str(revision)
    assert locator["ordinal"] == 1
    assert locator["passage_count"] == len(chunks)
    assert locator["chunker_version"] == CHUNKER_VERSION
    assert locator["embedding_model"] == model
    assert locator["index_generation"] == index_generation(model)
    assert body[locator["start"] : locator["end"]] == items[1].body
    assert items[1].embed_text == chunks[1].embed_text
    assert items[1].tokens == chunks[1].tokens


# ---------------------------------------------------------------------------
# Token counter (no download: a tiny tokenizer built here)
# ---------------------------------------------------------------------------


def _tiny_model_dir(root: Path) -> Path:
    vocab = {"[PAD]": 0, "[UNK]": 1, "[CLS]": 2, "[SEP]": 3, "alpha": 4, "beta": 5}
    tokenizer = Tokenizer(models.WordLevel(vocab, unk_token="[UNK]"))
    tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
    tokenizer.post_processor = processors.TemplateProcessing(
        single="[CLS] $A [SEP]", special_tokens=[("[CLS]", 2), ("[SEP]", 3)]
    )
    tokenizer.enable_truncation(max_length=8)
    tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
    root.mkdir(parents=True, exist_ok=True)
    tokenizer.save(str(root / "tokenizer.json"))
    (root / "tokenizer_config.json").write_text(
        json.dumps({"model_max_length": 8, "pad_token": "[PAD]"}), encoding="utf-8"
    )
    (root / "special_tokens_map.json").write_text(
        json.dumps({"cls_token": "[CLS]", "sep_token": "[SEP]", "pad_token": "[PAD]"}),
        encoding="utf-8",
    )
    return root


def test_passage_tokenizer_counts_past_the_window_with_special_tokens(tmp_path: Path) -> None:
    counter = PassageTokenizer.from_model_dir(_tiny_model_dir(tmp_path / "model"))
    assert counter.window == 8
    assert counter.count("alpha") == 3
    assert counter.count(" ".join(["alpha", "beta"] * 10)) == 22


async def test_embedding_service_builds_a_separate_counter(tmp_path: Path) -> None:
    from fastembed.common.preprocessor_utils import load_tokenizer

    model_dir = _tiny_model_dir(tmp_path / "model")
    shared, _ = load_tokenizer(model_dir)
    service = EmbeddingService(model_name="tiny/model", cache_dir=str(tmp_path))
    service._model = SimpleNamespace(
        model=SimpleNamespace(_model_dir=str(model_dir), tokenizer=shared)
    )

    counter = await service.passage_tokenizer()
    assert await service.passage_tokenizer() is counter
    long_text = " ".join(["alpha"] * 20)
    assert counter.count(long_text) == 22
    # The model's own tokenizer still cuts and pads.
    assert shared.truncation is not None and shared.truncation["max_length"] == 8
    assert shared.padding is not None
    assert len(shared.encode(long_text).ids) == 8


async def test_embedding_service_without_model_files_fails_loudly(tmp_path: Path) -> None:
    service = EmbeddingService(model_name="tiny/model", cache_dir=str(tmp_path))
    service._model = SimpleNamespace(model=SimpleNamespace())
    with pytest.raises(RuntimeError, match="token counter"):
        await service.passage_tokenizer()


# ---------------------------------------------------------------------------
# Prepared batch
# ---------------------------------------------------------------------------


async def test_prepared_batch_is_used_once_and_does_not_commit(fake_embed: AsyncMock) -> None:
    tenant = uuid.uuid4()
    batch = await prepare_index_batch(
        tenant,
        [IndexItem(source="scratch", source_id="a", kind="k", body="body a", metadata={})],
    )
    assert fake_embed.encode.await_count == 1
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        docs = await batch.apply(session)
        assert [d.source_id for d in docs] == ["a"]
        with pytest.raises(RuntimeError, match="already applied"):
            await batch.apply(session)
        await session.rollback()
    async with sessionmaker() as session:
        count = await session.scalar(select(func.count()).where(Document.tenant_id == tenant))
    assert count == 0


async def test_prepare_failure_writes_nothing(fake_embed: AsyncMock) -> None:
    fake_embed.encode.side_effect = RuntimeError("model down")
    with pytest.raises(RuntimeError, match="model down"):
        await prepare_index_batch(
            uuid.uuid4(),
            [IndexItem(source="scratch", source_id="a", kind="k", body="b", metadata={})],
        )


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------


async def _passages(tenant: uuid.UUID) -> list[Document]:
    async with get_sessionmaker()() as session:
        rows = await session.scalars(
            select(Document)
            .where(Document.tenant_id == tenant, Document.source == KB_PASSAGE_SOURCE)
            .order_by(Document.source_id)
        )
        return list(rows)


async def _revisions(entry_id: uuid.UUID) -> int:
    async with get_sessionmaker()() as session:
        count = await session.scalar(
            select(func.count()).select_from(KbRevision).where(KbRevision.entry_id == entry_id)
        )
    return int(count or 0)


async def test_only_current_revision_passages_exist(fake_embed: AsyncMock) -> None:
    service = KbService()
    tenant = uuid.uuid4()
    first, _ = await service.create_entry(tenant, "runbook", _long_body())
    old = await _passages(tenant)
    assert len(old) > 3
    assert {p.doc_metadata[META_KB_PASSAGE]["revision_id"] for p in old} == {str(first.revision_id)}

    second, _ = await service.create_entry(tenant, "runbook", "# New\n\nShort new body.")
    new = await _passages(tenant)
    assert [p.body for p in new] == ["# New\n\nShort new body."]
    assert new[0].doc_metadata[META_KB_PASSAGE]["revision_id"] == str(second.revision_id)
    assert await _revisions(first.id) == 2
    # The old body stays readable as a revision, but none of its passages remain.
    old_revision = await service.get_revision(tenant, first.id, first.revision_id)
    assert old_revision is not None and old_revision.body == _long_body()


async def test_unchanged_write_reuses_passages_and_refreshes_metadata(
    fake_embed: AsyncMock,
) -> None:
    service = KbService()
    tenant = uuid.uuid4()
    await service.create_entry(tenant, "runbook", _long_body(), {"team": "a"})
    before = await _passages(tenant)
    calls = fake_embed.encode.await_count

    await service.create_entry(tenant, "runbook", _long_body(), {"team": "b"})
    after = await _passages(tenant)
    assert fake_embed.encode.await_count == calls
    assert [p.source_id for p in after] == [p.source_id for p in before]
    assert {p.doc_metadata["team"] for p in after} == {"b"}


async def test_new_chunker_version_rechunks_the_same_revision(
    fake_embed: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = KbService()
    tenant = uuid.uuid4()
    entry, _ = await service.create_entry(tenant, "runbook", _long_body())
    before = {p.source_id for p in await _passages(tenant)}

    monkeypatch.setattr(passages_module, "CHUNKER_VERSION", "kb-md-heading-test")
    again, _ = await service.create_entry(tenant, "runbook", _long_body())
    after = await _passages(tenant)
    assert again.revision_id == entry.revision_id
    assert await _revisions(entry.id) == 1
    assert before.isdisjoint({p.source_id for p in after})
    assert {p.doc_metadata[META_KB_PASSAGE]["chunker_version"] for p in after} == {
        "kb-md-heading-test"
    }


async def test_failed_prepare_keeps_old_head_and_passages(fake_embed: AsyncMock) -> None:
    service = KbService()
    tenant = uuid.uuid4()
    entry, _ = await service.create_entry(tenant, "runbook", _long_body())
    before = [(p.source_id, p.body) for p in await _passages(tenant)]

    fake_embed.encode.side_effect = RuntimeError("model down")
    with pytest.raises(RuntimeError, match="model down"):
        await service.create_entry(tenant, "runbook", "A different body.")

    head = await service.get_entry(tenant, "runbook")
    assert head is not None and head.body == _long_body()
    assert head.revision_id == entry.revision_id
    assert [(p.source_id, p.body) for p in await _passages(tenant)] == before
    assert await _revisions(entry.id) == 1


async def test_failed_publish_keeps_old_head_and_passages(fake_embed: AsyncMock) -> None:
    service = KbService()
    tenant = uuid.uuid4()
    entry, _ = await service.create_entry(tenant, "runbook", _long_body())
    before = [(p.source_id, p.body) for p in await _passages(tenant)]

    with (
        patch("meho_backplane.kb.revisions._add_revision", side_effect=RuntimeError("db down")),
        pytest.raises(RuntimeError, match="db down"),
    ):
        await service.create_entry(tenant, "runbook", "A different body.")

    head = await service.get_entry(tenant, "runbook")
    assert head is not None and head.body == _long_body()
    assert [(p.source_id, p.body) for p in await _passages(tenant)] == before
    assert await _revisions(entry.id) == 1


async def test_delete_removes_passages(fake_embed: AsyncMock) -> None:
    service = KbService()
    tenant = uuid.uuid4()
    await service.create_entry(tenant, "runbook", _long_body())
    await service.create_entry(tenant, "other", "Other body.")
    assert await service.delete_entry(tenant, "runbook") is True
    remaining = await _passages(tenant)
    assert {p.doc_metadata[META_KB_PASSAGE]["slug"] for p in remaining} == {"other"}


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def _as_hit(doc: Document, rank: int) -> RetrievalHit:
    return RetrievalHit(
        document_id=doc.id,
        tenant_id=doc.tenant_id,
        source=doc.source,
        source_id=doc.source_id,
        kind=doc.kind,
        body=doc.body,
        doc_metadata=doc.doc_metadata,
        created_at=doc.created_at,
        updated_at=doc.updated_at,
        fused_score=1.0 / (60 + rank),
        bm25_score=None,
        cosine_score=None,
        bm25_rank=rank,
        cosine_rank=None,
    )


def _fake_retrieve(tenant: uuid.UUID, captured: dict[str, Any]) -> Any:
    """Rank the stored passage rows that contain a query word, in body order."""

    async def fake(**kwargs: Any) -> list[RetrievalHit]:
        captured.update(kwargs)
        words = str(kwargs["query"]).lower().split()
        rows = [p for p in await _passages(tenant) if any(word in p.body.lower() for word in words)]
        rows.sort(key=lambda p: p.doc_metadata[META_KB_PASSAGE]["ordinal"])
        return [_as_hit(row, rank) for rank, row in enumerate(rows, start=1)]

    return fake


async def test_search_returns_middle_and_end_evidence_not_the_full_body(
    fake_embed: AsyncMock,
) -> None:
    service = KbService()
    tenant = uuid.uuid4()
    entry, _ = await service.create_entry(tenant, "runbook", _long_body())
    captured: dict[str, Any] = {}

    with patch("meho_backplane.kb.service.retrieve", side_effect=_fake_retrieve(tenant, captured)):
        hits = await service.search_entries(tenant, "zebracode quokkacode")

    assert captured["source"] == KB_SOURCE
    assert captured["limit"] == retriever.CANDIDATE_LIMIT
    assert len(hits) == 1
    hit = hits[0]
    assert hit.slug == "runbook"
    assert hit.coverage == "partial"
    assert META_KB_PASSAGE not in hit.metadata
    texts = [p.text for p in hit.passages]
    assert any("zebracode" in t for t in texts)
    assert any("quokkacode" in t for t in texts)
    assert all(len(t) < len(_long_body()) // 3 for t in texts)
    revision = await service.get_revision(tenant, entry.id, entry.revision_id)
    assert revision is not None
    for evidence in hit.passages:
        loc = evidence.locator
        assert loc.entry_id == entry.id and loc.revision_id == entry.revision_id
        assert revision.body[loc.start : loc.end] == evidence.text
        assert loc.uri == f"meho://kb/id/{entry.id}/revision/{entry.revision_id}"
        assert loc.chunker_version == CHUNKER_VERSION
    zebra = next(p for p in hit.passages if "zebracode" in p.text)
    assert zebra.locator.heading_path == ["Runbook", "Restart"]


async def test_search_groups_entries_and_keeps_legacy_heads(fake_embed: AsyncMock) -> None:
    service = KbService()
    tenant = uuid.uuid4()
    await service.create_entry(tenant, "short", "Tiny zebracode entry.")
    passage = (await _passages(tenant))[0]
    legacy = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=tenant,
        source=KB_SOURCE,
        source_id="legacy-entry",
        kind="kb-entry",
        body="Old whole-body zebracode entry.",
        doc_metadata={"team": "x"},
        created_at=passage.created_at,
        updated_at=passage.updated_at,
    )
    ranked = [_as_hit(passage, 1), _as_hit(legacy, 2)]

    with patch("meho_backplane.kb.service.retrieve", AsyncMock(return_value=ranked)):
        hits = await service.search_entries(tenant, "zebracode", limit=5)
        assert await service.search_entries(tenant, "zebracode", limit=1) == hits[:1]

    assert [h.slug for h in hits] == ["short", "legacy-entry"]
    assert hits[0].coverage == "complete"
    assert [p.text for p in hits[0].passages] == ["Tiny zebracode entry."]
    assert hits[1].passages == [] and hits[1].coverage == "partial"
    assert hits[1].snippet == "Old whole-body zebracode entry."


def test_bounded_search_response_drops_lowest_ranked_hits() -> None:
    hits = [{"slug": f"s{i}", "text": "x" * 400} for i in range(10)]
    whole = _bounded_search_response(hits, 1_000_000)
    assert whole["delivery"] == "complete" and len(whole["hits"]) == 10

    limit = 3000
    bounded = _bounded_search_response(hits, limit)
    assert bounded["delivery"] == "partial"
    assert 0 < len(bounded["hits"]) < 10
    assert [h["slug"] for h in bounded["hits"]] == [f"s{i}" for i in range(len(bounded["hits"]))]
    assert bounded["omitted_hits"] == 10 - len(bounded["hits"])
    assert measure_delivered_wire_bytes(bounded, structured=False) <= limit


async def test_search_knowledge_wraps_text_as_untrusted(fake_embed: AsyncMock) -> None:
    from meho_backplane.auth.operator import Operator, TenantRole
    from meho_backplane.mcp.tools.knowledge import _search_knowledge_handler

    tenant = uuid.uuid4()
    await KbService().create_entry(tenant, "runbook", _long_body())
    operator = Operator(sub="op", raw_jwt="x", tenant_id=tenant, tenant_role=TenantRole.OPERATOR)
    with patch("meho_backplane.kb.service.retrieve", side_effect=_fake_retrieve(tenant, {})):
        payload = await _search_knowledge_handler(operator, {"query": "zebracode"})

    assert payload["delivery"] == "complete"
    hit = payload["hits"][0]
    assert hit["snippet"].startswith(BLOCK_START)
    assert all(p["text"].startswith(BLOCK_START) for p in hit["passages"])
    assert hit["passages"][0]["locator"]["uri"].startswith("meho://kb/id/")
