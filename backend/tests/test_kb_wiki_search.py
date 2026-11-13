# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Knowledge search reads the tenant's wiki through the knowledge index.

A tenant setting (``tenant.knowledge_wiki_collection``) names one docs
collection where the tenant keeps its lessons. These tests cover:

* with the setting empty, ``search_knowledge`` and ``add_to_knowledge``
  answer as before (no ``wiki`` key, the docs backend is never called);
* with the setting and the entitlement, the wiki's hits come next to the
  store's hits, with their collection and source link, and the audit row
  names the collection;
* without the docs add-on, without the entitlement, with an unknown or
  not-ready collection, with the index down, or with filters, the store's
  hits still come back and ``wiki`` says why the wiki was not searched;
* a wiki page that the store holds as an imported copy is shown once, and
  only when the whole wiki page (read with the hit's read handle) is the
  stored copy's whole text: a page at the same path in another repository
  stays, also when it shares a paragraph with the copy or when the copy is
  only one part of the wiki page; a page that cannot be read in full stays;
* ``add_to_knowledge`` stores the lesson as a direct write and its answer
  names the collection and says whether the lesson goes to the wiki;
* the setting is per tenant;
* ``POST /api/v1/retrieve`` with ``source="kb"`` (``meho kb search``)
  gives the same wiki result.

The docs backend is faked at the ``corpus-http`` transport seams (search
and read), the same seams ``tests/test_mcp_tools_docs.py`` and
``tests/test_mcp_tools_docs_read.py`` patch. The store search is faked
at the retriever, as in ``tests/test_mcp_tools_knowledge.py``.
"""

from __future__ import annotations

import json
import unicodedata
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.api.v1.retrieve import router as retrieve_router
from meho_backplane.auth.corpus import (
    CorpusChunk,
    CorpusReadError,
    CorpusSearchResponse,
    CorpusUnavailable,
    UpstreamRead,
)
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, Tenant
from meho_backplane.instance_settings.schema import parse_settings_file
from meho_backplane.kb.page_import import KbImportPage, KbImportRequest, import_pages
from meho_backplane.kb.provenance import stamp_provenance
from meho_backplane.kb.service import KbService
from meho_backplane.main import app
from meho_backplane.mcp.auth import verify_mcp_jwt_and_bind
from meho_backplane.mcp.tools.knowledge import (
    _add_to_knowledge_handler,
    _search_knowledge_handler,
)
from meho_backplane.middleware import verify_jwt_and_bind
from meho_backplane.operations.delivery_budget import measure_delivered_wire_bytes
from meho_backplane.retrieval.retriever import RetrievalHit
from meho_backplane.settings import get_settings
from meho_backplane.untrusted_text import BLOCK_START
from tests._kb_embedding_stub import fake_embedding_service
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
    seed_doc_collection,
)

_WIKI = "team-wiki"
_ENTITLED = frozenset({"meho-docs", f"meho-docs:{_WIKI}"})
_CORPUS_SEAM = "meho_backplane.docs_search.backends.corpus_http.search_corpus"
_READ_SEAM = "meho_backplane.docs_search.backends.corpus_http.read_corpus"
#: A wiki collection that offers read, so a hit carries a read handle.
_READ_ON: dict[str, Any] = {"type": "corpus-http", "ref": {"read": "upstream"}}
_HANDLE = "eyJ2IjoxLCJ0IjoiaCJ9.c2lnbmVkLWhhbmRsZQ"
_STORE_SEAM = "meho_backplane.kb.service.retrieve"
_REST_STORE_SEAM = "meho_backplane.api.v1.retrieve.retrieve"
_TENANT_B = uuid.UUID("00000000-0000-0000-0000-00000000b0b0")
_PAGE_PATH = "products/vcenter/snapshot-limits.md"
_WIKI_CHUNK = CorpusChunk(
    chunk_id="wiki-snapshot-limits-0001",
    title="Snapshot limits",
    content="Keep at most 32 snapshots per VM.",
    source_url=f"https://git.example.com/team/wiki/-/blob/abc123/{_PAGE_PATH}",
    score=0.9,
    read_handle=_HANDLE,
)
#: A longer wiki page at the same path: the hit above is one paragraph of it.
_LONG_PAGE = "# Snapshot limits\n\nKeep at most 32 snapshots per VM.\n\nDelete old ones weekly."
_LESSON = {"type": "lesson", "applies_to": "product-and-version", "status": "candidate"}


@pytest.fixture(autouse=True)
def stub_embedding() -> Iterator[AsyncMock]:
    fake = fake_embedding_service()
    with patch("meho_backplane.retrieval.indexer.get_embedding_service", return_value=fake):
        yield fake.encode_one


async def _seed_tenant(tenant_id: uuid.UUID, wiki: str | None) -> None:
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            Tenant(
                id=tenant_id,
                slug=f"tenant-{tenant_id.hex[-4:]}",
                name="Test tenant",
                knowledge_wiki_collection=wiki,
            )
        )


def _operator(
    tenant_id: uuid.UUID = OPERATOR_TENANT_ID,
    capabilities: frozenset[str] = _ENTITLED,
) -> Operator:
    return Operator(
        sub="op-test",
        name="Test",
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
        capabilities=capabilities,
    )


def _store_hit(
    tenant_id: uuid.UUID = OPERATOR_TENANT_ID,
    slug: str = "vcenter-snapshot-notes",
    metadata: dict[str, Any] | None = None,
) -> RetrievalHit:
    ts = datetime(2026, 11, 8, tzinfo=UTC)
    return RetrievalHit(
        document_id=uuid.uuid4(),
        tenant_id=tenant_id,
        source="kb",
        source_id=slug,
        kind="kb-entry",
        body="Snapshot notes from the store.",
        doc_metadata=metadata or {},
        created_at=ts,
        updated_at=ts,
        fused_score=0.5,
        bm25_score=0.5,
        cosine_score=0.5,
        bm25_rank=1,
        cosine_rank=1,
    )


def _corpus(*chunks: CorpusChunk) -> AsyncMock:
    return AsyncMock(return_value=CorpusSearchResponse(chunks=list(chunks)))


def _page(text: str | None, **fields: Any) -> AsyncMock:
    """A ``read_corpus`` stand-in that returns *text* as the whole wiki page."""
    reply = {"mode": "page", "text": text, "disclosure": "full", **fields}
    return AsyncMock(return_value=UpstreamRead.model_validate(reply))


async def _seed_wiki() -> None:
    """The caller's tenant keeps its lessons in ``_WIKI``, a collection that offers read."""
    await _seed_tenant(OPERATOR_TENANT_ID, _WIKI)
    await seed_doc_collection(collection_key=_WIKI, backend=_READ_ON)


async def _seed_import(
    path: str = _PAGE_PATH,
    body: str = "Keep at most 32 snapshots\nper VM.",
    tenant_id: uuid.UUID = OPERATOR_TENANT_ID,
) -> str:
    """Import one confirmed page into the store, as the wiki's CI job does; return its slug."""
    header = "type: lesson\napplies_to: product-and-version\nstatus: confirmed\n"
    page = KbImportPage(path=path, content=f"---\n{header}---\n{body}\n")
    result = await import_pages(
        KbImportRequest(pages=[page]), tenant_id=tenant_id, actor_sub="kb-import"
    )
    (written,) = result.pages
    assert written.action == "create", written
    assert written.slug is not None
    return written.slug


async def _search(
    operator: Operator,
    *,
    store: list[RetrievalHit] | None = None,
    corpus: AsyncMock | None = None,
    read: AsyncMock | None = None,
    **arguments: Any,
) -> tuple[dict[str, Any], AsyncMock]:
    corpus = corpus if corpus is not None else _corpus(_WIKI_CHUNK)
    hits = store if store is not None else [_store_hit(operator.tenant_id)]
    read = read if read is not None else _page(_WIKI_CHUNK.content)
    with (
        patch(_STORE_SEAM, new=AsyncMock(return_value=hits)),
        patch(_CORPUS_SEAM, new=corpus),
        patch(_READ_SEAM, new=read),
    ):
        response = await _search_knowledge_handler(operator, {"query": "snapshot", **arguments})
    return response, corpus


# --- the setting in the settings file -------------------------------------------


def test_the_setting_is_one_short_key() -> None:
    too_long = "w" * 129
    parsed = parse_settings_file(
        b"apiVersion: meho.evoila.com/v1\nkind: Settings\nspec:\n  tenants:\n"
        + f"    - policies: {{knowledge_wiki_collection: {too_long}}}\n".encode()
    )
    assert len(parsed.mistakes) == 1
    assert "knowledge_wiki_collection" in parsed.mistakes[0]
    assert too_long not in parsed.mistakes[0]


# --- the setting is empty: as before ------------------------------------------


@pytest.mark.asyncio
async def test_without_the_setting_both_tools_answer_as_before() -> None:
    await _seed_tenant(OPERATOR_TENANT_ID, None)
    await seed_doc_collection(collection_key=_WIKI)
    operator = _operator()
    response, corpus = await _search(operator)
    assert "wiki" not in response
    assert [hit["slug"] for hit in response["hits"]] == ["vcenter-snapshot-notes"]
    corpus.assert_not_called()

    added = await _add_to_knowledge_handler(
        operator, {"slug": "new-lesson", "body": "A lesson.", "metadata": dict(_LESSON)}
    )
    assert "wiki" not in added
    assert added["slug"] == "new-lesson"


@pytest.mark.asyncio
async def test_a_tenant_without_a_row_answers_as_before() -> None:
    response, corpus = await _search(_operator())
    assert "wiki" not in response
    corpus.assert_not_called()


# --- the setting is set and the entitlement held ------------------------------


@pytest.mark.asyncio
async def test_wiki_hits_come_next_to_the_store_hits() -> None:
    await _seed_tenant(OPERATOR_TENANT_ID, _WIKI)
    await seed_doc_collection(collection_key=_WIKI)
    response, corpus = await _search(_operator())

    assert corpus.await_args is not None
    assert corpus.await_args.args[1] == "snapshot"
    store_hit, wiki_hit = response["hits"]
    assert store_hit["slug"] == "vcenter-snapshot-notes"
    assert "source" not in store_hit
    assert wiki_hit["source"] == "wiki"
    assert wiki_hit["collection"] == _WIKI
    assert wiki_hit["source_url"].endswith(f"/blob/abc123/{_PAGE_PATH}")
    assert wiki_hit["title"] == "Snapshot limits"
    assert wiki_hit["content"].startswith(BLOCK_START)
    assert response["wiki"] == {
        "collection": _WIKI,
        "searched": True,
        "reason": None,
        "message": None,
        "hit_count": 1,
        "duplicate_count": 0,
    }


@pytest.mark.asyncio
async def test_an_imported_copy_of_a_wiki_page_is_shown_once() -> None:
    await _seed_wiki()
    assert await _seed_import() == "snapshot-limits"
    imported = stamp_provenance(
        {"status": "confirmed"},
        source_kind="import",
        relative_path=_PAGE_PATH,
        ingested_at=datetime(2026, 11, 8, tzinfo=UTC),
    )
    other_page = _WIKI_CHUNK.model_copy(
        update={
            "chunk_id": "wiki-other-0001",
            "source_url": "https://git.example.com/team/wiki/-/blob/abc123/products/x/limits.md",
        }
    )
    response, _ = await _search(
        _operator(),
        store=[_store_hit(slug="snapshot-limits", metadata=imported)],
        corpus=_corpus(_WIKI_CHUNK, other_page),
    )
    assert [hit.get("slug") or hit["chunk_id"] for hit in response["hits"]] == [
        "snapshot-limits",
        "wiki-other-0001",
    ]
    assert response["wiki"]["hit_count"] == 1
    assert response["wiki"]["duplicate_count"] == 1


@pytest.mark.asyncio
async def test_a_page_only_named_alike_is_not_a_copy() -> None:
    """``limits.md`` is not ``snapshot-limits.md``: only a whole path matches."""
    await _seed_wiki()
    slug = await _seed_import("limits.md")
    imported = stamp_provenance(
        {},
        source_kind="import",
        relative_path="limits.md",
        ingested_at=datetime(2026, 11, 8, tzinfo=UTC),
    )
    response, _ = await _search(_operator(), store=[_store_hit(slug=slug, metadata=imported)])
    assert response["wiki"]["duplicate_count"] == 0
    assert response["wiki"]["hit_count"] == 1


def _imported(path: str) -> dict[str, object]:
    return stamp_provenance(
        {},
        source_kind="import",
        relative_path=path,
        ingested_at=datetime(2026, 11, 8, tzinfo=UTC),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "imported_path",
    ["snapshot-limits.md", "vcenter/snapshot-limits.md", "products/nsx/snapshot-limits.md"],
)
async def test_the_same_file_name_in_another_folder_is_not_a_copy(imported_path: str) -> None:
    """Only the whole page path matches, not the end of it."""
    await _seed_wiki()
    slug = await _seed_import(imported_path)
    hit = _store_hit(slug=slug, metadata=_imported(imported_path))
    response, _ = await _search(_operator(), store=[hit])
    assert response["wiki"]["duplicate_count"] == 0
    assert response["wiki"]["hit_count"] == 1


@pytest.mark.asyncio
async def test_a_shared_import_of_another_tenant_is_not_a_copy() -> None:
    """Another tenant's import comes from that tenant's wiki, even with the same path."""
    await _seed_wiki()
    slug = await _seed_import()
    shared = _store_hit(tenant_id=_TENANT_B, slug=slug, metadata=_imported(_PAGE_PATH))
    response, _ = await _search(_operator(), store=[shared])
    assert response["wiki"]["duplicate_count"] == 0
    assert response["wiki"]["hit_count"] == 1


@pytest.mark.asyncio
async def test_a_hit_without_a_page_link_matches_only_by_its_whole_document_id() -> None:
    await _seed_wiki()
    same = _WIKI_CHUNK.model_copy(
        update={"chunk_id": "same", "source_url": None, "document_id": _PAGE_PATH}
    )
    other = _WIKI_CHUNK.model_copy(
        update={"chunk_id": "other", "source_url": None, "document_id": f"x/{_PAGE_PATH}"}
    )
    slug = await _seed_import()
    response, _ = await _search(
        _operator(),
        store=[_store_hit(slug=slug, metadata=_imported(_PAGE_PATH))],
        corpus=_corpus(same, other),
    )
    assert [hit.get("chunk_id") for hit in response["hits"][1:]] == ["other"]
    assert response["wiki"]["duplicate_count"] == 1


@pytest.mark.asyncio
async def test_the_same_path_from_another_repository_is_not_a_copy() -> None:
    """A page with this path imported from another repository has other text: it stays."""
    await _seed_wiki()
    slug = await _seed_import(body="Notes from another repository about snapshots.")
    response, _ = await _search(
        _operator(), store=[_store_hit(slug=slug, metadata=_imported(_PAGE_PATH))]
    )
    assert [hit.get("slug") or hit["chunk_id"] for hit in response["hits"]] == [
        "snapshot-limits",
        _WIKI_CHUNK.chunk_id,
    ]
    assert response["wiki"]["duplicate_count"] == 0
    assert response["wiki"]["hit_count"] == 1


@pytest.mark.asyncio
async def test_a_shared_paragraph_at_the_same_path_is_not_a_copy() -> None:
    """Pages from two repositories share a paragraph but differ elsewhere: the wiki hit stays."""
    await _seed_wiki()
    slug = await _seed_import(
        body="# Snapshot limits\n\nKeep at most 32 snapshots per VM.\n\nCheck them monthly."
    )
    response, _ = await _search(
        _operator(),
        store=[_store_hit(slug=slug, metadata=_imported(_PAGE_PATH))],
        read=_page(_LONG_PAGE),
    )
    wiki_hit = response["hits"][1]
    assert wiki_hit["chunk_id"] == _WIKI_CHUNK.chunk_id
    assert wiki_hit["source_url"].endswith(f"/blob/abc123/{_PAGE_PATH}")
    assert response["wiki"]["duplicate_count"] == 0
    assert response["wiki"]["hit_count"] == 1


@pytest.mark.asyncio
async def test_an_import_whose_stored_text_is_missing_is_not_a_copy() -> None:
    """The store hit says it is an import, but no stored text proves it: the wiki hit stays."""
    await _seed_wiki()
    response, _ = await _search(
        _operator(), store=[_store_hit(slug="snapshot-limits", metadata=_imported(_PAGE_PATH))]
    )
    assert response["wiki"]["duplicate_count"] == 0
    assert response["wiki"]["hit_count"] == 1


#: A lesson header at the top of a wiki page; the import does not store it in the text.
_HEADER = "---\ntype: lesson\napplies_to: product-and-version\nstatus: confirmed\n---\n"
_UMLAUT = "Größe: höchstens 32 Snapshots je VM, älteste zuerst löschen."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored_body", "hit_text", "page_text", "duplicates"),
    [
        pytest.param(
            "Keep at most 32 snapshots per VM.",
            "Keep at most 32 snapshots per VM.",
            _LONG_PAGE,
            0,
            id="short-copy-is-one-paragraph-of-a-longer-page",
        ),
        pytest.param(
            "# Snapshot limits", "# Snapshot limits", _LONG_PAGE, 0, id="hit-at-the-start"
        ),
        pytest.param(
            "Delete old ones weekly.", "Delete old ones weekly.", _LONG_PAGE, 0, id="hit-at-the-end"
        ),
        pytest.param(
            "# Snapshot limits", "# Snapshot limits", "# Snapshot limits", 1, id="only-a-title"
        ),
        pytest.param(_LONG_PAGE, "# Snapshot limits", _LONG_PAGE, 1, id="start-of-a-whole-copy"),
        pytest.param(
            _LONG_PAGE, "Delete old ones weekly.", _LONG_PAGE, 1, id="end-of-a-whole-copy"
        ),
        pytest.param(
            "Keep at most 32 snapshots per VM.",
            "Keep at most 32 snapshots per VM.",
            f"{_HEADER}Keep at most 32 snapshots per VM.\n",
            1,
            id="page-with-a-lesson-header",
        ),
        pytest.param(
            _UMLAUT, _UMLAUT, unicodedata.normalize("NFD", _UMLAUT), 1, id="non-ascii-copy"
        ),
        pytest.param(
            _UMLAUT, _UMLAUT, f"{_UMLAUT}\n\nÄltere Notizen folgen.", 0, id="non-ascii-longer"
        ),
        pytest.param(
            "Keep at most 32 snapshots per VM.",
            "Keep at most 32 snapshots per VM.",
            "",
            0,
            id="empty-page",
        ),
        pytest.param(
            "Keep at most 32 snapshots per VM.",
            " \n ",
            "Keep at most 32 snapshots per VM.",
            0,
            id="empty-hit",
        ),
    ],
)
async def test_a_wiki_hit_is_left_out_only_when_the_whole_page_is_the_copy(
    stored_body: str, hit_text: str, page_text: str, duplicates: int
) -> None:
    """The hit is one part of the wiki page: only the whole page proves the copy."""
    await _seed_wiki()
    slug = await _seed_import(body=stored_body)
    read = _page(page_text)
    response, _ = await _search(
        _operator(),
        store=[_store_hit(slug=slug, metadata=_imported(_PAGE_PATH))],
        corpus=_corpus(_WIKI_CHUNK.model_copy(update={"content": hit_text})),
        read=read,
    )
    assert response["wiki"]["duplicate_count"] == duplicates
    assert response["wiki"]["hit_count"] == 1 - duplicates
    if not duplicates:
        wiki_hit = response["hits"][1]
        assert wiki_hit["source_url"].endswith(f"/blob/abc123/{_PAGE_PATH}")
        assert wiki_hit["read_handle"] == _HANDLE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "read",
    [
        pytest.param(_page(_WIKI_CHUNK.content, truncated=True), id="page-cut"),
        pytest.param(_page(_WIKI_CHUNK.content, next="next-cursor-token"), id="more-to-read"),
        pytest.param(_page(None, disclosure="link"), id="link-only"),
        pytest.param(AsyncMock(side_effect=CorpusUnavailable("down")), id="index-down"),
        pytest.param(
            AsyncMock(
                side_effect=CorpusReadError("x", kind=CorpusReadError.KIND_RATE_LIMITED, status=429)
            ),
            id="read-refused",
        ),
    ],
)
async def test_a_wiki_hit_stays_when_its_whole_page_cannot_be_read(read: AsyncMock) -> None:
    await _seed_wiki()
    slug = await _seed_import()
    response, _ = await _search(
        _operator(), store=[_store_hit(slug=slug, metadata=_imported(_PAGE_PATH))], read=read
    )
    read.assert_awaited_once()
    assert response["wiki"]["duplicate_count"] == 0
    assert response["wiki"]["hit_count"] == 1
    assert response["hits"][0]["slug"] == slug


@pytest.mark.asyncio
async def test_a_wiki_hit_stays_when_the_collection_offers_no_read() -> None:
    """Without read the hit has no read handle, so the page cannot be compared."""
    await _seed_tenant(OPERATOR_TENANT_ID, _WIKI)
    await seed_doc_collection(collection_key=_WIKI)
    slug = await _seed_import()
    read = _page(_WIKI_CHUNK.content)
    response, _ = await _search(
        _operator(), store=[_store_hit(slug=slug, metadata=_imported(_PAGE_PATH))], read=read
    )
    read.assert_not_called()
    assert response["wiki"]["duplicate_count"] == 0
    assert response["wiki"]["hit_count"] == 1


@pytest.mark.asyncio
async def test_a_page_in_several_hits_is_read_once_and_left_out_whole() -> None:
    await _seed_wiki()
    slug = await _seed_import(body=_LONG_PAGE)
    first = _WIKI_CHUNK.model_copy(update={"content": "# Snapshot limits"})
    second = _WIKI_CHUNK.model_copy(
        update={"chunk_id": "wiki-snapshot-limits-0002", "content": "Delete old ones weekly."}
    )
    read = _page(_LONG_PAGE)
    response, _ = await _search(
        _operator(),
        store=[_store_hit(slug=slug, metadata=_imported(_PAGE_PATH))],
        corpus=_corpus(first, second),
        read=read,
    )
    read.assert_awaited_once()
    assert response["wiki"]["duplicate_count"] == 2
    assert response["wiki"]["hit_count"] == 0


@pytest.mark.asyncio
async def test_pages_at_one_path_in_two_repositories_are_each_read() -> None:
    """Two hits share a path, one page per repository: only the proven copy is left out."""
    await _seed_wiki()
    slug = await _seed_import(body=_WIKI_CHUNK.content)
    other_handle = "eyJ2IjoxLCJ0IjoiaCJ9.b3RoZXItaGFuZGxl"
    other = _WIKI_CHUNK.model_copy(
        update={
            "chunk_id": "wiki-snapshot-limits-other",
            "source_url": f"https://git.example.com/other/wiki/-/blob/def456/{_PAGE_PATH}",
            "read_handle": other_handle,
        }
    )
    pages = {_HANDLE: _WIKI_CHUNK.content, other_handle: _LONG_PAGE}

    async def read_page(_operator: Operator, handle: str, **_: Any) -> UpstreamRead:
        return UpstreamRead.model_validate(
            {"mode": "page", "text": pages[handle], "disclosure": "full"}
        )

    read = AsyncMock(side_effect=read_page)
    response, _ = await _search(
        _operator(),
        store=[_store_hit(slug=slug, metadata=_imported(_PAGE_PATH))],
        corpus=_corpus(_WIKI_CHUNK, other),
        read=read,
    )
    assert read.await_count == 2
    assert response["wiki"]["duplicate_count"] == 1
    assert response["wiki"]["hit_count"] == 1
    wiki_hit = response["hits"][1]
    assert wiki_hit["chunk_id"] == other.chunk_id
    assert wiki_hit["source_url"] == other.source_url
    assert wiki_hit["read_handle"] == other_handle


@pytest.mark.asyncio
async def test_the_size_limit_counts_the_wiki_status() -> None:
    """The final answer, ``wiki`` included, stays within the limit."""
    await _seed_tenant(OPERATOR_TENANT_ID, _WIKI)
    await seed_doc_collection(collection_key=_WIKI)
    whole, _ = await _search(_operator())
    without_wiki = {key: value for key, value in whole.items() if key != "wiki"}
    limit = measure_delivered_wire_bytes(without_wiki, structured=False)

    with patch.object(get_settings(), "result_complete_response_wire_max_bytes", limit):
        response, _ = await _search(_operator())

    assert measure_delivered_wire_bytes(response, structured=False) <= limit
    assert response["delivery"] == "partial"
    assert response["omitted_hits"] == 1
    assert response["wiki"]["collection"] == _WIKI
    assert [hit["slug"] for hit in response["hits"]] == ["vcenter-snapshot-notes"]


# --- the wiki is not searched: the answer says why ----------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("capabilities", "status", "seed", "reason", "words"),
    [
        (frozenset(), "ready", True, "docs_addon_off", "capability `meho-docs`"),
        (frozenset({"meho-docs"}), "ready", True, "not_entitled", f"meho-docs:{_WIKI}"),
        (_ENTITLED, "ready", False, "unknown_collection", "no docs collection"),
        (_ENTITLED, "rebuilding", True, "collection_not_ready", "not ready"),
        (_ENTITLED, "disabled", True, "collection_not_ready", "turned off"),
    ],
)
async def test_store_hits_come_back_and_the_answer_says_why(
    capabilities: frozenset[str], status: str, seed: bool, reason: str, words: str
) -> None:
    await _seed_tenant(OPERATOR_TENANT_ID, _WIKI)
    if seed:
        await seed_doc_collection(collection_key=_WIKI, status=status)
    response, corpus = await _search(_operator(capabilities=capabilities))
    corpus.assert_not_called()
    assert [hit["slug"] for hit in response["hits"]] == ["vcenter-snapshot-notes"]
    wiki = response["wiki"]
    assert (wiki["searched"], wiki["reason"], wiki["hit_count"]) == (False, reason, 0)
    assert f"docs collection `{_WIKI}`) was not searched" in wiki["message"]
    assert words in wiki["message"]


@pytest.mark.asyncio
async def test_index_down_returns_store_hits_and_says_why() -> None:
    await _seed_tenant(OPERATOR_TENANT_ID, _WIKI)
    await seed_doc_collection(collection_key=_WIKI)
    down = AsyncMock(side_effect=CorpusUnavailable("corpus unreachable: ConnectError"))
    response, _ = await _search(_operator(), corpus=down)
    assert [hit["slug"] for hit in response["hits"]] == ["vcenter-snapshot-notes"]
    assert response["wiki"]["reason"] == "index_unavailable"
    assert "did not answer" in response["wiki"]["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {"product": "vcenter"},
        {"filters": {"kind": "kb-entry"}},
        {"scope": "shared"},
        {"updated_after": "2026-11-01"},
    ],
)
async def test_filters_skip_the_wiki_and_say_so(arguments: dict[str, Any]) -> None:
    await _seed_tenant(OPERATOR_TENANT_ID, _WIKI)
    await seed_doc_collection(collection_key=_WIKI)
    response, corpus = await _search(_operator(), **arguments)
    corpus.assert_not_called()
    assert response["wiki"]["reason"] == "filtered"
    assert "cannot apply filters" in response["wiki"]["message"]


@pytest.mark.asyncio
async def test_scope_tenant_still_searches_the_wiki() -> None:
    await _seed_tenant(OPERATOR_TENANT_ID, _WIKI)
    await seed_doc_collection(collection_key=_WIKI)
    response, _ = await _search(_operator(), scope="tenant")
    assert response["wiki"]["searched"] is True


# --- the setting is per tenant --------------------------------------------------


@pytest.mark.asyncio
async def test_each_tenant_gets_its_own_wiki() -> None:
    await _seed_tenant(OPERATOR_TENANT_ID, _WIKI)
    await _seed_tenant(_TENANT_B, "other-wiki")
    await seed_doc_collection(collection_key=_WIKI)
    await seed_doc_collection(collection_key="other-wiki")
    tenant_c = uuid.UUID("00000000-0000-0000-0000-00000000c0c0")
    await _seed_tenant(tenant_c, None)
    caps = frozenset({"meho-docs", f"meho-docs:{_WIKI}", "meho-docs:other-wiki"})

    a, _ = await _search(_operator(OPERATOR_TENANT_ID, caps))
    b, _ = await _search(_operator(_TENANT_B, caps))
    c, corpus_c = await _search(_operator(tenant_c, caps))

    assert a["wiki"]["collection"] == _WIKI
    assert a["hits"][1]["collection"] == _WIKI
    assert b["wiki"]["collection"] == "other-wiki"
    assert b["hits"][1]["collection"] == "other-wiki"
    assert "wiki" not in c
    corpus_c.assert_not_called()


# --- add_to_knowledge on a wiki tenant -------------------------------------------


@pytest.mark.asyncio
async def test_add_to_knowledge_stores_and_says_the_lesson_goes_to_the_wiki() -> None:
    await _seed_tenant(OPERATOR_TENANT_ID, _WIKI)
    operator = _operator()
    added = await _add_to_knowledge_handler(
        operator,
        {"slug": "snapshot-revert", "body": "Revert snapshots one by one.", "metadata": _LESSON},
    )
    assert added["provenance"]["source_kind"] == "direct"
    assert added["wiki"]["collection"] == _WIKI
    assert added["wiki"]["export"] == "page"
    assert added["wiki"]["message"] == (
        "Stored. This tenant keeps its lessons in the wiki behind collection "
        f"`{_WIKI}`. This lesson will also appear there as a candidate page for review."
    )
    stored = await KbService().get_entry(tenant_id=OPERATOR_TENANT_ID, slug="snapshot-revert")
    assert stored is not None
    assert stored.provenance.source_kind == "direct"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("metadata", "export", "words"),
    [
        (None, "none", "This lesson stays in MEHO"),
        ({**_LESSON, "applies_to": "customer"}, "none", "This lesson stays in MEHO"),
        ({"applies_to": "product-and-version", "status": "candidate"}, "skip", "missing: type"),
    ],
)
async def test_add_to_knowledge_says_when_the_lesson_does_not_go_to_the_wiki(
    metadata: dict[str, Any] | None, export: str, words: str
) -> None:
    await _seed_tenant(OPERATOR_TENANT_ID, _WIKI)
    added = await _add_to_knowledge_handler(
        _operator(), {"slug": "a-lesson", "body": "Text.", "metadata": metadata}
    )
    assert added["slug"] == "a-lesson"
    assert added["wiki"]["export"] == export
    assert f"collection `{_WIKI}`" in added["wiki"]["message"]
    assert words in added["wiki"]["message"]


# --- the audit row --------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_audit_row_names_the_wiki_collection() -> None:
    await _seed_tenant(OPERATOR_TENANT_ID, _WIKI)
    await seed_doc_collection(collection_key=_WIKI)
    operator = _operator()

    async def _fake_verify() -> Operator:
        return operator

    app.dependency_overrides[verify_mcp_jwt_and_bind] = _fake_verify
    try:
        with (
            TestClient(app) as client,
            patch(_STORE_SEAM, new=AsyncMock(return_value=[_store_hit()])),
            patch(_CORPUS_SEAM, new=_corpus(_WIKI_CHUNK)),
        ):
            response = post_mcp(
                client,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "search_knowledge", "arguments": {"query": "snapshot"}},
                },
            )
    finally:
        app.dependency_overrides.pop(verify_mcp_jwt_and_bind, None)
    assert response.json()["result"]["isError"] is False
    payload = json.loads(response.json()["result"]["content"][0]["text"])
    assert payload["wiki"]["hit_count"] == 1

    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(AuditLog))).scalars().all()
    row = next(r for r in rows if r.method == "MCP")
    assert row.payload["op_id"] == "search_knowledge"
    assert row.payload["collection"] == _WIKI
    assert row.payload["wiki_status"] == "searched"
    assert row.payload["wiki_hit_count"] == 1
    assert "snapshot" not in json.dumps(row.payload)


# --- meho kb search (POST /api/v1/retrieve) ------------------------------------


def _retrieve_client(operator: Operator) -> TestClient:
    rest = FastAPI()
    rest.include_router(retrieve_router)

    async def _fake_verify() -> Operator:
        return operator

    rest.dependency_overrides[verify_jwt_and_bind] = _fake_verify
    return TestClient(rest)


@pytest.mark.asyncio
async def test_kb_search_over_rest_gives_the_same_wiki_result() -> None:
    await _seed_tenant(OPERATOR_TENANT_ID, _WIKI)
    await seed_doc_collection(collection_key=_WIKI)
    client = _retrieve_client(_operator())
    with (
        patch(_REST_STORE_SEAM, new=AsyncMock(return_value=[_store_hit()])),
        patch(_CORPUS_SEAM, new=_corpus(_WIKI_CHUNK)),
    ):
        response = client.post("/api/v1/retrieve", json={"query": "snapshot", "source": "kb"})
    assert response.status_code == 200
    body = response.json()
    assert [hit["source_id"] for hit in body["hits"]] == ["vcenter-snapshot-notes"]
    wiki = body["wiki"]
    assert (wiki["collection"], wiki["searched"], wiki["duplicate_count"]) == (_WIKI, True, 0)
    assert wiki["hits"][0]["collection"] == _WIKI
    assert wiki["hits"][0]["source_url"].endswith(f"/blob/abc123/{_PAGE_PATH}")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored_body", "page_text", "duplicates"),
    [
        pytest.param("Keep at most 32 snapshots\nper VM.", _WIKI_CHUNK.content, 1, id="whole-copy"),
        pytest.param(
            "Notes from another repository about snapshots.",
            _WIKI_CHUNK.content,
            0,
            id="other-text",
        ),
        pytest.param(
            "# Snapshot limits\n\nKeep at most 32 snapshots per VM.\n\nOther advice.",
            _WIKI_CHUNK.content,
            0,
            id="copy-holds-more",
        ),
        pytest.param(
            "Keep at most 32 snapshots per VM.",
            _LONG_PAGE,
            0,
            id="short-copy-is-one-paragraph-of-a-longer-page",
        ),
    ],
)
async def test_kb_search_over_rest_leaves_out_only_proven_copies(
    stored_body: str, page_text: str, duplicates: int
) -> None:
    await _seed_wiki()
    slug = await _seed_import(body=stored_body)
    client = _retrieve_client(_operator())
    store = [_store_hit(slug=slug, metadata=_imported(_PAGE_PATH))]
    with (
        patch(_REST_STORE_SEAM, new=AsyncMock(return_value=store)),
        patch(_CORPUS_SEAM, new=_corpus(_WIKI_CHUNK)),
        patch(_READ_SEAM, new=_page(page_text)),
    ):
        response = client.post("/api/v1/retrieve", json={"query": "snapshot", "source": "kb"})
    wiki = response.json()["wiki"]
    assert wiki["duplicate_count"] == duplicates
    assert len(wiki["hits"]) == 1 - duplicates


@pytest.mark.asyncio
async def test_kb_search_over_rest_says_why_the_wiki_was_not_searched() -> None:
    await _seed_tenant(OPERATOR_TENANT_ID, _WIKI)
    await seed_doc_collection(collection_key=_WIKI)
    client = _retrieve_client(_operator(capabilities=frozenset({"meho-docs"})))
    with patch(_REST_STORE_SEAM, new=AsyncMock(return_value=[])):
        response = client.post("/api/v1/retrieve", json={"query": "snapshot", "source": "kb"})
    wiki = response.json()["wiki"]
    assert (wiki["searched"], wiki["reason"], wiki["hits"]) == (False, "not_entitled", [])


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["kb", "memory", None])
async def test_rest_without_the_setting_has_no_wiki(source: str | None) -> None:
    await _seed_tenant(OPERATOR_TENANT_ID, None)
    client = _retrieve_client(_operator())
    corpus = _corpus(_WIKI_CHUNK)
    with (
        patch(_REST_STORE_SEAM, new=AsyncMock(return_value=[])),
        patch(_CORPUS_SEAM, new=corpus),
    ):
        response = client.post("/api/v1/retrieve", json={"query": "snapshot", "source": source})
    assert response.status_code == 200
    assert response.json()["wiki"] is None
    corpus.assert_not_called()
