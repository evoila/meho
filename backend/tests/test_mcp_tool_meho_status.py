# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the ``meho_status`` reference MCP tool (G0.5-T4, #249).

Covers acceptance criteria 1-4 on issue #249:

* ``tools/list`` exposes ``meho_status`` with a well-formed JSON Schema
  2020-12 ``inputSchema``.
* ``tools/call`` with no arguments returns the operator identity + Vault
  + DB-migration bundle wire-identical to ``GET /api/v1/health`` (modulo
  the MCP ``content`` wrapper).
* ``tools/call`` with extra arguments still answers normally (#4137):
  a read tool with no arguments ignores the ones it is sent. Tools with
  arguments and write tools still refuse an unknown argument with
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
from typing import Any

import pytest
from fastapi.testclient import TestClient

from meho_backplane.api.v1.health import HealthResponse
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.main import app
from meho_backplane.mcp.auth import verify_mcp_jwt_and_bind
from meho_backplane.mcp.schemas import INVALID_PARAMS
from tests.mcp_test_fixtures import (
    build_operator,
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)


def test_tools_list_exposes_meho_status_with_well_formed_input_schema(
    client_with_operator: tuple[TestClient, Operator],
) -> None:
    """AC #1: ``tools/list`` surfaces ``meho_status`` with a 2020-12 schema."""
    client, _op = client_with_operator

    response = post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )

    assert response.status_code == 200
    body = response.json()
    tools = body["result"]["tools"]
    statuses = [t for t in tools if t["name"] == "meho_status"]
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
    """AC #2: ``tools/call meho_status`` wire-matches ``GET /api/v1/health``.

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
            "params": {"name": "meho_status", "arguments": {}},
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
    # #2746: tenant identity now rides the operator bundle so a connected
    # session can confirm which tenant/role it runs as.
    assert payload["operator"]["tenant_id"] == str(op.tenant_id)
    assert payload["operator"]["tenant_role"] == op.tenant_role.value

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
    # #4830: the revisions ride along as values, equal when migrated.
    assert payload["db"]["revision"] == payload["db"]["head"]
    assert payload["db"]["detail"] == f"revision={payload['db']['head']}"

    # #4830: the lifespan registered the real readiness probes, so the
    # checks ``/ready`` would show are in the response — ``db`` among them.
    readiness = payload["readiness"]
    assert isinstance(readiness["ready"], bool)
    names = {check["name"] for check in readiness["checks"]}
    assert "db" in names
    assert all({"name", "ok", "detail"} == set(check) for check in readiness["checks"])

    # #4830: approval settings as state words, never a key value.
    approvals = payload["approvals"]
    assert approvals["custody"] in {"configured", "missing", "malformed"}
    assert approvals["effective_posture"] in {"four_eyes_enforced", "single_operator_break_glass"}
    assert approvals["delegated_decision_posture"] in {
        "production_own_hand_only",
        "lab_class_delegation_allowed",
    }
    feature_names = {entry["name"] for entry in payload["features"]}
    assert feature_names >= {"approval_custody", "approval_queue"}


@pytest.mark.parametrize(
    "extra_arguments",
    [{"input": {}}, {"": ""}, {"connector_id": "x"}],
)
def test_tools_call_meho_status_ignores_extra_arguments(
    client_with_operator: tuple[TestClient, Operator],
    extra_arguments: dict[str, object],
) -> None:
    """#4137: a read tool with no arguments ignores extra ones and answers normally."""
    client, _op = client_with_operator

    plain = _call_meho_status(client)
    with_extra = _call_tool(client, "meho_status", extra_arguments)

    assert "error" not in with_extra, with_extra
    assert with_extra["result"]["isError"] is False
    payload = json.loads(with_extra["result"]["content"][0]["text"])
    assert payload.keys() == plain.keys()
    assert payload["operator"] == plain["operator"]


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        # A read tool that takes arguments: a wrong name can mean a wrong answer.
        ("list_targets", {"no_such_argument": "x"}),
        # A write tool keeps the strict check.
        ("add_to_memory", {"scope": "personal", "no_such_argument": "x"}),
    ],
)
def test_tools_call_unknown_argument_still_refused(
    client_with_operator: tuple[TestClient, Operator],
    tool: str,
    arguments: dict[str, object],
) -> None:
    """#4137: only no-argument read tools drop extra arguments; the rest refuse them."""
    client, _op = client_with_operator

    body = _call_tool(client, tool, arguments)

    assert body["error"]["code"] == INVALID_PARAMS
    assert "no_such_argument" in body["error"]["message"]


def _call_tool(client: TestClient, name: str, arguments: dict[str, object]) -> dict[str, Any]:
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return body


def _call_meho_status(client: TestClient) -> dict[str, object]:
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "meho_status", "arguments": {}},
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["result"]["isError"] is False
    payload: dict[str, object] = json.loads(body["result"]["content"][0]["text"])
    return payload


def _listed_tool_names(client: TestClient) -> list[str]:
    response = post_mcp(client, {"jsonrpc": "2.0", "id": 5, "method": "tools/list"})
    return sorted(t["name"] for t in response.json()["result"]["tools"])


def test_meho_status_carries_granted_scopes_and_surface_summary(
    client_with_operator: tuple[TestClient, Operator],
) -> None:
    """#3666: the payload names the session's scopes and its working surface.

    The shared fixture operator is ``mcp:admin``-elevated, so the summary
    reports the operator surface as listed. ``listed_working_tools`` must
    match the working-surface part of this session's own ``tools/list``.
    """
    client, op = client_with_operator

    payload = _call_meho_status(client)

    assert payload["granted_scopes"] == sorted(op.scopes)
    summary = payload["surface_summary"]
    assert isinstance(summary, dict)
    assert summary["operator_surface_listed"] is True
    expected = summary["expected_working_tools"]
    listed = summary["listed_working_tools"]
    missing = summary["missing_working_tools"]
    assert "meho_status" in listed
    assert expected == sorted(expected)
    assert sorted(listed + missing) == expected
    assert set(listed) <= set(_listed_tool_names(client))


def test_meho_status_surface_summary_for_default_session() -> None:
    """#3666: a non-elevated session reports its scopes and its hidden tools.

    No ``mcp:admin`` scope → the operator surface is not listed. The
    tenant holds no ``meho-docs`` capability, so the four docs tools are
    expected on the working surface but missing for this session.
    """
    op = build_operator(scopes=frozenset({"openid", "mcp:read"}))

    async def _fake_verify() -> Operator:
        return op

    app.dependency_overrides[verify_mcp_jwt_and_bind] = _fake_verify
    try:
        with TestClient(app) as client:
            payload = _call_meho_status(client)
            tools_listed = set(_listed_tool_names(client))
    finally:
        app.dependency_overrides.pop(verify_mcp_jwt_and_bind, None)

    assert payload["granted_scopes"] == ["mcp:read", "openid"]
    summary = payload["surface_summary"]
    assert isinstance(summary, dict)
    assert summary["operator_surface_listed"] is False
    assert {"search_docs", "ask_docs", "read_docs", "list_doc_collections"} <= set(
        summary["missing_working_tools"]
    )
    assert set(summary["listed_working_tools"]) == tools_listed


def test_health_response_defaults_keep_old_shape_decodable() -> None:
    """#3666: the new fields default to empty so older payloads still validate."""
    response = HealthResponse.model_validate(
        {
            "operator": {
                "sub": "s",
                "name": None,
                "email": None,
                "tenant_id": "00000000-0000-0000-0000-000000000001",
                "tenant_role": "read_only",
            },
            "vault": {"reachable": False, "read_ok": False},
            "db": {"migrated": True},
            "mcp_session_id_capture": "when_negotiated",
        }
    )
    assert response.granted_scopes == []
    assert response.surface_summary.listed_working_tools == []
    assert response.surface_summary.operator_surface_listed is False
    # #4830: an older backplane sends none of these.
    assert response.db.revision is None
    assert response.db.head is None
    assert response.readiness is None
    assert response.approvals is None
    assert response.features == []
