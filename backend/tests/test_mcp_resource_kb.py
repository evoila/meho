# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the ``meho://kb/{slug}`` MCP resource (G4.1-T3, #417).

Covers every acceptance criterion in the task body that targets the
resource surface:

* The template is registered via G0.5's ``register_mcp_resource`` and
  surfaces in ``resources/templates/list`` with the
  :class:`ResourceTemplateDefinition` wire shape (``mimeType`` =
  ``text/markdown``; MEHO-internal ``required_role`` stripped).
* ``resources/read meho://kb/<existing-slug>`` returns the full
  :class:`KbEntry` payload as a JSON-encoded ``text/markdown`` content
  block.
* Unknown slug → INVALID_PARAMS (-32602).
* Malformed slug (one that fails ``SLUG_PATTERN``) → INVALID_PARAMS.
* Tenant boundary: another tenant's same-slug entry collapses to
  "not found" without revealing the foreign tenant's existence.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.kb.service import KbService
from meho_backplane.mcp.schemas import INVALID_PARAMS
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
def test_resources_templates_list_exposes_kb_entry(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC #5: the kb resource template surfaces in ``resources/templates/list``."""
    client, _op = client_with_operator
    response = post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 1, "method": "resources/templates/list"},
    )
    assert response.status_code == 200
    body = response.json()
    templates = body["result"]["resourceTemplates"]
    kb_entries = [t for t in templates if t["uriTemplate"] == "meho://kb/{slug}"]
    assert len(kb_entries) == 1
    template = kb_entries[0]
    assert template["mimeType"] == "text/markdown"
    # MEHO-internal RBAC field stripped from the wire shape.
    assert "required_role" not in template


# ---------------------------------------------------------------------------
# resources/read — happy path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
@pytest.mark.asyncio
async def test_resources_read_returns_full_kb_entry_body(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    stub_embedding: AsyncMock,
) -> None:
    """AC #8: read an existing slug → full body + metadata under text/markdown."""
    client, op = client_with_operator
    service = KbService()
    await service.create_entry(
        tenant_id=op.tenant_id,
        slug="vcenter-9.0-snapshot-revert",
        body="# Snapshot revert\n\nFull Markdown body of the runbook.",
        metadata={"author": "ops", "category": "runbook"},
    )

    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "resources/read",
            "params": {"uri": "meho://kb/vcenter-9.0-snapshot-revert"},
        },
    )
    assert response.status_code == 200
    body = response.json()
    contents = body["result"]["contents"]
    assert len(contents) == 1
    block = contents[0]
    assert block["uri"] == "meho://kb/vcenter-9.0-snapshot-revert"
    assert block["mimeType"] == "text/markdown"

    payload = json.loads(block["text"])
    assert payload["slug"] == "vcenter-9.0-snapshot-revert"
    assert payload["body"].startswith("# Snapshot revert")
    assert payload["metadata"]["author"] == "ops"
    assert payload["metadata"]["category"] == "runbook"
    # Substrate-side timestamps round-trip.
    assert "created_at" in payload
    assert "updated_at" in payload


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
            "params": {"uri": "meho://kb/does-not-exist"},
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
def test_resources_read_malformed_slug_returns_invalid_params(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """A slug that doesn't match ``SLUG_PATTERN`` is rejected before the DB query.

    The handler's ``validate_slug`` call surfaces
    :class:`InvalidKbSlugError`, which maps to INVALID_PARAMS. The
    rejection happens before any DB I/O, so a probe attempt with a
    bad-shape URI can't be used to learn whether arbitrary slugs
    exist.
    """
    client, _op = client_with_operator
    # ``BadCase`` violates SLUG_PATTERN (starts uppercase).
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "resources/read",
            "params": {"uri": "meho://kb/BadCase"},
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS


# ---------------------------------------------------------------------------
# Tenant boundary
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
    """AC #9: a slug owned by another tenant collapses to "not found".

    The error message must not distinguish "this slug doesn't exist"
    from "this slug exists but belongs to another tenant" — otherwise
    the resource handler is a tenant-existence oracle for any operator
    that can guess slugs.
    """
    client, _op = client_with_operator
    foreign_tenant = uuid.uuid4()
    service = KbService()
    await service.create_entry(
        tenant_id=foreign_tenant,
        slug="foreign-only-runbook",
        body="Foreign tenant's entry body.",
    )

    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "resources/read",
            "params": {"uri": "meho://kb/foreign-only-runbook"},
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    # Error message says "not found" — same shape as a truly-missing
    # slug, deliberately not "forbidden" or "cross-tenant".
    assert "not found" in body["error"]["message"].lower()


# ---------------------------------------------------------------------------
# RBAC visibility — read_only operator
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.READ_ONLY],
    indirect=True,
)
def test_kb_resource_hidden_from_read_only_operator(
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
    assert "meho://kb/{slug}" not in uri_templates
