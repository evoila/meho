# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the ``meho://docs/{...}`` companion resource (G4.6-T3, #1552).

Covers the collection-scoped resource acceptance criteria:

* ``meho://docs/{collection}/{product}/{version}/{chunk_id}`` is
  registered, gated by the SAME ``required_capability="meho-docs"`` as the
  ``search_docs`` tool: absent from ``resources/templates/list`` and
  403-on-read for a tenant without the base capability; present for one
  with it.
* **Per-collection entitlement (#1552):** a tenant with base ``meho-docs``
  but not ``meho-docs:<collection>`` → 403-class read even though the
  template is visible.
* ``resources/read`` recovers the cited chunk's text (the backend has no
  fetch-by-id endpoint, so the handler re-issues a scoped search and
  matches on ``chunk_id``).
* A ``(collection, product, version, chunk_id)`` that doesn't resolve
  collapses to INVALID_PARAMS "not found" without leaking collection
  contents.

The ``corpus-http`` backend wraps the JWT-forward transport; these unit
tests mock
:func:`meho_backplane.docs_search.backends.corpus_http.search_corpus` so
the read plumbing is exercised without a live corpus.
"""

from __future__ import annotations

import asyncio
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
    seed_doc_collection,
)

_DOCS_CAPABILITY = "meho-docs"
#: Per-collection entitlement key for the seeded ``vmware`` collection.
_VMWARE_CAP = "meho-docs:vmware"
#: A provisioned + vmware-entitled capability set.
_ENTITLED = frozenset({_DOCS_CAPABILITY, _VMWARE_CAP})
#: URI now carries the leading ``{collection}`` segment.
_DOCS_URI = "meho://docs/vmware/nsx/9.0/nsx-9.0-maximums-0007"
_DOCS_TEMPLATE = "meho://docs/{collection}/{product}/{version}/{chunk_id}"

#: The corpus-http backend's transport seam.
_CORPUS_SEAM = "meho_backplane.docs_search.backends.corpus_http.search_corpus"


def _seed_collection_sync(**kwargs: Any) -> None:
    """Run :func:`seed_doc_collection` to completion from a sync test."""
    asyncio.run(seed_doc_collection(**kwargs))


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
    assert _DOCS_TEMPLATE not in templates


@pytest.mark.parametrize("docs_client", [frozenset({_DOCS_CAPABILITY})], indirect=True)
def test_docs_resource_present_with_stripped_wire_fields_when_provisioned(
    docs_client: tuple[TestClient, Operator],
) -> None:
    """The template appears once provisioned; wire shape drops the gating fields.

    Visibility rides only the base ``meho-docs`` capability — the
    per-collection entitlement is enforced at read time, not list time.
    """
    client, _op = docs_client
    response = post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 1, "method": "resources/templates/list"},
    )
    assert response.status_code == 200
    templates = {t["uriTemplate"]: t for t in response.json()["result"]["resourceTemplates"]}
    template = templates.get(_DOCS_TEMPLATE)
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
    with patch(_CORPUS_SEAM, new=fake):
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


@pytest.mark.parametrize("docs_client", [_ENTITLED], indirect=True)
def test_resources_read_docs_returns_matching_chunk_text(
    docs_client: tuple[TestClient, Operator],
) -> None:
    """An entitled read recovers the chunk whose id matches the URI.

    The handler rebuilds the binary scope from the URI segments, runs the
    resolve + entitle + readiness gate, re-issues a scoped search on the
    collection's backend, and returns the exact-id match — even when the
    backend returns sibling chunks alongside it.
    """
    client, op = docs_client
    _seed_collection_sync()
    other = CorpusChunk(
        chunk_id="nsx-9.0-maximums-0008",
        document_id="nsx-9.0-config-maximums",
        content="An unrelated sibling chunk.",
    )
    fake = _fake_corpus(other, _SAMPLE_CHUNK)
    with patch(_CORPUS_SEAM, new=fake):
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

    # The optional product/version refinements reached the backend and the
    # operator identity was forwarded.
    captured = fake.captured  # type: ignore[attr-defined]
    assert captured["metadata_filters"] == {"product": "nsx", "version": "9.0"}
    assert captured["operator"].tenant_id == op.tenant_id


@pytest.mark.parametrize("docs_client", [frozenset({_DOCS_CAPABILITY})], indirect=True)
def test_resources_read_docs_403_when_not_entitled_to_collection(
    docs_client: tuple[TestClient, Operator],
) -> None:
    """A tenant with base ``meho-docs`` but not ``meho-docs:vmware`` → 403-class read.

    The template is visible (base capability), but the per-collection
    entitlement gate rejects the read before the backend re-search.
    """
    client, _op = docs_client
    _seed_collection_sync()
    fake = _fake_corpus(_SAMPLE_CHUNK)
    with patch(_CORPUS_SEAM, new=fake):
        response = post_mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "resources/read",
                "params": {"uri": _DOCS_URI},
            },
        )
    assert response.status_code == 200
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "entitled" in body["error"]["message"].lower()
    assert "query" not in fake.captured  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# resources/read — not-found collapse + backend-down internal error
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("docs_client", [_ENTITLED], indirect=True)
def test_resources_read_docs_unknown_chunk_collapses_to_not_found(
    docs_client: tuple[TestClient, Operator],
) -> None:
    """A chunk id absent from the re-search collapses to INVALID_PARAMS not-found.

    The message never distinguishes "empty scope" from "no such id" so
    the resource can't be used as a collection-contents oracle.
    """
    client, _op = docs_client
    _seed_collection_sync()
    # The backend returns only a non-matching chunk.
    fake = _fake_corpus(
        CorpusChunk(chunk_id="some-other-id", document_id="d", content="x"),
    )
    with patch(_CORPUS_SEAM, new=fake):
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


@pytest.mark.parametrize("docs_client", [_ENTITLED], indirect=True)
def test_resources_read_docs_backend_unavailable_is_internal_error(
    docs_client: tuple[TestClient, Operator],
) -> None:
    """A down backend surfaces as ``-32603`` Internal Error, not invalid params."""
    client, _op = docs_client
    _seed_collection_sync()

    async def _down(_op: Operator, _query: str, **_kwargs: Any) -> CorpusSearchResponse:
        raise CorpusUnavailable("corpus unreachable: ConnectError")

    with patch(_CORPUS_SEAM, new=_down):
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


@pytest.mark.parametrize("docs_client", [_ENTITLED], indirect=True)
def test_resources_read_docs_disabled_collection_is_invalid_params(
    docs_client: tuple[TestClient, Operator],
) -> None:
    """A ``disabled`` collection → -32602 terminal (the shared gate's #1567 split).

    The resource reuses ``resolve_entitled_ready_collection``, so a disabled
    collection is the same terminal ``-32602`` (``collection_disabled``) the
    ``search_docs`` tool surfaces — distinct from the retryable ``-32603`` a
    ``provisioning`` / ``rebuilding`` collection bubbles to.
    """
    client, _op = docs_client
    _seed_collection_sync(status="disabled")

    fake = _fake_corpus(
        CorpusChunk(chunk_id="nsx-9.0-maximums-0007", document_id="d", content="x"),
    )
    with patch(_CORPUS_SEAM, new=fake):
        response = post_mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "resources/read",
                "params": {"uri": _DOCS_URI},
            },
        )
    assert response.status_code == 200
    error = response.json()["error"]
    assert error["code"] == INVALID_PARAMS
    assert error["data"]["reason"] == "collection_disabled"
