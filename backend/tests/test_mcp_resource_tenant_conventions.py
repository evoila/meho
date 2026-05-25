# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Integration tests for ``meho://tenant/{tenant_id}/conventions/{slug}`` (G7.1-T4 #316).

Acceptance criteria from the issue body:

* **``resources/templates/list`` includes the conventions URI template.**
* **``resources/read meho://tenant/<id>/conventions/<slug>`` returns Markdown.**
  (Body returned as JSON wrapped in a ``contents`` entry whose
  ``mimeType`` is ``text/markdown`` per the registry definition.)
* **Cross-tenant resource access -> 403** (mapped to JSON-RPC
  ``-32602`` per the transport contract -- the JSON-RPC envelope
  doesn't carry HTTP-status semantics; ``-32602`` plus the "cross-
  tenant" message is the spec-correct shape, mirroring the
  G0.5-T4 ``tenant_info`` resource decision).

The tests use the shared ``mcp_test_fixtures`` constellation; the
``isolated_registry`` autouse fixture reloads the resource module
between tests so the registration side effect re-runs in each test.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from meho_backplane.auth.operator import Operator
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import TenantConvention
from meho_backplane.mcp.schemas import INVALID_PARAMS
from tests.mcp_test_fixtures import (
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
    seeded_operator_tenant,  # noqa: F401 — pytest-discovered fixture
)


def test_resources_templates_list_exposes_tenant_conventions(
    client_with_operator: tuple[TestClient, Operator],
) -> None:
    """The conventions URI template is registered and visible to read_only operators.

    Acceptance criterion: "``resources/templates/list`` includes
    the conventions URI template." Validates both the registration
    side effect AND the RBAC-filter inclusion (the fixture operator
    is READ_ONLY by default and the template is registered with
    ``required_role=READ_ONLY``).
    """
    client, _op = client_with_operator

    response = post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 1, "method": "resources/templates/list"},
    )
    assert response.status_code == 200
    body = response.json()
    templates = body["result"]["resourceTemplates"]
    conventions = [
        t for t in templates if t["uriTemplate"] == "meho://tenant/{tenant_id}/conventions/{slug}"
    ]
    assert len(conventions) == 1
    assert conventions[0]["mimeType"] == "text/markdown"
    # MEHO-internal RBAC field is stripped from the wire shape.
    assert "required_role" not in conventions[0]


@pytest.mark.asyncio
async def test_resources_read_own_tenant_returns_full_convention(
    client_with_operator: tuple[TestClient, Operator],
    seeded_operator_tenant: None,
) -> None:
    """Own-tenant read returns the full convention shape.

    The dispatcher wraps the handler's return value in a
    ``contents`` array whose ``text`` is the JSON-serialised dict;
    we parse it back to assert the full convention fields are
    present (id, tenant_id, slug, title, body, kind, priority,
    created_by_sub, created_at, updated_at).
    """
    client, op = client_with_operator
    convention_id = uuid.uuid4()
    now = datetime.now(UTC)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            TenantConvention(
                id=convention_id,
                tenant_id=op.tenant_id,
                slug="rbac-canonical",
                title="RBAC is canonical",
                body="Every operation runs through MEHO's RBAC layer.",
                kind="operational",
                priority=10,
                created_by_sub="test:user",
                created_at=now,
                updated_at=now,
            ),
        )
        await session.commit()

    uri = f"meho://tenant/{op.tenant_id}/conventions/rbac-canonical"
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "resources/read",
            "params": {"uri": uri},
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert "error" not in body
    contents = body["result"]["contents"]
    assert contents[0]["uri"] == uri
    assert contents[0]["mimeType"] == "text/markdown"
    bundle = json.loads(contents[0]["text"])
    assert bundle["id"] == str(convention_id)
    assert bundle["tenant_id"] == str(op.tenant_id)
    assert bundle["slug"] == "rbac-canonical"
    assert bundle["title"] == "RBAC is canonical"
    assert bundle["body"] == "Every operation runs through MEHO's RBAC layer."
    assert bundle["kind"] == "operational"
    assert bundle["priority"] == 10
    assert bundle["created_by_sub"] == "test:user"


def test_resources_read_cross_tenant_returns_invalid_params(
    client_with_operator: tuple[TestClient, Operator],
) -> None:
    """Bound tenant != operator tenant -> -32602 with "cross-tenant" message.

    Acceptance criterion: "Cross-tenant resource access -> 403."
    Per the transport, "403" maps to ``-32602`` plus the
    "cross-tenant" string in the message (same shape
    ``tenant_info`` settled on, G0.5-T4 #249). The check runs
    BEFORE the DB query so this test doesn't need to seed any rows.
    """
    client, _op = client_with_operator
    foreign_tenant = uuid4()
    uri = f"meho://tenant/{foreign_tenant}/conventions/rbac-canonical"

    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "resources/read",
            "params": {"uri": uri},
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "cross-tenant" in body["error"]["message"].lower()


def test_resources_read_invalid_tenant_uuid_returns_invalid_params(
    client_with_operator: tuple[TestClient, Operator],
) -> None:
    """Non-UUID ``{tenant_id}`` binding -> -32602.

    Same defensive shape :mod:`tenant_info` enforces. The URI must
    still match the template's path shape for the matcher to bind
    ``tenant_id``; the bound value is then validated as a UUID
    inside the handler.
    """
    client, _op = client_with_operator
    uri = "meho://tenant/not-a-uuid/conventions/rbac-canonical"

    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "resources/read",
            "params": {"uri": uri},
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "not a uuid" in body["error"]["message"].lower()


@pytest.mark.asyncio
async def test_resources_read_unknown_slug_returns_invalid_params(
    client_with_operator: tuple[TestClient, Operator],
    seeded_operator_tenant: None,
) -> None:
    """Unknown slug in own tenant -> -32602 "convention not found".

    Distinct from the cross-tenant arm: the tenant_id IS the
    operator's, so the boundary check passes; the rejection is on
    the (tenant_id, slug) lookup miss.
    """
    client, op = client_with_operator
    uri = f"meho://tenant/{op.tenant_id}/conventions/no-such-slug"

    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "resources/read",
            "params": {"uri": uri},
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "not found" in body["error"]["message"].lower()


def test_resources_read_invalid_slug_shape_returns_invalid_params(
    client_with_operator: tuple[TestClient, Operator],
) -> None:
    """Malformed slug (uppercase / special chars) -> -32602 before DB probe."""
    client, op = client_with_operator
    # The slug "Bad_Slug" has uppercase + underscore, both outside
    # the URL-safe shape the substrate enforces. The template
    # matcher will bind it (any non-``/`` content matches); the
    # handler rejects it pre-DB.
    uri = f"meho://tenant/{op.tenant_id}/conventions/Bad_Slug"

    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "resources/read",
            "params": {"uri": uri},
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "invalid slug" in body["error"]["message"].lower()
