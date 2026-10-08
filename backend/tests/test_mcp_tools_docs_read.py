# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the ``read_docs`` MCP tool and ``read_handle`` on docs hits (#3948).

* ``read_docs`` is in the capability-gated docs add-on: absent without
  ``meho-docs``, with a strict schema when present.
* A read for a read-enabled collection returns the text wrapped in the
  untrusted-text envelope, a public ``source_url`` (never a ``gs://`` path)
  and the cursors; the transport gets the handle, the mode and the window.
* The four refusals (unknown collection, not entitled, a collection without
  read, a handle the backend refuses) are the **same** ``-32602`` error with
  the same message and no ``data``: no probe oracle.
* A handle that is too old is ``-32602`` with ``data.reason ==
  "search_again"``; a rate limit is ``-32000`` with ``retry_after_seconds``.
* ``read_handle`` rides ``search_docs`` hits only for a read-enabled
  collection, and never reaches the audit row or the broadcast feed.

The ``corpus-http`` transports are patched at the adapter's seams
(``corpus_http.read_corpus`` / ``corpus_http.search_corpus``), so the router
-> backend -> transport path runs for real.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.corpus import (
    CorpusChunk,
    CorpusReadError,
    CorpusSearchResponse,
    UpstreamRead,
)
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast.events import BroadcastEvent
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.main import app
from meho_backplane.mcp.auth import verify_mcp_jwt_and_bind
from meho_backplane.mcp.schemas import INTERNAL_ERROR, INVALID_PARAMS
from meho_backplane.untrusted_text import BLOCK_START
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
    seed_doc_collection,
    seeded_operator_tenant,  # noqa: F401 — pytest-discovered fixture
)

_DOCS_CAPABILITY = "meho-docs"
_ENTITLED = frozenset({_DOCS_CAPABILITY, "meho-docs:vmware"})
_READ_SEAM = "meho_backplane.docs_search.backends.corpus_http.read_corpus"
_SEARCH_SEAM = "meho_backplane.docs_search.backends.corpus_http.search_corpus"

#: A collection that opts in to the corpus read endpoint.
_READ_ON: dict[str, Any] = {"type": "corpus-http", "ref": {"read": "upstream"}}
#: Read on, plus hard scope filters (#3912): the read must send the filters.
_READ_AND_FILTERS: dict[str, Any] = {
    "type": "corpus-http",
    "ref": {"read": "upstream", "scope_filters": True},
}

_HANDLE = "eyJ2IjoxLCJ0IjoiaCJ9.c2lnbmVkLWhhbmRsZQ"
_CURSOR = "eyJjIjoyfQ.Y3Vyc29yLXNpZw"

#: JSON-RPC implementation-defined code the dispatcher uses for rate limits.
_RATE_LIMITED = -32000


def _operator(capabilities: frozenset[str]) -> Operator:
    return Operator(
        sub="op-read",
        name="Reader",
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=OPERATOR_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        capabilities=capabilities,
    )


@pytest.fixture
def docs_client(request: pytest.FixtureRequest) -> Iterator[TestClient]:
    """``TestClient`` whose operator holds the parametrised capability set."""
    capabilities: frozenset[str] = getattr(request, "param", _ENTITLED)
    op = _operator(capabilities)

    async def _fake_verify() -> Operator:
        return op

    app.dependency_overrides[verify_mcp_jwt_and_bind] = _fake_verify
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(verify_mcp_jwt_and_bind, None)


@pytest.fixture
def captured_broadcast(monkeypatch: pytest.MonkeyPatch) -> list[BroadcastEvent]:
    """Capture the MCP dispatcher's broadcast events."""
    import meho_backplane.mcp.handlers as handlers_module

    events: list[BroadcastEvent] = []

    async def _capture(event: BroadcastEvent) -> None:
        events.append(event)

    monkeypatch.setattr(handlers_module, "publish_event", _capture)
    return events


def _seed(**kwargs: Any) -> None:
    asyncio.run(seed_doc_collection(**kwargs))


def _fake_read(result: UpstreamRead | Exception) -> Any:
    """A stand-in for ``read_corpus`` that records its call."""
    calls: list[dict[str, Any]] = []

    async def _read(operator: Operator, read_handle: str, **kwargs: Any) -> UpstreamRead:
        calls.append({"operator": operator, "read_handle": read_handle, **kwargs})
        if isinstance(result, Exception):
            raise result
        return result

    _read.calls = calls  # type: ignore[attr-defined]
    return _read


_FULL_READ = UpstreamRead.model_validate(
    {
        "mode": "around",
        "text": "Before.\nThe hit about logical switches.\nAfter.",
        "title": "NSX Configuration Maximums",
        "source_uri": "gs://private-bucket/docs/nsx/maximums.html",
        "upstream_url": "https://docs.example.com/nsx/maximums",
        "disclosure": "full",
        "reason": None,
        "located": True,
        "truncated": True,
        "next": "next-cursor-token",
        "up": None,
    }
)


def _call(client: TestClient, arguments: dict[str, Any], *, call_id: int = 1) -> Any:
    return post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": call_id,
            "method": "tools/call",
            "params": {"name": "read_docs", "arguments": arguments},
        },
    )


def _read_args(**overrides: Any) -> dict[str, Any]:
    return {"collection": "vmware", "read_handle": _HANDLE, **overrides}


# ---------------------------------------------------------------------------
# Listing + schema
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("docs_client", [frozenset()], indirect=True)
def test_read_docs_absent_without_docs_capability(docs_client: TestClient) -> None:
    """The tool is part of the capability-gated docs add-on."""
    response = post_mcp(docs_client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {t["name"] for t in response.json()["result"]["tools"]}
    assert "read_docs" not in names
    assert "search_docs" not in names


def test_read_docs_listed_with_strict_schema(docs_client: TestClient) -> None:
    """Required ``[collection, read_handle]``, bounded window, no MEHO-internal fields."""
    response = post_mcp(docs_client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = {t["name"]: t for t in response.json()["result"]["tools"]}
    tool = tools["read_docs"]
    schema = tool["inputSchema"]
    assert schema["required"] == ["collection", "read_handle"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["mode"]["enum"] == ["around", "page", "section"]
    assert schema["properties"]["before"]["maximum"] == 3
    assert schema["properties"]["after"]["minimum"] == 0
    assert "broadcast_omit_args" not in tool
    assert "required_capability" not in tool


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_read_docs_returns_wrapped_text_and_public_source(docs_client: TestClient) -> None:
    """The text is wrapped as untrusted; the source is the public link, never ``gs://``."""
    _seed(backend=_READ_ON)
    fake = _fake_read(_FULL_READ)
    with patch(_READ_SEAM, new=fake):
        response = _call(
            docs_client,
            _read_args(mode="around", before=2, after=0, cursor=_CURSOR),
        )
    body = response.json()
    assert body["result"]["isError"] is False, body
    payload = json.loads(body["result"]["content"][0]["text"])
    assert payload["text"].startswith(BLOCK_START)
    assert "The hit about logical switches." in payload["text"]
    assert payload["source_url"] == "https://docs.example.com/nsx/maximums"
    assert payload["title"] == "NSX Configuration Maximums"
    assert payload["disclosure"] == "full"
    assert payload["truncated"] is True
    assert payload["located"] is True
    assert payload["next"] == "next-cursor-token"
    assert payload["mode"] == "around"
    assert "gs://" not in json.dumps(payload)

    (call,) = fake.calls  # type: ignore[attr-defined]
    assert call["read_handle"] == _HANDLE
    assert call["mode"] == "around"
    assert call["before"] == 2
    assert call["after"] == 0
    assert call["cursor"] == _CURSOR
    assert call["filters"] is None
    # Derived from the legacy corpus URL: search URL's last segment -> read.
    assert call["read_url"] is None or call["read_url"].endswith("/read")


def test_read_docs_link_only_file_returns_only_its_link(docs_client: TestClient) -> None:
    """A link-only file returns ``text: null``, ``disclosure: link`` and the link."""
    _seed(backend=_READ_ON)
    link_only = UpstreamRead.model_validate(
        {
            "mode": "page",
            "text": "the owner said no text",
            "title": "Partner guide",
            "source_uri": "gs://private-bucket/partner/guide.html",
            "upstream_url": "https://partner.example.com/guide",
            "disclosure": "link",
            "reason": "link_only",
        }
    )
    with patch(_READ_SEAM, new=_fake_read(link_only)):
        response = _call(docs_client, _read_args(mode="page"))
    payload = json.loads(response.json()["result"]["content"][0]["text"])
    assert payload["text"] is None
    assert payload["disclosure"] == "link"
    assert payload["reason"] == "link_only"
    assert payload["source_url"] == "https://partner.example.com/guide"
    assert "the owner said no text" not in json.dumps(payload)


def test_read_docs_sends_search_filters_on_a_scope_filters_collection(
    docs_client: TestClient,
) -> None:
    """The handle is bound to the hit's search filters, so the read sends them."""
    _seed(backend=_READ_AND_FILTERS)
    fake = _fake_read(_FULL_READ)
    with patch(_READ_SEAM, new=fake):
        _call(docs_client, _read_args(product="nsx", version="9.0"))
    (call,) = fake.calls  # type: ignore[attr-defined]
    assert call["filters"] == {"product": "nsx", "version": "9.0"}


# ---------------------------------------------------------------------------
# The not-found rule: one error for every refusal
# ---------------------------------------------------------------------------


def _not_found_error(response: Any) -> dict[str, Any]:
    body = response.json()
    assert "error" in body, body
    error = body["error"]
    assert error["code"] == INVALID_PARAMS
    return error


@pytest.mark.parametrize(
    ("case", "capabilities"),
    [
        ("unknown_collection", _ENTITLED),
        ("not_entitled", frozenset({_DOCS_CAPABILITY})),
        ("read_not_enabled", _ENTITLED),
        ("upstream_404", _ENTITLED),
        ("disabled_collection", _ENTITLED),
    ],
)
def test_every_refusal_is_the_same_not_found_error(
    case: str,
    capabilities: frozenset[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown, not entitled, no read, refused handle, disabled: one identical error."""
    op = _operator(capabilities)

    async def _fake_verify() -> Operator:
        return op

    app.dependency_overrides[verify_mcp_jwt_and_bind] = _fake_verify
    try:
        if case == "read_not_enabled":
            _seed()
        elif case == "disabled_collection":
            _seed(backend=_READ_ON, status="disabled")
        elif case != "unknown_collection":
            _seed(backend=_READ_ON)
        refused = CorpusReadError(
            "corpus read endpoint found no readable source",
            kind=CorpusReadError.KIND_NOT_FOUND,
            status=404,
        )
        fake = _fake_read(refused if case == "upstream_404" else _FULL_READ)
        with TestClient(app) as client, patch(_READ_SEAM, new=fake):
            response = _call(client, _read_args())
    finally:
        app.dependency_overrides.pop(verify_mcp_jwt_and_bind, None)

    error = _not_found_error(response)
    assert error["message"] == "read_docs: docs source not found"
    assert "data" not in error or error["data"] is None
    # Only the upstream-404 case ever reaches the backend.
    assert len(fake.calls) == (1 if case == "upstream_404" else 0)  # type: ignore[attr-defined]


def test_search_again_and_rate_limit_are_actionable(docs_client: TestClient) -> None:
    """409 asks for a new search; 429 is the rate-limited code with the wait."""
    _seed(backend=_READ_ON)
    expired = CorpusReadError("x", kind=CorpusReadError.KIND_SEARCH_AGAIN, status=409)
    with patch(_READ_SEAM, new=_fake_read(expired)):
        response = _call(docs_client, _read_args())
    error = response.json()["error"]
    assert error["code"] == INVALID_PARAMS
    assert error["data"] == {"reason": "search_again"}
    assert "search again" in error["message"]

    limited = CorpusReadError(
        "x", kind=CorpusReadError.KIND_RATE_LIMITED, status=429, retry_after=9
    )
    with patch(_READ_SEAM, new=_fake_read(limited)):
        response = _call(docs_client, _read_args(), call_id=2)
    error = response.json()["error"]
    assert error["code"] == _RATE_LIMITED
    assert error["data"] == {"reason": "rate_limited", "retry_after_seconds": 9}
    assert "wait 9 seconds" in error["message"]
    assert _HANDLE not in json.dumps(error)


def test_not_ready_collection_is_retryable_internal_error(docs_client: TestClient) -> None:
    """A known, entitled collection that is rebuilding stays a retryable -32603."""
    _seed(backend=_READ_ON, status="rebuilding")
    with patch(_READ_SEAM, new=_fake_read(_FULL_READ)):
        response = _call(docs_client, _read_args())
    assert response.json()["error"]["code"] == INTERNAL_ERROR


def test_missing_collection_is_invalid_params(docs_client: TestClient) -> None:
    response = _call(docs_client, {"read_handle": _HANDLE})
    assert response.json()["error"]["code"] == INVALID_PARAMS


# ---------------------------------------------------------------------------
# The handle never leaks: audit row and broadcast feed
# ---------------------------------------------------------------------------


async def _mcp_audit_rows() -> list[AuditLog]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(select(AuditLog).order_by(AuditLog.occurred_at))
        return [row for row in result.scalars().all() if row.method == "MCP"]


def test_read_handle_never_reaches_the_audit_row_or_the_broadcast_feed(
    docs_client: TestClient,
    captured_broadcast: list[BroadcastEvent],
) -> None:
    """The audit row stores a hash; the broadcast params drop the handle and cursor."""
    _seed(backend=_READ_ON)
    with patch(_READ_SEAM, new=_fake_read(_FULL_READ)):
        response = _call(docs_client, _read_args(cursor=_CURSOR))
    assert response.json()["result"]["isError"] is False

    rows = asyncio.run(_mcp_audit_rows())
    read_rows = [r for r in rows if r.path == "/mcp/tools/call/read_docs"]
    assert len(read_rows) == 1
    row_text = json.dumps(read_rows[0].payload)
    assert read_rows[0].payload["op_id"] == "meho.docs.read"
    assert read_rows[0].payload["collection"] == "vmware"
    assert _HANDLE not in row_text
    assert _CURSOR not in row_text

    events = [e for e in captured_broadcast if e.op_id == "read_docs"]
    assert len(events) == 1
    feed = json.dumps(events[0].payload)
    assert _HANDLE not in feed
    assert _CURSOR not in feed
    # The rest of the arguments still reach the feed as before.
    assert "vmware" in feed


# ---------------------------------------------------------------------------
# read_handle on search_docs hits
# ---------------------------------------------------------------------------


_HIT_WITH_HANDLE = CorpusChunk.model_validate(
    {
        "chunk_id": "nsx-max-0007",
        "text": "NSX supports 10,000 logical switches.",
        "source_uri": "gs://private-bucket/docs/nsx/maximums.html",
        "upstream_url": "https://docs.example.com/nsx/maximums",
        "read_handle": _HANDLE,
    }
)


def _search(client: TestClient) -> dict[str, Any]:
    async def _fake_search(operator: Operator, query: str, **_kw: Any) -> CorpusSearchResponse:
        return CorpusSearchResponse(chunks=[_HIT_WITH_HANDLE])

    with patch(_SEARCH_SEAM, new=_fake_search):
        response = post_mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "search_docs",
                    "arguments": {"query": "logical switches", "collection": "vmware"},
                },
            },
        )
    return json.loads(response.json()["result"]["content"][0]["text"])


def test_search_hit_carries_read_handle_for_read_enabled_collection(
    docs_client: TestClient,
) -> None:
    """A read-enabled collection's hit carries the handle unchanged, and no ``gs://``."""
    _seed(backend=_READ_ON)
    payload = _search(docs_client)
    (chunk,) = payload["chunks"]
    assert chunk["read_handle"] == _HANDLE
    assert "gs://" not in json.dumps(payload)


def test_search_hit_has_no_read_handle_without_read(docs_client: TestClient) -> None:
    """A collection without read drops the handle: every handle on the wire is readable."""
    _seed()
    payload = _search(docs_client)
    (chunk,) = payload["chunks"]
    assert chunk["read_handle"] is None
