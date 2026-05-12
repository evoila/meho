# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the ``meho.status`` reference MCP tool (G0.5-T4, #249).

Covers acceptance criteria 1-4 on issue #249:

* ``tools/list`` exposes ``meho.status`` with a well-formed JSON Schema
  2020-12 ``inputSchema``.
* ``tools/call`` with no arguments returns the operator identity + Vault
  + DB-migration bundle wire-identical to ``GET /api/v1/health`` (modulo
  the MCP ``content`` wrapper).
* ``tools/call`` with an extra argument violates the
  ``additionalProperties: false`` constraint and surfaces as
  ``INVALID_PARAMS`` (-32602).

AC #4 (sub-``read_only`` role rejection) is satisfied by the registry's
call-time RBAC re-check — already tested in
:mod:`tests.test_mcp_registry`'s
``test_tools_call_forbidden_for_under_privileged_operator`` — plus the
``required_role=READ_ONLY`` on this tool's definition. The AC itself
notes "which doesn't exist in v0.2, but the check is defensive";
adding a duplicate end-to-end test here would only re-exercise registry
machinery T3 already proved.

Vault + DB notes
================

The lifespan startup that triggers ``eager_import_mcp_modules`` also
exercises the chassis env (DB engine, settings). The handler under
test calls
:func:`~meho_backplane.api.v1.health.build_health_response`, which in
turn probes Vault. Tests don't mock Vault: the probe is designed to
return :class:`VaultStatus(reachable=False, …)` on a failed login, and
the AC wants *wire-shape identity* with the chassis route, which holds
regardless of the probe outcome.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from meho_backplane.auth.operator import Operator
from meho_backplane.mcp.schemas import INVALID_PARAMS
from tests.mcp_test_fixtures import (
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)


def test_tools_list_exposes_meho_status_with_well_formed_input_schema(
    client_with_operator: tuple[TestClient, Operator],
) -> None:
    """AC #1: ``tools/list`` surfaces ``meho.status`` with a 2020-12 schema."""
    client, _op = client_with_operator

    response = post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )

    assert response.status_code == 200
    body = response.json()
    tools = body["result"]["tools"]
    statuses = [t for t in tools if t["name"] == "meho.status"]
    assert len(statuses) == 1
    schema = statuses[0]["inputSchema"]
    assert schema["type"] == "object"
    assert schema["properties"] == {}
    assert schema["additionalProperties"] is False
    # MEHO-internal fields stripped from the wire shape.
    assert "required_role" not in statuses[0]
    assert "op_class" not in statuses[0]


def test_tools_call_meho_status_returns_health_response_shape(
    client_with_operator: tuple[TestClient, Operator],
) -> None:
    """AC #2: ``tools/call meho.status`` wire-matches ``GET /api/v1/health``.

    The MCP envelope wraps the response in ``content[0].text`` carrying
    a JSON-serialised dict. Decoding that dict and asserting against the
    chassis :class:`HealthResponse` model is the canonical "wire-shape
    identical (modulo wrapper)" check.
    """
    client, op = client_with_operator

    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "meho.status", "arguments": {}},
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["result"]["isError"] is False
    payload = json.loads(body["result"]["content"][0]["text"])

    # Operator identity matches the fixture operator.
    assert payload["operator"]["sub"] == op.sub
    assert payload["operator"]["name"] == op.name
    assert payload["operator"]["email"] is None

    # Vault federation status is structurally present. The test environment
    # has no real Vault — the probe is designed to fail closed, returning
    # ``reachable=False`` with a structured detail.
    assert "reachable" in payload["vault"]
    assert "read_ok" in payload["vault"]
    assert payload["vault"]["reachable"] is False

    # DB-migration probe runs against the test SQLite (set up by the autouse
    # ``_default_database_url`` fixture in conftest). The probe should
    # report healthy because conftest ran ``alembic upgrade head``.
    assert payload["db"]["migrated"] is True


def test_tools_call_meho_status_rejects_extra_arguments(
    client_with_operator: tuple[TestClient, Operator],
) -> None:
    """AC #3: extra args fail the ``additionalProperties: false`` schema → -32602."""
    client, _op = client_with_operator

    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "meho.status", "arguments": {"foo": "bar"}},
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "inputschema" in body["error"]["message"].lower()
