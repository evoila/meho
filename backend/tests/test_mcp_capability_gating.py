# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the tenant-provisioned MCP capability gate (G4.5-T1, #1519).

The capability gate is a second axis orthogonal to the role gate: a
``ToolDefinition`` / ``ResourceTemplateDefinition`` carrying
``required_capability="x"`` is **absent** from ``tools/list`` /
``resources/templates/list`` AND rejected with a 403-class error at
``tools/call`` / ``resources/read`` for any operator whose
``capabilities`` set lacks ``"x"``. It mirrors the existing role gate
(:func:`role_at_least`) — provisioning is a tenant-level capability
toggle, not a packaging/entitlement system.

Acceptance criteria covered (issue #1519):

* AC1 — a capability-gated tool is absent from ``tools/list`` for an
  operator lacking the capability and present when they have it; role
  gating is unchanged for tools without ``required_capability``.
* AC2 — ``tools/call`` on a capability-gated tool returns a 403-class
  error (not a handler error) when the capability is absent, even when
  the client names the tool directly.
* AC3 — ``Operator.capabilities`` is populated from the configured JWT
  claim; an absent claim → empty set (fail-closed).
* AC4 — ``meho://tenant/{id}/info`` includes a ``capabilities`` array
  matching the operator's set.
* AC5 — ``to_wire()`` does not leak ``required_capability`` onto the
  MCP wire.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from meho_backplane.auth.jwt import _extract_capabilities
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.main import app
from meho_backplane.mcp import (
    ResourceTemplateDefinition,
    ToolDefinition,
    register_mcp_resource,
    register_mcp_tool,
)
from meho_backplane.mcp.auth import verify_mcp_jwt_and_bind
from meho_backplane.mcp.registry import (
    all_resource_templates_for,
    all_tools_for,
    capability_satisfied,
    clear_registries,
)
from meho_backplane.mcp.schemas import INVALID_PARAMS
from meho_backplane.settings import get_settings
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    post_mcp,
    seeded_operator_tenant,  # noqa: F401 — pytest-discovered fixture
)

_DOCS_CAPABILITY = "meho-docs"


def _operator(
    *,
    role: TenantRole = TenantRole.READ_ONLY,
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


# ---------------------------------------------------------------------------
# AC5 — to_wire() never leaks the MEHO-internal capability field
# ---------------------------------------------------------------------------


def test_tool_to_wire_strips_required_capability() -> None:
    """``ToolDefinition.to_wire`` drops ``required_capability`` from the wire."""
    defn = ToolDefinition(
        name="docs.search",
        description="Capability-gated docs search",
        inputSchema={"type": "object", "properties": {}},
        required_capability=_DOCS_CAPABILITY,
    )
    wire = defn.to_wire()
    assert "required_capability" not in wire
    assert "required_role" not in wire
    assert "op_class" not in wire
    # The field is still readable on the model for server-side gating.
    assert defn.required_capability == _DOCS_CAPABILITY


def test_resource_template_to_wire_strips_required_capability() -> None:
    """``ResourceTemplateDefinition.to_wire`` drops ``required_capability``."""
    defn = ResourceTemplateDefinition(
        uriTemplate="meho://docs/{slug}",
        name="docs chunk",
        description="Capability-gated docs resource",
        required_capability=_DOCS_CAPABILITY,
    )
    wire = defn.to_wire()
    assert "required_capability" not in wire
    assert "required_role" not in wire
    assert defn.required_capability == _DOCS_CAPABILITY


def test_required_capability_defaults_to_none() -> None:
    """An undeclared ``required_capability`` defaults to ``None`` (no gate)."""
    defn = ToolDefinition(
        name="ungated.tool",
        description="A tool with no capability gate",
        inputSchema={"type": "object", "properties": {}},
    )
    assert defn.required_capability is None


# ---------------------------------------------------------------------------
# capability_satisfied — the shared gate predicate
# ---------------------------------------------------------------------------


def test_capability_satisfied_none_always_admits() -> None:
    """``required_capability=None`` admits any operator (no gate)."""
    assert capability_satisfied(_operator(), None) is True
    assert capability_satisfied(_operator(capabilities=frozenset({"x"})), None) is True


def test_capability_satisfied_requires_membership() -> None:
    """A declared capability admits iff it is in the operator's set."""
    op_with = _operator(capabilities=frozenset({_DOCS_CAPABILITY}))
    op_without = _operator(capabilities=frozenset({"other"}))
    op_empty = _operator()
    assert capability_satisfied(op_with, _DOCS_CAPABILITY) is True
    assert capability_satisfied(op_without, _DOCS_CAPABILITY) is False
    # Fail-closed: an empty set never satisfies a capability gate.
    assert capability_satisfied(op_empty, _DOCS_CAPABILITY) is False


# ---------------------------------------------------------------------------
# AC1 — list-time true absence + presence, role gating unchanged
# ---------------------------------------------------------------------------


@pytest.fixture
def _isolated_registries() -> Iterator[None]:
    """Reset the module-level registries around a unit-level gating test."""
    clear_registries()
    yield
    clear_registries()


def _register_one_gated_one_ungated() -> None:
    """Register one capability-gated tool + one ungated tool (same role)."""

    async def _stub(_op: Operator, _args: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True}

    register_mcp_tool(
        ToolDefinition(
            name="ungated.tool",
            description="Always visible",
            inputSchema={"type": "object", "properties": {}},
            required_role=TenantRole.READ_ONLY,
        ),
        _stub,
    )
    register_mcp_tool(
        ToolDefinition(
            name="docs.search",
            description="Gated on the meho-docs capability",
            inputSchema={"type": "object", "properties": {}},
            required_role=TenantRole.READ_ONLY,
            required_capability=_DOCS_CAPABILITY,
        ),
        _stub,
    )


def test_all_tools_for_hides_gated_tool_without_capability(
    _isolated_registries: None,
) -> None:
    """AC1: a capability-gated tool is absent for an operator lacking it.

    The ungated tool stays visible — role gating is unchanged for tools
    that declare no ``required_capability``.
    """
    _register_one_gated_one_ungated()
    visible = [t.name for t in all_tools_for(_operator())]
    assert visible == ["ungated.tool"]


def test_all_tools_for_shows_gated_tool_with_capability(
    _isolated_registries: None,
) -> None:
    """AC1: the gated tool appears once the operator is provisioned."""
    _register_one_gated_one_ungated()
    op = _operator(capabilities=frozenset({_DOCS_CAPABILITY}))
    visible = [t.name for t in all_tools_for(op)]
    assert visible == ["ungated.tool", "docs.search"]


def test_capability_gate_is_orthogonal_to_role_gate(
    _isolated_registries: None,
) -> None:
    """A tool gated on BOTH a role and a capability needs both to pass.

    The capability gate does not relax the role gate: an operator with
    the capability but an insufficient role still doesn't see the tool.
    """

    async def _stub(_op: Operator, _args: dict[str, Any]) -> dict[str, Any]:
        return {}

    register_mcp_tool(
        ToolDefinition(
            name="admin.docs",
            description="Admin-only AND meho-docs gated",
            inputSchema={"type": "object", "properties": {}},
            required_role=TenantRole.TENANT_ADMIN,
            required_capability=_DOCS_CAPABILITY,
        ),
        _stub,
    )
    # Has the capability but only read_only role → still hidden.
    read_only_with_cap = _operator(
        role=TenantRole.READ_ONLY,
        capabilities=frozenset({_DOCS_CAPABILITY}),
    )
    assert all_tools_for(read_only_with_cap) == []
    # Admin role but no capability → still hidden.
    admin_without_cap = _operator(role=TenantRole.TENANT_ADMIN)
    assert all_tools_for(admin_without_cap) == []
    # Both → visible.
    admin_with_cap = _operator(
        role=TenantRole.TENANT_ADMIN,
        capabilities=frozenset({_DOCS_CAPABILITY}),
    )
    assert [t.name for t in all_tools_for(admin_with_cap)] == ["admin.docs"]


def test_all_resource_templates_for_applies_capability_gate(
    _isolated_registries: None,
) -> None:
    """The resource-template list filter applies the same capability gate."""

    async def _stub(_op: Operator, _params: dict[str, str]) -> dict[str, Any]:
        return {}

    register_mcp_resource(
        ResourceTemplateDefinition(
            uriTemplate="meho://docs/{slug}",
            name="docs chunk",
            description="Gated docs resource",
            required_role=TenantRole.READ_ONLY,
            required_capability=_DOCS_CAPABILITY,
        ),
        _stub,
    )
    assert all_resource_templates_for(_operator()) == []
    provisioned = _operator(capabilities=frozenset({_DOCS_CAPABILITY}))
    visible = [r.uriTemplate for r in all_resource_templates_for(provisioned)]
    assert visible == ["meho://docs/{slug}"]


# ---------------------------------------------------------------------------
# End-to-end: list absence + call-time 403 over the JSON-RPC surface
# ---------------------------------------------------------------------------


@pytest.fixture
def gated_client(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, Operator]]:
    """``TestClient`` whose operator + a single capability-gated tool are pinned.

    The operator's capability set is parametrised via
    ``@pytest.mark.parametrize("gated_client", [frozenset({...})], indirect=True)``;
    default is the empty set (unprovisioned). A lone capability-gated
    ``docs.search`` tool is registered after the lifespan's eager import
    so the test exercises the gate in isolation from the production
    registry.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("BACKPLANE_URL", "https://meho.test")
    get_settings.cache_clear()

    capabilities: frozenset[str] = getattr(request, "param", frozenset())
    op = _operator(role=TenantRole.OPERATOR, capabilities=capabilities)

    async def _fake_verify() -> Operator:
        return op

    handler_calls: dict[str, int] = {"count": 0}

    async def _docs_handler(_op: Operator, _args: dict[str, Any]) -> dict[str, Any]:
        handler_calls["count"] += 1
        return {"results": []}

    app.dependency_overrides[verify_mcp_jwt_and_bind] = _fake_verify
    try:
        with TestClient(app) as client:
            # Register AFTER lifespan eager-import so the gated tool is
            # present for this test only; clear at teardown.
            register_mcp_tool(
                ToolDefinition(
                    name="docs.search",
                    description="Gated on the meho-docs capability",
                    inputSchema={"type": "object", "properties": {}},
                    required_role=TenantRole.READ_ONLY,
                    required_capability=_DOCS_CAPABILITY,
                ),
                _docs_handler,
            )
            client._docs_handler_calls = handler_calls  # type: ignore[attr-defined]
            yield client, op
    finally:
        app.dependency_overrides.pop(verify_mcp_jwt_and_bind, None)
        clear_registries()
        get_settings.cache_clear()


def test_tools_list_omits_gated_tool_for_unprovisioned_operator(
    gated_client: tuple[TestClient, Operator],
) -> None:
    """AC1 (e2e): the gated tool is absent from ``tools/list`` by default."""
    client, _op = gated_client
    response = post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert response.status_code == 200
    names = [t["name"] for t in response.json()["result"]["tools"]]
    assert "docs.search" not in names


@pytest.mark.parametrize(
    "gated_client",
    [frozenset({_DOCS_CAPABILITY})],
    indirect=True,
)
def test_tools_list_includes_gated_tool_for_provisioned_operator(
    gated_client: tuple[TestClient, Operator],
) -> None:
    """AC1 (e2e): the gated tool appears once the operator is provisioned."""
    client, _op = gated_client
    response = post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert response.status_code == 200
    names = [t["name"] for t in response.json()["result"]["tools"]]
    assert "docs.search" in names


def test_tools_call_gated_tool_returns_403_when_unprovisioned(
    gated_client: tuple[TestClient, Operator],
) -> None:
    """AC2: naming the gated tool directly still 403s when unprovisioned.

    The error is a 403-class capability rejection (INVALID_PARAMS on the
    JSON-RPC wire, ``forbidden`` message) — NOT a handler error: the
    handler must never run, so knowing the name can't bypass the gate.
    """
    client, _op = gated_client
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "docs.search", "arguments": {}},
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "forbidden" in body["error"]["message"].lower()
    assert "capability" in body["error"]["message"].lower()
    # The gated handler never ran.
    assert client._docs_handler_calls["count"] == 0  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "gated_client",
    [frozenset({_DOCS_CAPABILITY})],
    indirect=True,
)
def test_tools_call_gated_tool_dispatches_when_provisioned(
    gated_client: tuple[TestClient, Operator],
) -> None:
    """AC2 (positive): a provisioned operator reaches the handler."""
    client, _op = gated_client
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "docs.search", "arguments": {}},
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["result"]["isError"] is False
    assert client._docs_handler_calls["count"] == 1  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# AC4 — meho://tenant/{id}/info exposes the capabilities array
# ---------------------------------------------------------------------------


@pytest.fixture
def tenant_info_client(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, Operator]]:
    """``TestClient`` for the tenant_info resource with a parametrised cap set."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("BACKPLANE_URL", "https://meho.test")
    get_settings.cache_clear()

    capabilities: frozenset[str] = getattr(request, "param", frozenset())
    op = _operator(role=TenantRole.READ_ONLY, capabilities=capabilities)

    async def _fake_verify() -> Operator:
        return op

    app.dependency_overrides[verify_mcp_jwt_and_bind] = _fake_verify
    try:
        with TestClient(app) as client:
            yield client, op
    finally:
        app.dependency_overrides.pop(verify_mcp_jwt_and_bind, None)
        get_settings.cache_clear()


@pytest.mark.parametrize(
    "tenant_info_client",
    [frozenset({_DOCS_CAPABILITY, "other-addon"})],
    indirect=True,
)
def test_tenant_info_exposes_sorted_capabilities_array(
    tenant_info_client: tuple[TestClient, Operator],
    seeded_operator_tenant: None,
) -> None:
    """AC4: the tenant-info bundle carries a sorted ``capabilities`` array."""
    client, op = tenant_info_client
    uri = f"meho://tenant/{op.tenant_id}/info"
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
    bundle = json.loads(response.json()["result"]["contents"][0]["text"])
    assert bundle["capabilities"] == sorted({_DOCS_CAPABILITY, "other-addon"})


def test_tenant_info_capabilities_empty_for_unprovisioned_operator(
    tenant_info_client: tuple[TestClient, Operator],
    seeded_operator_tenant: None,
) -> None:
    """AC4: an unprovisioned operator's bundle carries an empty array."""
    client, op = tenant_info_client
    uri = f"meho://tenant/{op.tenant_id}/info"
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
    bundle = json.loads(response.json()["result"]["contents"][0]["text"])
    assert bundle["capabilities"] == []


# ---------------------------------------------------------------------------
# AC3 — capabilities are extracted from the configured JWT claim
# ---------------------------------------------------------------------------


class _StubSettings:
    """Minimal settings stand-in carrying only the capabilities claim name."""

    def __init__(self, claim_name: str = "capabilities") -> None:
        self.jwt_capabilities_claim_name = claim_name


def test_extract_capabilities_from_list_claim() -> None:
    """AC3: a JSON array of strings becomes the operator's capability set."""
    claims = {"capabilities": ["meho-docs", "other"]}
    result = _extract_capabilities(claims, _StubSettings())  # type: ignore[arg-type]
    assert result == frozenset({"meho-docs", "other"})


def test_extract_capabilities_absent_claim_is_empty_fail_closed() -> None:
    """AC3: an absent claim resolves to the empty set (fail-closed)."""
    result = _extract_capabilities({}, _StubSettings())  # type: ignore[arg-type]
    assert result == frozenset()


def test_extract_capabilities_single_string_claim_coerced() -> None:
    """A scalar string claim is coerced to a one-element set."""
    claims = {"capabilities": "meho-docs"}
    result = _extract_capabilities(claims, _StubSettings())  # type: ignore[arg-type]
    assert result == frozenset({"meho-docs"})


def test_extract_capabilities_drops_non_string_entries() -> None:
    """Non-string entries in the array are dropped, not coerced or raised."""
    claims = {"capabilities": ["meho-docs", 42, None, "other"]}
    result = _extract_capabilities(claims, _StubSettings())  # type: ignore[arg-type]
    assert result == frozenset({"meho-docs", "other"})


def test_extract_capabilities_malformed_claim_is_empty_fail_closed() -> None:
    """A non-array, non-string claim (e.g. an object) → empty set."""
    claims = {"capabilities": {"unexpected": "object"}}
    result = _extract_capabilities(claims, _StubSettings())  # type: ignore[arg-type]
    assert result == frozenset()


def test_extract_capabilities_honours_configured_claim_name() -> None:
    """AC3: the claim name is settings-controlled."""
    claims = {"caps": ["meho-docs"]}
    result = _extract_capabilities(
        claims,
        _StubSettings(claim_name="caps"),  # type: ignore[arg-type]
    )
    assert result == frozenset({"meho-docs"})
    # The default claim name finds nothing in this token.
    assert _extract_capabilities(claims, _StubSettings()) == frozenset()  # type: ignore[arg-type]


def test_operator_default_capabilities_is_empty_frozenset() -> None:
    """Constructing an Operator without capabilities defaults to empty set."""
    op = Operator(
        sub="op",
        raw_jwt="x",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0a0"),
        tenant_role=TenantRole.READ_ONLY,
    )
    assert op.capabilities == frozenset()
