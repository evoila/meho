# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the bounded own-tenant convention index resource (#3755).

``meho://tenant-conventions/index`` lists the caller's own conventions
without bodies, with paging through an opaque ``next_uri``. The tests
drive the real ``/mcp`` JSON-RPC path with the shared MCP fixtures.
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.conventions.index import (
    INDEX_PAGE_SIZE,
    IndexCursor,
    encode_index_cursor,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import TenantConvention
from meho_backplane.mcp.resources.tenant_conventions import INDEX_URI
from meho_backplane.mcp.schemas import INVALID_PARAMS
from tests.mcp_test_fixtures import (
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
    seeded_operator_tenant,  # noqa: F401 — pytest-discovered fixture
)

_BASE_TIME = datetime(2026, 1, 1, tzinfo=UTC)


async def _seed(
    tenant_id: uuid.UUID,
    slug: str,
    *,
    kind: str = "operational",
    priority: int = 0,
    body: str = "Short rule body.",
    minute: int = 0,
) -> None:
    ts = _BASE_TIME + timedelta(minutes=minute)
    async with get_sessionmaker()() as session:
        session.add(
            TenantConvention(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                slug=slug,
                title=f"Title {slug}",
                body=body,
                kind=kind,
                priority=priority,
                created_by_sub="test:user",
                created_at=ts,
                updated_at=ts,
            ),
        )
        await session.commit()


def _read(client: TestClient, uri: str) -> dict[str, Any]:
    response = post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 1, "method": "resources/read", "params": {"uri": uri}},
    )
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return body


def _page(client: TestClient, uri: str = INDEX_URI) -> dict[str, Any]:
    body = _read(client, uri)
    assert "error" not in body, body
    content = body["result"]["contents"][0]
    assert content["uri"] == uri
    assert content["mimeType"] == "application/json"
    page: dict[str, Any] = json.loads(content["text"])
    return page


def _error(client: TestClient, uri: str) -> dict[str, Any]:
    body = _read(client, uri)
    assert body["error"]["code"] == INVALID_PARAMS
    error: dict[str, Any] = body["error"]
    return error


def test_index_is_listed_and_next_page_template_is_advertised(
    client_with_operator: tuple[TestClient, Operator],
) -> None:
    client, _op = client_with_operator

    listed = post_mcp(client, {"jsonrpc": "2.0", "id": 1, "method": "resources/list"})
    uris = [r["uri"] for r in listed.json()["result"]["resources"]]
    assert uris.count(INDEX_URI) == 1

    templates = post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 2, "method": "resources/templates/list"},
    ).json()["result"]["resourceTemplates"]
    assert any(t["uriTemplate"] == f"{INDEX_URI}/{{cursor}}" for t in templates)


@pytest.mark.asyncio
async def test_empty_tenant_returns_empty_index(
    client_with_operator: tuple[TestClient, Operator],
    seeded_operator_tenant: None,
) -> None:
    client, _op = client_with_operator

    page = _page(client)

    assert page["entries"] == []
    assert page["total"] == 0
    assert page["next_uri"] is None
    assert page["preamble_budget"]["over_budget"] is False
    assert page["preamble_budget"]["estimated_tokens"] == 0
    assert "does not record whether a client read" in page["note"]


@pytest.mark.asyncio
async def test_index_reports_truthful_availability_without_bodies(
    client_with_operator: tuple[TestClient, Operator],
    seeded_operator_tenant: None,
) -> None:
    """Included, dropped, oversize, and non-operational rows; no body text."""
    client, op = client_with_operator
    # ~1,300 chars each: each fits alone, but not both together.
    half = "a" * 1300
    await _seed(op.tenant_id, "kept-rule", priority=10, body=half, minute=1)
    await _seed(op.tenant_id, "lost-room", priority=5, body="b" * 1300, minute=2)
    await _seed(op.tenant_id, "too-large", priority=1, body="c" * 4000, minute=3)
    await _seed(op.tenant_id, "how-to", kind="workflow", priority=0, minute=4)
    await _seed(uuid.uuid4(), "other-tenant-rule", priority=99, minute=0)

    page = _page(client)

    by_slug = {e["slug"]: e for e in page["entries"]}
    assert list(by_slug) == ["kept-rule", "lost-room", "too-large", "how-to"]
    assert by_slug["kept-rule"]["availability"] == "included"
    assert by_slug["lost-room"]["availability"] == "dropped"
    assert by_slug["too-large"]["availability"] == "dropped_oversize"
    assert by_slug["how-to"]["availability"] == "not_in_preamble"
    assert page["preamble_budget"]["over_budget"] is True
    assert page["total"] == 4
    for slug, entry in by_slug.items():
        assert entry["uri"] == f"meho://tenant/{op.tenant_id}/conventions/{slug}"
        assert set(entry) == {
            "slug",
            "title",
            "kind",
            "priority",
            "updated_at",
            "availability",
            "uri",
        }
    raw = json.dumps(page)
    assert half not in raw
    assert "Short rule body." not in raw
    assert "other-tenant-rule" not in raw

    # The canonical per-entry URI reads through the existing per-slug resource.
    detail = _page_text(client, by_slug["how-to"]["uri"])
    assert detail["slug"] == "how-to"


def _page_text(client: TestClient, uri: str) -> dict[str, Any]:
    body = _read(client, uri)
    assert "error" not in body, body
    detail: dict[str, Any] = json.loads(body["result"]["contents"][0]["text"])
    return detail


@pytest.mark.asyncio
async def test_pages_cover_every_row_once(
    client_with_operator: tuple[TestClient, Operator],
    seeded_operator_tenant: None,
) -> None:
    client, op = client_with_operator
    count = INDEX_PAGE_SIZE + 5
    for i in range(count):
        await _seed(op.tenant_id, f"rule-{i:02d}", kind="reference", minute=i)

    first = _page(client)
    assert len(first["entries"]) == INDEX_PAGE_SIZE
    assert first["total"] == count
    next_uri = first["next_uri"]
    assert next_uri.startswith(f"{INDEX_URI}/")
    # Opaque: no tenant id or offset in plain text.
    assert str(op.tenant_id) not in next_uri

    second = _page(client, next_uri)
    assert second["revision"] == first["revision"]
    assert len(second["entries"]) == 5
    assert second["next_uri"] is None

    slugs = [e["slug"] for e in first["entries"] + second["entries"]]
    assert slugs == [f"rule-{i:02d}" for i in range(count)]


@pytest.mark.asyncio
async def test_changed_snapshot_refuses_next_page(
    client_with_operator: tuple[TestClient, Operator],
    seeded_operator_tenant: None,
) -> None:
    client, op = client_with_operator
    for i in range(INDEX_PAGE_SIZE + 1):
        await _seed(op.tenant_id, f"rule-{i:02d}", kind="reference", minute=i)
    first = _page(client)

    async with get_sessionmaker()() as session:
        await session.execute(
            update(TenantConvention)
            .where(TenantConvention.slug == "rule-00")
            .values(updated_at=_BASE_TIME + timedelta(days=1)),
        )
        await session.commit()

    error = _error(client, first["next_uri"])
    assert "changed" in error["message"]
    assert INDEX_URI in error["message"]


@pytest.mark.asyncio
async def test_foreign_broken_and_out_of_range_cursors_share_one_error(
    client_with_operator: tuple[TestClient, Operator],
    seeded_operator_tenant: None,
) -> None:
    """No oracle: every bad cursor gets the same message, revealing nothing."""
    client, op = client_with_operator
    for i in range(INDEX_PAGE_SIZE + 1):
        await _seed(op.tenant_id, f"rule-{i:02d}", kind="reference", minute=i)
    revision = _page(client)["revision"]

    foreign = encode_index_cursor(IndexCursor(t=uuid.uuid4(), r=revision, o=1))
    beyond = encode_index_cursor(IndexCursor(t=op.tenant_id, r=revision, o=999))
    not_json = base64.urlsafe_b64encode(b"not json").decode("ascii")
    extra = base64.urlsafe_b64encode(
        json.dumps({"t": str(op.tenant_id), "r": revision, "o": 1, "x": 1}).encode(),
    ).decode("ascii")

    messages = {
        _error(client, f"{INDEX_URI}/{token}")["message"]
        for token in (foreign, beyond, not_json, extra, "%%%", "bad=cursor")
    }
    assert len(messages) == 1
    (message,) = messages
    assert "invalid cursor" in message
    assert str(op.tenant_id) not in message
    assert "changed" not in message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.READ_ONLY, TenantRole.TENANT_ADMIN],
    indirect=True,
)
async def test_every_role_sees_the_same_own_tenant_index(
    client_with_operator: tuple[TestClient, Operator],
    seeded_operator_tenant: None,
) -> None:
    """The lowest role can read the index; a higher role sees no extra data."""
    client, op = client_with_operator
    await _seed(op.tenant_id, "only-rule", minute=1)

    page = _page(client)

    assert [e["slug"] for e in page["entries"]] == ["only-rule"]
    assert page["entries"][0]["availability"] == "included"
