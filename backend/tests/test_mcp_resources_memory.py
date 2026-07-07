# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the ``meho://memory/{scope}/{slug}`` MCP resource (G5.1-T3, #423).

Covers every acceptance criterion in the task body that targets the
resource surface:

* The template is registered via G0.5's ``register_mcp_resource`` and
  surfaces in ``resources/templates/list`` with the
  :class:`ResourceTemplateDefinition` wire shape (``mimeType`` =
  ``text/markdown``; MEHO-internal ``required_role`` stripped).
* ``resources/read meho://memory/user/<existing-slug>`` returns the
  full :class:`MemoryEntry` payload as a JSON-encoded ``text/markdown``
  content block.
* Unknown slug → INVALID_PARAMS (-32602).
* Unknown scope → INVALID_PARAMS.
* Malformed slug (one that fails ``SLUG_PATTERN``) → INVALID_PARAMS.
* Tenant boundary: another tenant's same-slug entry collapses to
  "not found" without revealing the foreign tenant's existence.
* Cross-operator user-scoped read collapses to "not found" without
  revealing the foreign operator's row.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.mcp.schemas import INVALID_PARAMS
from meho_backplane.memory.schemas import MemoryScope
from meho_backplane.memory.service import MemoryService
from meho_backplane.untrusted_text import BLOCK_END, BLOCK_START, GUARD_PREFIX
from tests.mcp_test_fixtures import (
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)


@pytest.fixture
def stub_embedding() -> Iterator[AsyncMock]:
    """Patch the embedding service so SQLite-backed tests run without fastembed."""
    fake = AsyncMock()
    fake.encode_one.return_value = [0.1] * 384
    fake.encode.return_value = [[0.1] * 384]
    fake.dimension = 384
    with patch(
        "meho_backplane.retrieval.indexer.get_embedding_service",
        return_value=fake,
    ):
        yield fake.encode_one


# ---------------------------------------------------------------------------
# resources/templates/list shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_resources_templates_list_exposes_memory_entry(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC: the memory resource template surfaces in ``resources/templates/list``."""
    client, _op = client_with_operator
    response = post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 1, "method": "resources/templates/list"},
    )
    assert response.status_code == 200
    body = response.json()
    templates = body["result"]["resourceTemplates"]
    memory_entries = [t for t in templates if t["uriTemplate"] == "meho://memory/{scope}/{slug}"]
    assert len(memory_entries) == 1
    template = memory_entries[0]
    assert template["mimeType"] == "text/markdown"
    # MEHO-internal RBAC field stripped from the wire shape.
    assert "required_role" not in template
    # #154: the description advertises the served body as untrusted
    # agent-authored content, not a directive channel.
    assert "untrusted" in template["description"]
    assert "not a system directive" in template["description"]
    assert "UNTRUSTED_AGENT_TEXT" in template["description"]


# ---------------------------------------------------------------------------
# resources/read — happy path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
@pytest.mark.asyncio
async def test_resources_read_returns_full_memory_entry_body(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    stub_embedding: AsyncMock,
) -> None:
    """AC: read an existing user-scoped slug → full body + metadata under text/markdown."""
    client, op = client_with_operator
    service = MemoryService()
    await service.remember(
        op,
        scope=MemoryScope.USER,
        body="# Wine\n\nFull Markdown body of the operator's preference.",
        slug="wine-preference",
    )

    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "resources/read",
            "params": {"uri": "meho://memory/user/wine-preference"},
        },
    )
    assert response.status_code == 200
    body = response.json()
    contents = body["result"]["contents"]
    assert len(contents) == 1
    block = contents[0]
    assert block["uri"] == "meho://memory/user/wine-preference"
    assert block["mimeType"] == "text/markdown"

    payload = json.loads(block["text"])
    assert payload["scope"] == "user"
    assert payload["slug"] == "wine-preference"
    # The agent-authored body is served inside the untrusted-content
    # envelope (stored-prompt-injection guard, #154): delimiters
    # bracket the intact original Markdown.
    assert payload["body"].startswith(BLOCK_START)
    assert payload["body"].endswith(BLOCK_END)
    assert GUARD_PREFIX in payload["body"]
    assert "# Wine\n\nFull Markdown body of the operator's preference." in payload["body"]
    # Substrate-side timestamps round-trip.
    assert "created_at" in payload
    assert "updated_at" in payload
    # The handler returns the full MemoryEntry shape, including
    # service-managed metadata fields.
    assert payload["user_sub"] == op.sub


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
@pytest.mark.asyncio
async def test_resources_read_returns_tenant_scope_entry(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    stub_embedding: AsyncMock,
) -> None:
    """A tenant-scope read works for any operator in the tenant.

    Tenant-shared memory has no ``target_name`` dimension and no
    per-operator ``user_sub`` gating, so the
    ``meho://memory/tenant/{slug}`` URI template fully addresses the
    entry. Seeds the row via the service under a tenant_admin operator
    (write side requires that role) and reads back as the fixture
    operator (read side is open to every operator in the tenant).
    """
    client, op = client_with_operator
    # Service-side write needs tenant_admin role; seed via a stand-in
    # admin operator pinned to the same tenant.
    admin = Operator(
        sub=op.sub,  # same operator, elevated role for the seed write
        name=op.name,
        email=op.email,
        raw_jwt=op.raw_jwt,
        tenant_id=op.tenant_id,
        tenant_role=TenantRole.TENANT_ADMIN,
    )
    service = MemoryService()
    await service.remember(
        admin,
        scope=MemoryScope.TENANT,
        body="Tenant convention: use Brunello for all demos.",
        slug="wine-default",
    )

    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "resources/read",
            "params": {"uri": "meho://memory/tenant/wine-default"},
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert "error" not in body
    payload = json.loads(body["result"]["contents"][0]["text"])
    assert payload["scope"] == "tenant"
    assert payload["slug"] == "wine-default"
    # Body arrives inside the untrusted-content envelope (#154).
    assert payload["body"] == (
        f"{BLOCK_START}\n{GUARD_PREFIX}\n\n"
        "Tenant convention: use Brunello for all demos."
        f"\n{BLOCK_END}"
    )


# ---------------------------------------------------------------------------
# resources/read — rejection arms
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_resources_read_unknown_slug_returns_invalid_params(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC: an unknown but well-formed slug → INVALID_PARAMS."""
    client, _op = client_with_operator
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "resources/read",
            "params": {"uri": "meho://memory/user/does-not-exist"},
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "not found" in body["error"]["message"].lower()


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_resources_read_unknown_scope_returns_invalid_params(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """An unrecognised ``{scope}`` value → INVALID_PARAMS.

    Rejection happens before any DB query so a probe with an arbitrary
    scope string can't be used to learn the enum shape via timing.
    """
    client, _op = client_with_operator
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "resources/read",
            "params": {"uri": "meho://memory/imaginary/some-slug"},
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "invalid scope" in body["error"]["message"].lower()


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_resources_read_malformed_slug_returns_invalid_params(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """A slug containing characters outside the safe set → INVALID_PARAMS.

    The handler's ``validate_slug`` call surfaces :class:`ValueError`,
    which maps to INVALID_PARAMS. The rejection happens before any DB
    I/O, so a probe attempt with a bad-shape URI can't be used to
    learn whether arbitrary slugs exist.
    """
    client, _op = client_with_operator
    # ``has:colon`` violates SLUG_PATTERN (colon is not in the safe set).
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "resources/read",
            "params": {"uri": "meho://memory/user/has:colon"},
        },
    )
    # The URI template's `[^/]+` capture would actually parse
    # `user` / `has:colon` into the bound vars; the colon is the
    # offending character that fails SLUG_PATTERN inside the handler.
    assert response.status_code == 200
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS


# ---------------------------------------------------------------------------
# Tenant boundary + cross-operator info-leak avoidance
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
@pytest.mark.asyncio
async def test_resources_read_does_not_reveal_foreign_tenant_entries(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    stub_embedding: AsyncMock,
) -> None:
    """AC: a slug owned by another tenant collapses to "not found".

    The error message must not distinguish "this slug doesn't exist"
    from "this slug exists but belongs to another tenant" — otherwise
    the resource handler is a tenant-existence oracle for any operator
    that can guess slugs.
    """
    client, op = client_with_operator
    foreign_tenant = uuid.uuid4()
    foreign_op = Operator(
        sub="foreign-op",
        name=None,
        email=None,
        raw_jwt="not-a-real-jwt",
        tenant_id=foreign_tenant,
        tenant_role=TenantRole.TENANT_ADMIN,
    )
    service = MemoryService()
    await service.remember(
        foreign_op,
        scope=MemoryScope.TENANT,
        body="Foreign tenant's convention.",
        slug="foreign-tenant-default",
    )

    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "resources/read",
            "params": {"uri": "meho://memory/tenant/foreign-tenant-default"},
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    # Error message says "not found" — same shape as a truly-missing
    # slug, deliberately not "forbidden" or "cross-tenant".
    assert "not found" in body["error"]["message"].lower()
    # The fixture operator's own tenant_id never appears in the
    # foreign tenant's row — defensive sanity that the test's
    # foreign-tenant write actually used a different id.
    assert op.tenant_id != foreign_tenant


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
@pytest.mark.asyncio
async def test_resources_read_does_not_reveal_other_operator_user_scope(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    stub_embedding: AsyncMock,
) -> None:
    """AC: operator A cannot read operator B's user-scoped slug in the same tenant.

    The natural-key encoding (``user:<sub>:<slug>``) embeds the writer's
    ``sub`` so the lookup under the requester's ``sub`` never matches
    another operator's row. The handler renders the miss as "not found"
    — the cross-operator boundary is invisible from the wire shape.
    """
    client, op = client_with_operator
    # Seed a user-scoped row under a different operator in the same tenant.
    other = Operator(
        sub="other-operator",
        name=None,
        email=None,
        raw_jwt="not-a-real-jwt",
        tenant_id=op.tenant_id,
        tenant_role=TenantRole.OPERATOR,
    )
    service = MemoryService()
    await service.remember(
        other,
        scope=MemoryScope.USER,
        body="Other operator's personal note.",
        slug="other-personal-note",
    )

    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "resources/read",
            "params": {"uri": "meho://memory/user/other-personal-note"},
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "not found" in body["error"]["message"].lower()


# ---------------------------------------------------------------------------
# RBAC visibility — read_only operator
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.READ_ONLY],
    indirect=True,
)
def test_memory_resource_hidden_from_read_only_operator(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """``required_role=OPERATOR`` hides the template from read-only operators."""
    client, _op = client_with_operator
    response = post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 1, "method": "resources/templates/list"},
    )
    templates = response.json()["result"]["resourceTemplates"]
    uri_templates = {t["uriTemplate"] for t in templates}
    assert "meho://memory/{scope}/{slug}" not in uri_templates
