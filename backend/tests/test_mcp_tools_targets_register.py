# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the ``meho_targets_register`` admin tool (#2861).

Coverage matrix (Task #2861 acceptance criteria — the MCP-level half;
the ``create_target`` service and its guards are covered end-to-end in
:mod:`tests.test_api_v1_targets`):

* The tool registers with ``required_role=TENANT_ADMIN`` +
  ``op_class='write'`` + ``feature='targets'``, a conforming
  ``^[a-zA-Z0-9_-]{1,64}$`` name, and a parked-union ``outputSchema``;
  non-admin sessions do not see it in ``tools/list`` and a direct
  ``tools/call`` from an operator returns -32602 ``forbidden``.
* A human ``tenant_admin`` call creates the target immediately and
  ``list_targets`` shows the new row (round-trip).
* An **agent-principal** call parks as a durable ``ApprovalRequest``
  (``awaiting_approval`` envelope) and writes no target row — the
  G11.2 policy gate, mirroring ``meho_topology_create_node``.
* The reused ``create_target`` guards fire through this path: unknown
  ``product`` → error, ``secret_ref`` outside the tenant subtree →
  error, SSRF-y ``host`` → error.
* ``tenant_id`` and ``fingerprint`` are rejected at the schema layer
  (``additionalProperties: false``) — ``tenant_id`` always comes from
  the caller identity, ``fingerprint`` is server-managed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, Tenant
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.main import app
from meho_backplane.mcp.auth import verify_mcp_jwt_and_bind
from meho_backplane.mcp.registry import get_tool
from meho_backplane.mcp.schemas import INVALID_PARAMS
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

_TOOL = "meho_targets_register"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def _seeded_tenant() -> AsyncIterator[None]:
    """Insert the operator's :class:`Tenant` row so the target FK resolves."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        session.add(Tenant(id=OPERATOR_TENANT_ID, slug="op-tenant", name="Op Tenant"))
    yield


def _register_call(client: TestClient, call_id: int, arguments: dict[str, Any]) -> Any:
    return client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": call_id,
            "method": "tools/call",
            "params": {"name": _TOOL, "arguments": arguments},
        },
    )


def _list_targets_call(client: TestClient, call_id: int) -> Any:
    return client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": call_id,
            "method": "tools/call",
            "params": {"name": "list_targets", "arguments": {}},
        },
    )


def _tools_list(client: TestClient) -> list[dict[str, Any]]:
    body = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    ).json()
    return body["result"]["tools"]


@pytest.fixture
def _agent_admin_client() -> Iterator[tuple[TestClient, Operator]]:
    """``TestClient`` bound to an AGENT-principal ``tenant_admin`` operator.

    The shared ``client_with_operator`` fixture only builds USER-kind
    operators; the policy gate's agent-parking branch keys off
    ``principal_kind == AGENT``, so this fixture supplies one to exercise
    the parked path.
    """
    op = Operator(
        sub="agent:onboarder",
        name="Onboarder",
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=OPERATOR_TENANT_ID,
        tenant_role=TenantRole.TENANT_ADMIN,
        principal_kind=PrincipalKind.AGENT,
    )

    async def _fake_verify() -> Operator:
        return op

    app.dependency_overrides[verify_mcp_jwt_and_bind] = _fake_verify
    try:
        with TestClient(app) as client:
            yield client, op
    finally:
        app.dependency_overrides.pop(verify_mcp_jwt_and_bind, None)


async def _count(model: Any) -> int:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(func.count()).select_from(model).where(model.tenant_id == OPERATOR_TENANT_ID)
        )
        return int(result.scalar_one())


# ---------------------------------------------------------------------------
# Registration + conformance shape
# ---------------------------------------------------------------------------


def test_tool_registers_with_tenant_admin_write_and_targets_feature() -> None:
    """The admin tool lands with the TENANT_ADMIN gate + write op_class."""
    entry = get_tool(_TOOL)
    assert entry is not None
    defn, _handler = entry
    assert defn.required_role is TenantRole.TENANT_ADMIN
    assert defn.op_class == "write"
    assert defn.feature == "targets"


def test_tool_name_matches_anthropic_pattern() -> None:
    """The name satisfies the #2745 ``^[a-zA-Z0-9_-]{1,64}$`` gate."""
    import re

    assert re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", _TOOL)


def test_output_schema_is_parked_union_object_root() -> None:
    """The #2774 outputSchema is a ``type: object`` root with the parked union."""
    entry = get_tool(_TOOL)
    assert entry is not None
    defn, _handler = entry
    schema = defn.outputSchema
    assert schema is not None
    assert schema["type"] == "object"
    branches = schema["oneOf"]
    parked = next(b for b in branches if "awaiting_approval" in str(b))
    assert parked["properties"]["status"]["const"] == "awaiting_approval"


def test_input_schema_requires_core_fields_and_forbids_extras() -> None:
    """Required ``name/product/host``; ``additionalProperties: false``."""
    entry = get_tool(_TOOL)
    assert entry is not None
    defn, _handler = entry
    schema = defn.inputSchema
    assert schema["required"] == ["name", "product", "host"]
    assert schema["additionalProperties"] is False
    # The documented optional set is present; the two server-managed
    # inputs are absent (so additionalProperties rejects them).
    props = schema["properties"]
    assert "fingerprint" not in props
    assert "tenant_id" not in props
    for optional in ("aliases", "version", "port", "fqdn", "secret_ref", "auth_model"):
        assert optional in props


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_hidden_from_non_admin_tools_list(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """An operator (non-admin) does not see the write tool in ``tools/list``."""
    client, _op = client_with_operator
    names = {t["name"] for t in _tools_list(client)}
    assert _TOOL not in names


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_call_from_non_admin_is_forbidden(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """A direct ``tools/call`` from an operator → -32602 ``forbidden``."""
    client, _op = client_with_operator
    body = _register_call(
        client, 1, {"name": "x", "product": "vault", "host": "vault.example"}
    ).json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "forbidden" in body["error"]["message"].lower()


# ---------------------------------------------------------------------------
# Executed branch (human tenant_admin) + round-trip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
async def test_human_admin_creates_and_round_trips_via_list_targets(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    _seeded_tenant: None,
) -> None:
    """A human tenant_admin registration executes immediately + is listable."""
    client, op = client_with_operator
    created = _register_call(
        client, 1, {"name": "rdc-vault", "product": "vault", "host": "vault.example"}
    ).json()["result"]
    assert created["isError"] is False
    payload = created["structuredContent"]
    assert payload["name"] == "rdc-vault"
    assert payload["product"] == "vault"
    assert payload["tenant_id"] == str(op.tenant_id)

    listed = _list_targets_call(client, 2).json()["result"]["structuredContent"]
    assert [t["name"] for t in listed["targets"]] == ["rdc-vault"]
    assert listed["targets"][0]["id"] == payload["target_id"]


# ---------------------------------------------------------------------------
# Parked branch (agent principal) — the G11.2 policy gate
# ---------------------------------------------------------------------------


async def test_agent_principal_parks_as_needs_approval(
    _agent_admin_client: tuple[TestClient, Operator],
    _seeded_tenant: None,
) -> None:
    """An agent-principal registration parks; no target row lands."""
    client, _op = _agent_admin_client
    result = _register_call(
        client, 1, {"name": "agent-rke2", "product": "vault", "host": "vault.example"}
    ).json()["result"]
    assert result["isError"] is False
    payload = result["structuredContent"]
    assert payload["status"] == "awaiting_approval"
    assert payload["op_id"] == "targets.register"

    # The write parked as a durable ApprovalRequest and no target landed.
    assert await _count(ApprovalRequest) == 1
    assert await _count(TargetORM) == 0


# ---------------------------------------------------------------------------
# Reused guards fire through this path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
async def test_unknown_product_is_rejected(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    _seeded_tenant: None,
) -> None:
    """The reused product-token validation rejects an unknown product."""
    client, _op = client_with_operator
    body = _register_call(
        client,
        1,
        {"name": "bad-prod", "product": "definitely-not-a-real-product", "host": "svc.example"},
    ).json()
    assert body["error"]["code"] == INVALID_PARAMS


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
async def test_ssrf_host_is_rejected(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    _seeded_tenant: None,
) -> None:
    """The reused SSRF guard rejects a non-public ``host``.

    ``169.254.169.254`` (cloud-metadata link-local) is the address the
    suite-wide allowlist deliberately leaves blocked (see the
    ``_default_target_ssrf_allowlist`` conftest fixture), and IP literals
    are screened directly — so the guard fires without re-patching the
    DNS seam.
    """
    client, _op = client_with_operator
    body = _register_call(
        client, 1, {"name": "ssrf", "product": "vault", "host": "169.254.169.254"}
    ).json()
    assert body["error"]["code"] == INVALID_PARAMS


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
async def test_secret_ref_outside_tenant_scope_is_rejected(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    _seeded_tenant: None,
) -> None:
    """The reused ``secret_ref`` tenant-scope guard rejects an out-of-subtree ref."""
    client, _op = client_with_operator
    body = _register_call(
        client,
        1,
        {
            "name": "oos-secret",
            "product": "vault",
            "host": "vault.example",
            "secret_ref": "secret/meho/vcf-logs/logmaster",
        },
    ).json()
    assert body["error"]["code"] == INVALID_PARAMS


# ---------------------------------------------------------------------------
# tenant_id / fingerprint are schema-rejected (never params)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
def test_smuggled_tenant_id_is_rejected_at_schema_layer(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """A ``tenant_id`` in arguments → -32602 (additionalProperties=false)."""
    client, _op = client_with_operator
    body = _register_call(
        client,
        1,
        {
            "name": "x",
            "product": "vault",
            "host": "vault.example",
            "tenant_id": "00000000-0000-0000-0000-0000000000ff",
        },
    ).json()
    assert body["error"]["code"] == INVALID_PARAMS


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
def test_smuggled_fingerprint_is_rejected_at_schema_layer(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """A server-managed ``fingerprint`` in arguments → -32602."""
    client, _op = client_with_operator
    body = _register_call(
        client,
        1,
        {
            "name": "x",
            "product": "vault",
            "host": "vault.example",
            "fingerprint": {"vendor": "spoofed"},
        },
    ).json()
    assert body["error"]["code"] == INVALID_PARAMS
