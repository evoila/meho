# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the ``meho://docs/{...}`` companion resource (G4.5-T4, #1523).

Covers the resource-side acceptance criteria:

* ``meho://docs/{product}/{version}/{chunk_id}`` is registered, gated by
  the SAME ``required_capability="meho-docs"`` as the ``search_docs`` tool:
  absent from ``resources/templates/list`` and 403-on-read for a tenant
  without the capability; present + readable for one with it.
* ``resources/read`` recovers the cited chunk's text (the corpus transport
  has no fetch-by-id endpoint, so the handler re-issues a scoped search
  and matches on ``chunk_id``).
* A ``(product, version, chunk_id)`` that doesn't resolve collapses to
  INVALID_PARAMS "not found" without leaking corpus contents.
* The MEHO-internal RBAC + capability fields are stripped from the wire.

The shared docs-search service federates to an external corpus; these
unit tests mock
:func:`meho_backplane.docs_search.service.search_corpus` so the read
plumbing is exercised without a live corpus.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from meho_backplane.auth.corpus import CorpusChunk, CorpusSearchResponse, CorpusUnavailable
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.main import app
from meho_backplane.mcp.auth import verify_mcp_jwt_and_bind
from meho_backplane.mcp.schemas import INTERNAL_ERROR, INVALID_PARAMS
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

_DOCS_CAPABILITY = "meho-docs"
_DOCS_URI = "meho://docs/nsx/9.0/nsx-9.0-maximums-0007"


def _operator(
    *,
    role: TenantRole = TenantRole.OPERATOR,
    capabilities: frozenset[str] = frozenset(),
) -> Operator:
    return Operator(
        sub="op-test",
        name="Test",
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=OPERATOR_TENANT_ID,
        tenant_role=role,
        capabilities=capabilities,
    )


@pytest.fixture
def docs_client(
    request: pytest.FixtureRequest,
) -> Iterator[tuple[TestClient, Operator]]:
    """``TestClient`` with a capability-parametrised operator (default: unprovisioned)."""
    capabilities: frozenset[str] = getattr(request, "param", frozenset())
    op = _operator(role=TenantRole.OPERATOR, capabilities=capabilities)

    async def _fake_verify() -> Operator:
        return op

    app.dependency_overrides[verify_mcp_jwt_and_bind] = _fake_verify
    try:
        with TestClient(app) as client:
            yield client, op
    finally:
        app.dependency_overrides.pop(verify_mcp_jwt_and_bind, None)


def _fake_corpus(*chunks: CorpusChunk) -> Any:
    """An async stand-in for ``search_corpus`` capturing its call args."""
    captured: dict[str, Any] = {}

    async def _search(operator: Operator, query: str, **kwargs: Any) -> CorpusSearchResponse:
        captured["operator"] = operator
        captured["query"] = query
        captured.update(kwargs)
        return CorpusSearchResponse(chunks=list(chunks))

    _search.captured = captured  # type: ignore[attr-defined]
    return _search


_SAMPLE_CHUNK = CorpusChunk(
    chunk_id="nsx-9.0-maximums-0007",
    document_id="nsx-9.0-config-maximums",
    content="NSX 9.0 supports up to 10,000 logical switches per manager.",
    source_url="https://docs.example.com/nsx/9.0/maximums",
    score=0.91,
)


# ---------------------------------------------------------------------------
# resources/templates/list — capability gate + wire shape
# ---------------------------------------------------------------------------


def test_docs_resource_absent_from_templates_list_when_unprovisioned(
    docs_client: tuple[TestClient, Operator],
) -> None:
    """An operator without ``meho-docs`` never sees the template (true absence)."""
    client, _op = docs_client
    response = post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 1, "method": "resources/templates/list"},
    )
    assert response.status_code == 200
    templates = {t["uriTemplate"] for t in response.json()["result"]["resourceTemplates"]}
    assert "meho://docs/{product}/{version}/{chunk_id}" not in templates


@pytest.mark.parametrize("docs_client", [frozenset({_DOCS_CAPABILITY})], indirect=True)
def test_docs_resource_present_with_stripped_wire_fields_when_provisioned(
    docs_client: tuple[TestClient, Operator],
) -> None:
    """The template appears once provisioned; wire shape drops the gating fields."""
    client, _op = docs_client
    response = post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 1, "method": "resources/templates/list"},
    )
    assert response.status_code == 200
    templates = {t["uriTemplate"]: t for t in response.json()["result"]["resourceTemplates"]}
    template = templates.get("meho://docs/{product}/{version}/{chunk_id}")
    assert template is not None
    assert template["mimeType"] == "text/markdown"
    assert "required_role" not in template
    assert "required_capability" not in template


# ---------------------------------------------------------------------------
# resources/read — capability gate (403 when unprovisioned)
# ---------------------------------------------------------------------------


def test_resources_read_docs_403_when_unprovisioned(
    docs_client: tuple[TestClient, Operator],
) -> None:
    """Reading a known docs URI still 403s when the capability is absent.

    The handler (and the corpus re-search) must never run, so knowing the
    URI cannot bypass the gate.
    """
    client, _op = docs_client
    fake = _fake_corpus(_SAMPLE_CHUNK)
    with patch("meho_backplane.docs_search.service.search_corpus", new=fake):
        response = post_mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/read",
                "params": {"uri": _DOCS_URI},
            },
        )
    assert response.status_code == 200
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "forbidden" in body["error"]["message"].lower()
    assert "capability" in body["error"]["message"].lower()
    assert "query" not in fake.captured  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# resources/read — provisioned happy path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("docs_client", [frozenset({_DOCS_CAPABILITY})], indirect=True)
def test_resources_read_docs_returns_matching_chunk_text(
    docs_client: tuple[TestClient, Operator],
) -> None:
    """A provisioned read recovers the chunk whose id matches the URI.

    The handler rebuilds the binary scope from the URI segments,
    re-issues a scoped corpus search, and returns the exact-id match —
    even when the corpus returns sibling chunks alongside it.
    """
    client, op = docs_client
    other = CorpusChunk(
        chunk_id="nsx-9.0-maximums-0008",
        document_id="nsx-9.0-config-maximums",
        content="An unrelated sibling chunk.",
    )
    fake = _fake_corpus(other, _SAMPLE_CHUNK)
    with patch("meho_backplane.docs_search.service.search_corpus", new=fake):
        response = post_mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "resources/read",
                "params": {"uri": _DOCS_URI},
            },
        )
    assert response.status_code == 200
    body = response.json()
    contents = body["result"]["contents"]
    assert contents[0]["uri"] == _DOCS_URI
    assert contents[0]["mimeType"] == "text/markdown"
    chunk = json.loads(contents[0]["text"])
    assert chunk["chunk_id"] == "nsx-9.0-maximums-0007"
    assert chunk["content"].startswith("NSX 9.0 supports")
    assert chunk["source_url"].endswith("/maximums")

    # The binary scope reached the corpus and the operator identity was forwarded.
    captured = fake.captured  # type: ignore[attr-defined]
    assert captured["metadata_filters"] == {"product": "nsx", "version": "9.0"}
    assert captured["operator"].tenant_id == op.tenant_id


# ---------------------------------------------------------------------------
# resources/read — not-found collapse + corpus-down internal error
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("docs_client", [frozenset({_DOCS_CAPABILITY})], indirect=True)
def test_resources_read_docs_unknown_chunk_collapses_to_not_found(
    docs_client: tuple[TestClient, Operator],
) -> None:
    """A chunk id absent from the re-search collapses to INVALID_PARAMS not-found.

    The message never distinguishes "empty scope" from "no such id" so
    the resource can't be used as a corpus-contents oracle.
    """
    client, _op = docs_client
    # The corpus returns only a non-matching chunk.
    fake = _fake_corpus(
        CorpusChunk(chunk_id="some-other-id", document_id="d", content="x"),
    )
    with patch("meho_backplane.docs_search.service.search_corpus", new=fake):
        response = post_mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "resources/read",
                "params": {"uri": _DOCS_URI},
            },
        )
    assert response.status_code == 200
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "not found" in body["error"]["message"].lower()


@pytest.mark.parametrize("docs_client", [frozenset({_DOCS_CAPABILITY})], indirect=True)
def test_resources_read_docs_corpus_unavailable_is_internal_error(
    docs_client: tuple[TestClient, Operator],
) -> None:
    """A down corpus surfaces as ``-32603`` Internal Error, not invalid params."""
    client, _op = docs_client

    async def _down(_op: Operator, _query: str, **_kwargs: Any) -> CorpusSearchResponse:
        raise CorpusUnavailable("corpus unreachable: ConnectError")

    with patch("meho_backplane.docs_search.service.search_corpus", new=_down):
        response = post_mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "resources/read",
                "params": {"uri": _DOCS_URI},
            },
        )
    assert response.status_code == 200
    assert response.json()["error"]["code"] == INTERNAL_ERROR
