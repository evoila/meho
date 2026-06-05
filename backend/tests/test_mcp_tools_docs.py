# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the capability-gated ``search_docs`` MCP tool (G4.5-T4, #1523).

Covers every acceptance criterion in the task body:

* ``search_docs`` is registered with ``required_capability="meho-docs"``;
  it is **absent** from ``tools/list`` for a tenant without the
  capability and **present** for one with it (T1's gate, #1519).
* ``tools/call search_docs`` from an unprovisioned tenant → 403-class
  error (the handler never runs); from a provisioned tenant with
  product+version → cited chunks; missing/blank product or version →
  the T3 422 surfaced as an MCP ``-32602`` error.
* ``inputSchema`` is strict (``additionalProperties: false``, required
  ``[query, product, version]``); the MEHO-internal RBAC + capability
  fields are stripped from the wire shape.
* The handler calls the shared docs-search service with the operator's
  forwarded identity and the validated binary scope.
* The description names the sibling tools (``search_knowledge`` /
  ``search_memory``) and points to the companion resource.

The shared docs-search service (T3, #1521) federates to an **external**
corpus; these unit tests mock
:func:`meho_backplane.docs_search.service.search_corpus` so the wire
shape + scope plumbing are exercised without a live corpus. PG-real /
live-corpus coverage is out of scope for the unit suite.
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
from meho_backplane.settings import get_settings
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

_DOCS_CAPABILITY = "meho-docs"


def _operator(
    *,
    role: TenantRole = TenantRole.OPERATOR,
    capabilities: frozenset[str] = frozenset(),
) -> Operator:
    """Build a fixture operator with the requested role + capability set."""
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
    """``TestClient`` whose operator's capability set is parametrised.

    The production ``search_docs`` tool is registered by the lifespan's
    eager import (it lives in ``mcp/tools/docs.py``); this fixture only
    pins the operator. Provision the ``meho-docs`` capability with
    ``@pytest.mark.parametrize("docs_client", [frozenset({"meho-docs"})], indirect=True)``;
    default is the empty set (unprovisioned).
    """
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


def _fake_corpus(
    *chunks: CorpusChunk,
) -> Any:
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
# tools/list shape + capability gate (AC1, AC3)
# ---------------------------------------------------------------------------


def test_search_docs_absent_from_tools_list_for_unprovisioned_tenant(
    docs_client: tuple[TestClient, Operator],
) -> None:
    """AC1: an operator without ``meho-docs`` never sees the tool (true absence)."""
    client, _op = docs_client
    response = post_mcp(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response.status_code == 200
    names = {t["name"] for t in response.json()["result"]["tools"]}
    assert "search_docs" not in names


@pytest.mark.parametrize("docs_client", [frozenset({_DOCS_CAPABILITY})], indirect=True)
def test_search_docs_present_with_strict_schema_for_provisioned_tenant(
    docs_client: tuple[TestClient, Operator],
) -> None:
    """AC1 + AC3: the tool appears once provisioned, with a strict 2020-12 schema.

    Required ``[query, product, version]``, ``additionalProperties:
    false``, and the MEHO-internal RBAC + capability fields stripped by
    :meth:`ToolDefinition.to_wire`.
    """
    client, _op = docs_client
    response = post_mcp(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response.status_code == 200
    tools_by_name = {t["name"]: t for t in response.json()["result"]["tools"]}

    assert "search_docs" in tools_by_name
    tool = tools_by_name["search_docs"]
    schema = tool["inputSchema"]
    assert schema["type"] == "object"
    assert schema["required"] == ["query", "product", "version"]
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"query", "product", "version", "limit"}
    assert schema["properties"]["limit"]["default"] == 10
    assert schema["properties"]["limit"]["maximum"] == 50
    # Wire shape never leaks the server-side gating fields.
    assert "required_role" not in tool
    assert "op_class" not in tool
    assert "required_capability" not in tool


@pytest.mark.parametrize("docs_client", [frozenset({_DOCS_CAPABILITY})], indirect=True)
def test_search_docs_description_names_siblings_and_companion_resource(
    docs_client: tuple[TestClient, Operator],
) -> None:
    """AC: the description names what / when / when-NOT and the sibling tools.

    Loose substring assertions so prose can evolve without churn; the
    load-bearing routing hints (vendor reference vs. how-we-do-X vs.
    cross-session state) and the companion-resource pointer must stay.
    """
    client, _op = docs_client
    response = post_mcp(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools_by_name = {t["name"]: t for t in response.json()["result"]["tools"]}
    desc = tools_by_name["search_docs"]["description"]

    # What: vendor-document corpus search.
    assert "vendor-document corpus" in desc or "vendor document" in desc.lower()
    # When-NOT: routes to the sibling tools.
    assert "search_knowledge" in desc
    assert "search_memory" in desc
    # Mandatory binary scope is called out, not a hint.
    assert "product" in desc and "version" in desc
    # Companion resource pointer for the full text on a later turn.
    assert "meho://docs/" in desc
    assert "resources/read" in desc


def test_search_docs_hidden_from_provisioned_read_only_operator() -> None:
    """The capability does not relax the role gate: read_only never sees it."""
    op = _operator(role=TenantRole.READ_ONLY, capabilities=frozenset({_DOCS_CAPABILITY}))

    async def _fake_verify() -> Operator:
        return op

    app.dependency_overrides[verify_mcp_jwt_and_bind] = _fake_verify
    try:
        with TestClient(app) as client:
            response = post_mcp(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    finally:
        app.dependency_overrides.pop(verify_mcp_jwt_and_bind, None)
    names = {t["name"] for t in response.json()["result"]["tools"]}
    assert "search_docs" not in names


# ---------------------------------------------------------------------------
# tools/call — capability gate (AC2)
# ---------------------------------------------------------------------------


def test_tools_call_search_docs_403_when_unprovisioned(
    docs_client: tuple[TestClient, Operator],
) -> None:
    """AC2: naming the tool directly still 403s when the capability is absent.

    The capability re-check in the dispatcher fires before the handler,
    so an unprovisioned tenant that learned the name cannot reach the
    corpus. Surfaces as INVALID_PARAMS with a ``forbidden`` / ``capability``
    message (the JSON-RPC projection of a 403).
    """
    client, _op = docs_client
    with patch(
        "meho_backplane.docs_search.service.search_corpus",
        new=_fake_corpus(_SAMPLE_CHUNK),
    ) as spy:
        response = post_mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "search_docs",
                    "arguments": {"query": "nsx maximums", "product": "nsx", "version": "9.0"},
                },
            },
        )
    assert response.status_code == 200
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "forbidden" in body["error"]["message"].lower()
    assert "capability" in body["error"]["message"].lower()
    # The handler never reached the corpus.
    assert "query" not in spy.captured  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# tools/call — provisioned happy path (AC2 positive)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("docs_client", [frozenset({_DOCS_CAPABILITY})], indirect=True)
def test_tools_call_search_docs_returns_cited_chunks(
    docs_client: tuple[TestClient, Operator],
) -> None:
    """AC2: a provisioned operator with product+version gets cited chunks.

    Pins the wire round-trip: the handler projects the corpus chunk into
    MEHO's ``DocsChunk`` surface, the dispatcher JSON-encodes it into
    ``content[0].text``, and the binary scope reaches the corpus as
    ``metadata_filters``.
    """
    client, op = docs_client
    fake = _fake_corpus(_SAMPLE_CHUNK)
    with patch("meho_backplane.docs_search.service.search_corpus", new=fake):
        response = post_mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "search_docs",
                    "arguments": {
                        "query": "config maximums",
                        "product": "nsx",
                        "version": "9.0",
                        "limit": 5,
                    },
                },
            },
        )
    assert response.status_code == 200
    body = response.json()
    assert body["result"]["isError"] is False
    payload = json.loads(body["result"]["content"][0]["text"])
    assert "chunks" in payload
    assert len(payload["chunks"]) == 1
    chunk = payload["chunks"][0]
    assert chunk["chunk_id"] == "nsx-9.0-maximums-0007"
    assert chunk["content"].startswith("NSX 9.0 supports")
    assert chunk["source_url"].endswith("/maximums")

    # The binary product+version scope reached the corpus as filters, and
    # the operator's identity was forwarded (never a caller-supplied one).
    captured = fake.captured  # type: ignore[attr-defined]
    assert captured["query"] == "config maximums"
    assert captured["limit"] == 5
    assert captured["metadata_filters"] == {"product": "nsx", "version": "9.0"}
    assert captured["operator"].tenant_id == op.tenant_id


# ---------------------------------------------------------------------------
# tools/call — REQUIRE_FILTERS surfaces as an MCP error (AC2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("docs_client", [frozenset({_DOCS_CAPABILITY})], indirect=True)
def test_tools_call_search_docs_missing_version_rejected_by_schema(
    docs_client: tuple[TestClient, Operator],
) -> None:
    """A missing required ``version`` fails inputSchema validation → -32602.

    The strict schema catches the missing mandatory scope before the
    handler runs — the first line of the REQUIRE_FILTERS defence.
    """
    client, _op = docs_client
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "search_docs",
                "arguments": {"query": "x", "product": "nsx"},
            },
        },
    )
    assert response.status_code == 200
    assert response.json()["error"]["code"] == INVALID_PARAMS


@pytest.mark.parametrize("docs_client", [frozenset({_DOCS_CAPABILITY})], indirect=True)
def test_tools_call_search_docs_blank_version_surfaces_service_422_as_mcp_error(
    docs_client: tuple[TestClient, Operator],
) -> None:
    """A blank-after-strip ``version`` passes the schema but the service rejects it.

    ``minLength: 1`` admits a single space; the shared
    ``build_docs_scope`` treats blank-after-strip as absent and raises
    ``MissingDocsFilterError`` (the route's 422), which the handler maps
    to ``-32602`` — the MCP analogue. The corpus is never called.
    """
    client, _op = docs_client
    fake = _fake_corpus(_SAMPLE_CHUNK)
    with patch("meho_backplane.docs_search.service.search_corpus", new=fake):
        response = post_mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {
                    "name": "search_docs",
                    "arguments": {"query": "x", "product": "nsx", "version": " "},
                },
            },
        )
    assert response.status_code == 200
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "version" in body["error"]["message"].lower()
    # The REQUIRE_FILTERS rejection short-circuits before the corpus call.
    assert "query" not in fake.captured  # type: ignore[attr-defined]


@pytest.mark.parametrize("docs_client", [frozenset({_DOCS_CAPABILITY})], indirect=True)
def test_tools_call_search_docs_rejects_extra_arguments(
    docs_client: tuple[TestClient, Operator],
) -> None:
    """``additionalProperties: false`` rejects unknown top-level keys."""
    client, _op = docs_client
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {
                "name": "search_docs",
                "arguments": {
                    "query": "x",
                    "product": "nsx",
                    "version": "9.0",
                    "tenant_id": "smuggled",
                },
            },
        },
    )
    assert response.status_code == 200
    assert response.json()["error"]["code"] == INVALID_PARAMS


# ---------------------------------------------------------------------------
# tools/call — corpus unavailable surfaces as INTERNAL_ERROR (not -32602)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("docs_client", [frozenset({_DOCS_CAPABILITY})], indirect=True)
def test_tools_call_search_docs_corpus_unavailable_is_internal_error(
    docs_client: tuple[TestClient, Operator],
) -> None:
    """A down corpus is a server fault → ``-32603``, not invalid params.

    The well-formed request is not the client's fault; the typed
    ``CorpusUnavailable`` is not caught in the handler and bubbles to the
    dispatcher's generic catch (the MCP analogue of the route's 503).
    """
    client, _op = docs_client

    async def _down(_op: Operator, _query: str, **_kwargs: Any) -> CorpusSearchResponse:
        raise CorpusUnavailable("corpus_url is not configured")

    with patch("meho_backplane.docs_search.service.search_corpus", new=_down):
        response = post_mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {
                    "name": "search_docs",
                    "arguments": {"query": "x", "product": "nsx", "version": "9.0"},
                },
            },
        )
    assert response.status_code == 200
    assert response.json()["error"]["code"] == INTERNAL_ERROR


# ---------------------------------------------------------------------------
# Settings hygiene — the gate-off path keeps the corpus reachable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("docs_client", [frozenset({_DOCS_CAPABILITY})], indirect=True)
def test_tools_call_search_docs_gate_off_allows_partial_scope(
    docs_client: tuple[TestClient, Operator],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``corpus_require_filters`` off, a blank version degrades to optional.

    The schema's ``minLength: 1`` still requires the key be present and
    non-empty per the wire contract, but a blank-after-strip value no
    longer trips REQUIRE_FILTERS — it simply widens the corpus query
    (only ``product`` reaches ``metadata_filters``).
    """
    monkeypatch.setenv("CORPUS_REQUIRE_FILTERS", "false")
    get_settings.cache_clear()
    client, _op = docs_client
    fake = _fake_corpus()
    try:
        with patch("meho_backplane.docs_search.service.search_corpus", new=fake):
            response = post_mcp(
                client,
                {
                    "jsonrpc": "2.0",
                    "id": 8,
                    "method": "tools/call",
                    "params": {
                        "name": "search_docs",
                        "arguments": {"query": "x", "product": "nsx", "version": " "},
                    },
                },
            )
    finally:
        get_settings.cache_clear()
    assert response.status_code == 200
    assert response.json()["result"]["isError"] is False
    # Only the non-blank product survived into the corpus filter.
    assert fake.captured["metadata_filters"] == {"product": "nsx"}  # type: ignore[attr-defined]
