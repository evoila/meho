# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the read seam and the shared ``read_docs`` service (#3948).

* ``SearchBackend.supports_read`` / ``read`` are off by default.
* ``CorpusHttpBackend`` reads only with ``backend.ref["read"] == "upstream"``,
  at ``read_endpoint``, else the search URL with its last segment replaced by
  ``read`` (ref endpoint, else the legacy global corpus URL).
* ``read_docs`` refuses a collection without read, sends the hard filters
  only when the collection's scope gates send filters, maps the backend's
  refusals onto the typed errors, and never returns a storage path.
* ``read_handle`` rides a hit (search and the upstream answer's hits) only
  when the collection offers read.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest

from meho_backplane.auth.corpus import (
    CorpusChunk,
    CorpusReadError,
    CorpusSearchResponse,
    CorpusUnavailable,
    UpstreamAnswer,
    UpstreamRead,
)
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.docs_collections import DocCollection
from meho_backplane.docs_search import (
    DocsReadNotFoundError,
    DocsReadRateLimitedError,
    DocsReadSearchAgainError,
    build_docs_scope,
    read_docs,
    search_docs,
)
from meho_backplane.docs_search.answer import answer_docs_question
from meho_backplane.docs_search.backends import CorpusHttpBackend, SearchBackend
from meho_backplane.docs_search.service import _project_chunk
from meho_backplane.settings import get_settings

_READ_SEAM = "meho_backplane.docs_search.backends.corpus_http.read_corpus"
_SEARCH_SEAM = "meho_backplane.docs_search.backends.corpus_http.search_corpus"
_ASK_SEAM = "meho_backplane.docs_search.backends.corpus_http.ask_corpus"
_LEGACY_URL = "https://legacy-corpus.test/v1/search"
_HANDLE = "eyJ2IjoxfQ.c2lnbmF0dXJl"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("CORPUS_URL", _LEGACY_URL)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _operator() -> Operator:
    return Operator(
        sub="op-7",
        raw_jwt="header.payload.signature",
        tenant_id=uuid4(),
        tenant_role=TenantRole.OPERATOR,
        capabilities=frozenset({"meho-docs", "meho-docs:vmware"}),
    )


def _collection(ref: Mapping[str, Any] | None) -> DocCollection:
    now = datetime.now(UTC)
    backend: dict[str, Any] = {"type": "corpus-http"}
    if ref is not None:
        backend["ref"] = dict(ref)
    return DocCollection(
        id=uuid4(),
        tenant_id=None,
        collection_key="vmware",
        vendor="VMware by Broadcom",
        products=("vsphere",),
        description="VMware docs.",
        when_to_use="Vendor product questions.",
        backend=backend,
        status="ready",
        last_ingested_at=None,
        doc_count=None,
        readiness=None,
        extras={},
        created_at=now,
        updated_at=now,
    )


class _FakeRead:
    """A recording ``read_corpus`` stand-in."""

    def __init__(self, result: Mapping[str, Any] | Exception) -> None:
        self._result = result
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, operator: Operator, read_handle: str, **kwargs: Any) -> UpstreamRead:
        self.calls.append({"read_handle": read_handle, **kwargs})
        if isinstance(self._result, Exception):
            raise self._result
        return UpstreamRead.model_validate(self._result)


_READ_REPLY: dict[str, Any] = {
    "mode": "section",
    "text": "Section text.",
    "title": "Widgets",
    "source_uri": "gs://private-bucket/docs/widgets.html",
    "disclosure": "full",
    "truncated": False,
    "next": None,
    "up": "up-cursor",
}


# ---------------------------------------------------------------------------
# The backend seam
# ---------------------------------------------------------------------------


class _SearchOnly(SearchBackend):
    backend_type = "search-only-test"

    async def search(self, operator: Operator, query: str, **_kw: Any) -> CorpusSearchResponse:
        return CorpusSearchResponse(chunks=[])


@pytest.mark.asyncio
async def test_search_backend_read_is_off_by_default() -> None:
    backend = _SearchOnly()
    assert backend.supports_read({"read": "upstream"}) is False
    with pytest.raises(NotImplementedError):
        await backend.read(_operator(), _HANDLE)


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        (None, False),
        ({}, False),
        ({"read": "UPSTREAM"}, False),
        ({"read": True}, False),
        ({"answer": "upstream"}, False),
        ({"read": "upstream"}, True),
    ],
)
def test_corpus_http_reads_only_on_exact_opt_in(ref: dict[str, Any] | None, expected: bool) -> None:
    assert CorpusHttpBackend().supports_read(ref) is expected


@pytest.mark.parametrize(
    ("ref", "expected_url"),
    [
        (
            {"read": "upstream", "read_endpoint": "https://reader.test/v2/read-it"},
            "https://reader.test/v2/read-it",
        ),
        (
            {"read": "upstream", "endpoint": "https://corpus.test/v1/search"},
            "https://corpus.test/v1/read",
        ),
        ({"read": "upstream", "url": "https://corpus.test/search"}, "https://corpus.test/read"),
        ({"read": "upstream"}, "https://legacy-corpus.test/v1/read"),
    ],
)
@pytest.mark.asyncio
async def test_corpus_http_read_url_resolution(ref: dict[str, Any], expected_url: str) -> None:
    """``read_endpoint`` wins; else the search URL's last segment becomes ``read``."""
    fake = _FakeRead(_READ_REPLY)
    with patch(_READ_SEAM, new=fake):
        await CorpusHttpBackend().read(
            _operator(), _HANDLE, backend_ref=ref, mode="page", before=0, after=3
        )
    (call,) = fake.calls
    assert call["read_url"] == expected_url
    assert call["read_handle"] == _HANDLE
    assert call["mode"] == "page"
    assert call["before"] == 0
    assert call["after"] == 3


@pytest.mark.asyncio
async def test_corpus_http_read_without_any_endpoint_is_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ref endpoint and no legacy URL: the transport fails closed."""
    monkeypatch.setenv("CORPUS_URL", "")
    get_settings.cache_clear()
    with pytest.raises(CorpusUnavailable):
        await CorpusHttpBackend().read(_operator(), _HANDLE, backend_ref={"read": "upstream"})


# ---------------------------------------------------------------------------
# The read_docs service
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_docs_refuses_a_collection_without_read() -> None:
    """No opt-in: not found, and the backend is never called."""
    fake = _FakeRead(_READ_REPLY)
    with patch(_READ_SEAM, new=fake), pytest.raises(DocsReadNotFoundError) as exc:
        await read_docs(
            _operator(),
            _HANDLE,
            scope=build_docs_scope("vmware"),
            collection=_collection({"endpoint": "https://corpus.test/search"}),
        )
    assert str(exc.value) == "docs source not found"
    assert fake.calls == []


@pytest.mark.asyncio
async def test_read_docs_projects_the_reply_without_a_storage_path() -> None:
    """A ``gs://`` source with no public link is ``None``; the rest passes through."""
    fake = _FakeRead(_READ_REPLY)
    with patch(_READ_SEAM, new=fake):
        result = await read_docs(
            _operator(),
            _HANDLE,
            scope=build_docs_scope("vmware"),
            collection=_collection({"read": "upstream"}),
            mode="section",
            cursor="c-1",
        )
    assert result.mode == "section"
    assert result.text == "Section text."
    assert result.title == "Widgets"
    assert result.source_url is None
    assert result.up == "up-cursor"
    assert "gs://" not in result.model_dump_json()
    assert fake.calls[0]["cursor"] == "c-1"


@pytest.mark.asyncio
async def test_read_docs_keeps_an_https_source() -> None:
    fake = _FakeRead({**_READ_REPLY, "source_uri": "https://docs.example/widgets"})
    with patch(_READ_SEAM, new=fake):
        result = await read_docs(
            _operator(),
            _HANDLE,
            scope=build_docs_scope("vmware"),
            collection=_collection({"read": "upstream"}),
        )
    assert result.source_url == "https://docs.example/widgets"


@pytest.mark.parametrize(
    ("ref", "expected_filters"),
    [
        ({"read": "upstream"}, None),
        ({"read": "upstream", "scope": "soft"}, None),
        ({"read": "upstream", "scope": "soft", "scope_filters": True}, None),
        ({"read": "upstream", "scope_filters": True}, {"product": "nsx", "version": "9.0"}),
    ],
)
@pytest.mark.asyncio
async def test_read_docs_sends_filters_only_when_search_would(
    ref: dict[str, Any], expected_filters: dict[str, str] | None
) -> None:
    """The handle is bound to the search filters; a soft scope is never sent on a read."""
    fake = _FakeRead(_READ_REPLY)
    with patch(_READ_SEAM, new=fake):
        await read_docs(
            _operator(),
            _HANDLE,
            scope=build_docs_scope("vmware", "nsx", "9.0"),
            collection=_collection(ref),
        )
    assert fake.calls[0]["filters"] == expected_filters
    assert "soft_scope" not in fake.calls[0]


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            CorpusReadError("x", kind=CorpusReadError.KIND_NOT_FOUND, status=404),
            DocsReadNotFoundError,
        ),
        (
            CorpusReadError("x", kind=CorpusReadError.KIND_SEARCH_AGAIN, status=409),
            DocsReadSearchAgainError,
        ),
        (
            CorpusReadError("x", kind=CorpusReadError.KIND_RATE_LIMITED, status=429, retry_after=4),
            DocsReadRateLimitedError,
        ),
        (CorpusUnavailable("down", status=503), CorpusUnavailable),
    ],
)
@pytest.mark.asyncio
async def test_read_docs_maps_backend_refusals(error: Exception, expected: type[Exception]) -> None:
    with patch(_READ_SEAM, new=_FakeRead(error)), pytest.raises(expected) as exc:
        await read_docs(
            _operator(),
            _HANDLE,
            scope=build_docs_scope("vmware"),
            collection=_collection({"read": "upstream"}),
        )
    if isinstance(exc.value, DocsReadRateLimitedError):
        assert exc.value.retry_after == 4
        assert "wait 4 seconds" in str(exc.value)
    assert _HANDLE not in str(exc.value)


# ---------------------------------------------------------------------------
# read_handle on hits
# ---------------------------------------------------------------------------


_HIT = CorpusChunk.model_validate(
    {"chunk_id": "c1", "text": "hit text", "source_uri": "https://d/1", "read_handle": _HANDLE}
)


def test_project_chunk_keeps_the_handle_only_when_readable() -> None:
    assert _project_chunk(_HIT, collection_key="vmware", readable=True).read_handle == _HANDLE
    assert _project_chunk(_HIT, collection_key="vmware").read_handle is None


@pytest.mark.parametrize(("ref", "expected"), [({"read": "upstream"}, _HANDLE), (None, None)])
@pytest.mark.asyncio
async def test_search_docs_hits_carry_the_handle_only_for_read(
    ref: dict[str, Any] | None, expected: str | None
) -> None:
    async def _search(operator: Operator, query: str, **_kw: Any) -> CorpusSearchResponse:
        return CorpusSearchResponse(chunks=[_HIT])

    with patch(_SEARCH_SEAM, new=_search):
        result = await search_docs(
            _operator(), "q", scope=build_docs_scope("vmware"), collection=_collection(ref)
        )
    assert result.chunks[0].read_handle == expected


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ({"answer": "upstream", "read": "upstream"}, _HANDLE),
        ({"answer": "upstream"}, None),
    ],
)
@pytest.mark.asyncio
async def test_upstream_answer_citations_carry_the_handle_only_for_read(
    ref: dict[str, Any], expected: str | None
) -> None:
    """An upstream ``ask_docs`` answer's hits and citations carry the handle the same way."""
    body = {
        "answer": "Widgets pool per cluster [0].",
        "citations": [{"chunk_index": 0, "chunk_id": "c1"}],
        "hits": [
            {
                "chunk_id": "c1",
                "text": "hit text",
                "source_uri": "https://d/1",
                "read_handle": _HANDLE,
            }
        ],
    }

    async def _ask(operator: Operator, query: str, **_kw: Any) -> UpstreamAnswer:
        return UpstreamAnswer.model_validate(body)

    with patch(_ASK_SEAM, new=_ask):
        outcome = await answer_docs_question(
            _operator(),
            "q",
            scope=build_docs_scope("vmware"),
            collection=_collection(ref),
            limit=5,
        )
    assert outcome.error is None
    assert outcome.answer is not None
    assert outcome.answer.citations[0].read_handle == expected
    assert outcome.retrieved_chunks[0].read_handle == expected
