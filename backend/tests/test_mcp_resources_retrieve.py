# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the ``meho://retrieve/{query}`` resource (G0.5-T9, #348).

Covers the acceptance criteria:

* ``meho://retrieve/{query}`` is registered via ``register_mcp_resource``
  and appears in ``resources/templates/list`` for an operator-role JWT.
* The resource handler returns the same ``RetrievalHit`` shape the HTTP
  route (``POST /api/v1/retrieve``) does — ``{"hits": [...]}`` where each
  hit is a ``RetrievalHit.model_dump(mode="json")``.
* **RBAC:** ``read_only`` → 403-class read (the handler / retrieve never
  run); ``operator`` / ``tenant_admin`` → hits.
* **Tenant scoping:** the handler threads the JWT's ``tenant_id`` into
  ``retrieve`` (never a URI value); a fake substrate keyed on tenant_id
  shows a tenant-A query returns no tenant-B document.
* **Audit privacy:** the persisted ``audit_log`` row carries
  ``query_hash`` (+ ``hit_count``) but **no raw query string** anywhere —
  the query-bearing URI is redacted in both ``path`` and ``payload.uri``.

The PG-only ``@@`` / ``<=>`` ranking is exercised in the integration
suite; here ``retrieve`` is mocked at the resource's import site so the
MCP wire-shape + audit plumbing is exercised without spinning up
Postgres + fastembed.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.main import app
from meho_backplane.mcp.auth import verify_mcp_jwt_and_bind
from meho_backplane.mcp.resources.retrieve import _compute_query_hash
from meho_backplane.mcp.schemas import INVALID_PARAMS
from meho_backplane.retrieval.retriever import RetrievalHit
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

_RETRIEVE_TEMPLATE = "meho://retrieve/{query}"
#: The seam the resource patches: ``retrieve`` is imported into the
#: resource module's namespace, so patching it there stubs the call the
#: handler makes (same pattern the HTTP-route tests use).
_RETRIEVE_SEAM = "meho_backplane.mcp.resources.retrieve.retrieve"

#: A query carrying a space + punctuation so the percent-encoding round
#: trip and the "raw query must not appear in the audit row" negative
#: assertion are both meaningful.
_RAW_QUERY = "kubernetes ingress timeout"
_RETRIEVE_URI = f"meho://retrieve/{quote(_RAW_QUERY, safe='')}"


def _operator(
    *,
    role: TenantRole = TenantRole.OPERATOR,
    tenant_id: uuid.UUID = OPERATOR_TENANT_ID,
) -> Operator:
    return Operator(
        sub="op-test",
        name="Test",
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=tenant_id,
        tenant_role=role,
    )


@pytest.fixture
def retrieve_client(
    request: pytest.FixtureRequest,
) -> Iterator[tuple[TestClient, Operator]]:
    """``TestClient`` with a role-parametrised operator (default: operator)."""
    role: TenantRole = getattr(request, "param", TenantRole.OPERATOR)
    op = _operator(role=role)

    async def _fake_verify() -> Operator:
        return op

    app.dependency_overrides[verify_mcp_jwt_and_bind] = _fake_verify
    try:
        with TestClient(app) as client:
            yield client, op
    finally:
        app.dependency_overrides.pop(verify_mcp_jwt_and_bind, None)


def _make_hit(*, tenant_id: uuid.UUID, body: str = "doc body") -> RetrievalHit:
    """Build a :class:`RetrievalHit` stub scoped to *tenant_id*."""
    ts = datetime(2026, 5, 21, 10, 16, 12, tzinfo=UTC)
    return RetrievalHit(
        document_id=uuid.uuid4(),
        tenant_id=tenant_id,
        source="kb",
        source_id="kb-entry:ingress-timeouts",
        kind="kb-entry",
        body=body,
        doc_metadata={"product": "k8s"},
        created_at=ts,
        updated_at=ts,
        fused_score=0.7,
        bm25_score=0.4,
        cosine_score=0.85,
        bm25_rank=1,
        cosine_rank=1,
    )


# ---------------------------------------------------------------------------
# Registration + resources/templates/list
# ---------------------------------------------------------------------------


def test_retrieve_resource_in_templates_list(
    retrieve_client: tuple[TestClient, Operator],
) -> None:
    """AC: the template is registered and visible to an operator-role JWT."""
    client, _op = retrieve_client
    response = post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 1, "method": "resources/templates/list"},
    )
    assert response.status_code == 200
    templates = {t["uriTemplate"]: t for t in response.json()["result"]["resourceTemplates"]}
    template = templates.get(_RETRIEVE_TEMPLATE)
    assert template is not None
    assert template["mimeType"] == "application/json"
    # MEHO-internal RBAC fields are dropped from the wire shape.
    assert "required_role" not in template
    assert "audit_redact_uri" not in template


@pytest.mark.parametrize("retrieve_client", [TenantRole.READ_ONLY], indirect=True)
def test_retrieve_resource_absent_for_read_only_in_templates_list(
    retrieve_client: tuple[TestClient, Operator],
) -> None:
    """A ``read_only`` operator never sees the operator-gated template."""
    client, _op = retrieve_client
    response = post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 1, "method": "resources/templates/list"},
    )
    assert response.status_code == 200
    templates = {t["uriTemplate"] for t in response.json()["result"]["resourceTemplates"]}
    assert _RETRIEVE_TEMPLATE not in templates


# ---------------------------------------------------------------------------
# resources/read — happy path + hit shape + tenant binding
# ---------------------------------------------------------------------------


def test_resources_read_returns_retrieval_hit_shape(
    retrieve_client: tuple[TestClient, Operator],
) -> None:
    """AC: the read returns the same ``RetrievalHit`` shape the HTTP route does.

    Also asserts the handler decoded the percent-encoded query and passed
    the operator's JWT ``tenant_id`` (not a URI value) into ``retrieve``.
    """
    client, op = retrieve_client
    fake = AsyncMock(
        return_value=[
            _make_hit(tenant_id=op.tenant_id, body="body-A"),
            _make_hit(tenant_id=op.tenant_id, body="body-B"),
        ],
    )
    with patch(_RETRIEVE_SEAM, new=fake):
        response = post_mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/read",
                "params": {"uri": _RETRIEVE_URI},
            },
        )
    assert response.status_code == 200
    body = response.json()
    contents = body["result"]["contents"]
    assert contents[0]["uri"] == _RETRIEVE_URI
    assert contents[0]["mimeType"] == "application/json"
    payload = json.loads(contents[0]["text"])
    assert list(payload.keys()) == ["hits"]
    assert len(payload["hits"]) == 2
    hit = payload["hits"][0]
    # The hit carries the full RetrievalHit projection (same keys the
    # HTTP route's RetrieveResponse.hits entries carry).
    assert set(hit) == {
        "document_id",
        "tenant_id",
        "source",
        "source_id",
        "kind",
        "body",
        "doc_metadata",
        "created_at",
        "updated_at",
        "fused_score",
        "bm25_score",
        "cosine_score",
        "bm25_rank",
        "cosine_rank",
    }
    assert hit["body"] == "body-A"
    assert hit["fused_score"] == pytest.approx(0.7)

    # Tenant from JWT, query decoded from the URI.
    fake.assert_awaited_once()
    call_kwargs = fake.await_args.kwargs
    assert call_kwargs["tenant_id"] == op.tenant_id
    assert call_kwargs["query"] == _RAW_QUERY
    # The resource threads the operator's `sub` as `principal_sub` so the
    # substrate enforces per-principal memory isolation (#1797). This
    # resource retrieves across every source with no metadata_filters, so
    # without it a user-scoped memory row written by another principal in
    # the same tenant would leak here.
    assert call_kwargs["principal_sub"] == op.sub


def test_resources_read_empty_query_is_invalid_params(
    retrieve_client: tuple[TestClient, Operator],
) -> None:
    """A ``{query}`` decoding to whitespace rejects before retrieval runs."""
    client, _op = retrieve_client
    fake = AsyncMock(return_value=[])
    with patch(_RETRIEVE_SEAM, new=fake):
        response = post_mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "resources/read",
                "params": {"uri": f"meho://retrieve/{quote('   ', safe='')}"},
            },
        )
    assert response.status_code == 200
    error = response.json()["error"]
    assert error["code"] == INVALID_PARAMS
    assert "empty query" in error["message"].lower()
    fake.assert_not_awaited()


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("retrieve_client", [TenantRole.READ_ONLY], indirect=True)
def test_resources_read_read_only_is_forbidden(
    retrieve_client: tuple[TestClient, Operator],
) -> None:
    """AC: a ``read_only`` operator is rejected; the handler must not run."""
    client, _op = retrieve_client
    fake = AsyncMock(return_value=[_make_hit(tenant_id=OPERATOR_TENANT_ID)])
    with patch(_RETRIEVE_SEAM, new=fake):
        response = post_mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "resources/read",
                "params": {"uri": _RETRIEVE_URI},
            },
        )
    assert response.status_code == 200
    error = response.json()["error"]
    assert error["code"] == INVALID_PARAMS
    assert "forbidden" in error["message"].lower()
    # The retrieve substrate must never be reached for a denied read.
    fake.assert_not_awaited()


@pytest.mark.parametrize(
    "retrieve_client",
    [TenantRole.OPERATOR, TenantRole.TENANT_ADMIN],
    indirect=True,
)
def test_resources_read_operator_and_tenant_admin_get_hits(
    retrieve_client: tuple[TestClient, Operator],
) -> None:
    """AC: both ``operator`` and ``tenant_admin`` roles get hits back."""
    client, op = retrieve_client
    fake = AsyncMock(return_value=[_make_hit(tenant_id=op.tenant_id, body="ok")])
    with patch(_RETRIEVE_SEAM, new=fake):
        response = post_mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "resources/read",
                "params": {"uri": _RETRIEVE_URI},
            },
        )
    assert response.status_code == 200
    payload = json.loads(response.json()["result"]["contents"][0]["text"])
    assert len(payload["hits"]) == 1
    assert payload["hits"][0]["body"] == "ok"


# ---------------------------------------------------------------------------
# Tenant scoping (JWT, not URI)
# ---------------------------------------------------------------------------


def test_resources_read_tenant_scoped_to_jwt_excludes_other_tenant(
    retrieve_client: tuple[TestClient, Operator],
) -> None:
    """AC: a query under tenant A returns no tenant-B documents.

    The fake substrate models the real helper's tenant filter — it only
    returns documents whose tenant matches the ``tenant_id`` it was
    called with. Since the handler sources ``tenant_id`` purely from the
    JWT, a tenant-B document seeded into the fake is structurally
    invisible to tenant A's read.
    """
    client, op = retrieve_client
    other_tenant = uuid.uuid4()
    corpus = [
        _make_hit(tenant_id=op.tenant_id, body="tenant-A doc"),
        _make_hit(tenant_id=other_tenant, body="tenant-B doc"),
    ]

    async def _tenant_filtered(**kwargs: Any) -> list[RetrievalHit]:
        return [h for h in corpus if h.tenant_id == kwargs["tenant_id"]]

    with patch(_RETRIEVE_SEAM, side_effect=_tenant_filtered):
        response = post_mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "resources/read",
                "params": {"uri": _RETRIEVE_URI},
            },
        )
    assert response.status_code == 200
    payload = json.loads(response.json()["result"]["contents"][0]["text"])
    bodies = [h["body"] for h in payload["hits"]]
    assert bodies == ["tenant-A doc"]
    assert "tenant-B doc" not in bodies


# ---------------------------------------------------------------------------
# Audit privacy contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_row_carries_query_hash_not_raw_query(
    retrieve_client: tuple[TestClient, Operator],
) -> None:
    """AC: the persisted audit row carries ``query_hash`` + ``hit_count``, no raw query.

    The query is a URI path segment, so without redaction it would leak
    into ``audit_log.path`` / ``payload.uri``. The resource opts into
    ``audit_redact_uri`` so the row records a query-stripped sentinel and
    the correlatable ``query_hash`` the handler binds — never the query.
    """
    client, op = retrieve_client
    fake = AsyncMock(
        return_value=[
            _make_hit(tenant_id=op.tenant_id, body="a"),
            _make_hit(tenant_id=op.tenant_id, body="b"),
        ],
    )
    with patch(_RETRIEVE_SEAM, new=fake):
        response = post_mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "resources/read",
                "params": {"uri": _RETRIEVE_URI},
            },
        )
    assert response.status_code == 200

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows = (
            (await session.execute(select(AuditLog).where(AuditLog.method == "MCP")))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    row = rows[0]

    # Positive: privacy-preserving identity present, mirroring the HTTP route.
    assert row.payload["query_hash"] == _compute_query_hash(_RAW_QUERY)
    assert len(row.payload["query_hash"]) == 64
    assert row.payload["hit_count"] == 2

    # The URI was redacted in both the path and the payload.
    assert row.payload["uri"] == "meho://retrieve/<redacted>"
    assert row.path == "/mcp/resources/read/meho://retrieve/<redacted>"

    # Negative: the raw query (and its percent-encoded form) must not
    # appear anywhere in the serialised row.
    serialised = json.dumps(row.payload) + row.path
    assert _RAW_QUERY not in serialised
    assert quote(_RAW_QUERY, safe="") not in serialised
    for token in _RAW_QUERY.split():
        assert token not in serialised
