# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the MCP tool + resource registries (G0.5-T3, #248).

Covers the acceptance criteria on issue #248:

* ``register_mcp_tool`` + ``register_mcp_resource`` make entries listable
  via the corresponding handlers.
* RBAC: ``tools/list`` and ``resources/templates/list`` filter to entries
  the operator's :class:`TenantRole` admits.
* ``tools/call`` dispatches to the registered handler; unknown tool →
  invalid-params; malformed arguments (against the ``inputSchema``) →
  invalid-params.
* ``resources/read`` resolves a concrete URI against the registered
  templates, invokes the handler, and packs the result into the MCP
  ``contents`` shape.
* Duplicate registration raises ``RuntimeError``.
* URI-template matching binds variables correctly and rejects
  non-matching shapes.
* ``eager_import_mcp_modules`` is callable when the subpackages are
  empty (the v0.2 state through T3).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.main import app
from meho_backplane.mcp import (
    ResourceTemplateDefinition,
    ToolDefinition,
    eager_import_mcp_modules,
    register_mcp_resource,
    register_mcp_tool,
)
from meho_backplane.mcp.auth import verify_mcp_jwt_and_bind
from meho_backplane.mcp.registry import (
    _match_uri_template,
    all_resource_templates_for,
    all_tools_for,
    clear_registries,
    get_resource_for_uri,
    get_tool,
)
from meho_backplane.mcp.schemas import INVALID_PARAMS

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _operator(role: TenantRole, sub: str = "op-test") -> Operator:
    """Build a fixture :class:`Operator` with the requested role."""
    return Operator(
        sub=sub,
        name="Test",
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0a0"),
        tenant_role=role,
    )


@pytest.fixture(autouse=True)
def _isolated_registries() -> Iterator[None]:
    """Reset the module-level tool / resource registries around every test."""
    clear_registries()
    yield
    clear_registries()


@pytest.fixture
def client_with_operator(
    request: pytest.FixtureRequest,
) -> Iterator[tuple[TestClient, Operator]]:
    """:class:`TestClient` with ``verify_mcp_jwt_and_bind`` overridden to a fixture.

    Parametrise the operator's :class:`TenantRole` via
    ``@pytest.mark.parametrize("role", [TenantRole.OPERATOR, ...])``;
    the fixture builds the corresponding :class:`Operator` and injects
    it for every dispatch this test makes.
    """
    role: TenantRole = getattr(request, "param", TenantRole.OPERATOR)
    op = _operator(role)

    async def _fake_verify() -> Operator:
        return op

    app.dependency_overrides[verify_mcp_jwt_and_bind] = _fake_verify
    try:
        yield TestClient(app), op
    finally:
        app.dependency_overrides.pop(verify_mcp_jwt_and_bind, None)


def _post_mcp(client: TestClient, body: Any) -> Any:
    """POST a JSON-RPC envelope to ``/mcp`` and return the response."""
    return client.post("/mcp", json=body)


# ---------------------------------------------------------------------------
# Tool registry — registration + lookup
# ---------------------------------------------------------------------------


def test_register_mcp_tool_makes_it_callable_via_get_tool() -> None:
    """``register_mcp_tool`` populates the registry; ``get_tool`` retrieves the entry."""

    async def _stub(_op: Operator, _args: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True}

    defn = ToolDefinition(
        name="test.tool",
        description="A test tool",
        inputSchema={"type": "object", "properties": {}},
    )
    register_mcp_tool(defn, _stub)

    entry = get_tool("test.tool")
    assert entry is not None
    stored_defn, stored_handler = entry
    assert stored_defn.name == "test.tool"
    assert stored_handler is _stub


def test_register_mcp_tool_rejects_duplicate() -> None:
    """Two registrations of the same name → ``RuntimeError``."""

    async def _stub(_op: Operator, _args: dict[str, Any]) -> dict[str, Any]:
        return {}

    defn = ToolDefinition(
        name="dup.tool",
        description="dup",
        inputSchema={"type": "object"},
    )
    register_mcp_tool(defn, _stub)

    with pytest.raises(RuntimeError, match="already registered"):
        register_mcp_tool(defn, _stub)


# ---------------------------------------------------------------------------
# tools/list — RBAC filtering
# ---------------------------------------------------------------------------


def _register_three_tools_at_varying_roles() -> None:
    """Helper: register one tool at each :class:`TenantRole`."""

    async def _stub(_op: Operator, _args: dict[str, Any]) -> dict[str, Any]:
        return {}

    for role, name in (
        (TenantRole.READ_ONLY, "read_only.tool"),
        (TenantRole.OPERATOR, "operator.tool"),
        (TenantRole.TENANT_ADMIN, "admin.tool"),
    ):
        register_mcp_tool(
            ToolDefinition(
                name=name,
                description=f"Tool requiring {role}",
                inputSchema={"type": "object", "properties": {}},
                required_role=role,
            ),
            _stub,
        )


def test_all_tools_for_read_only_operator_returns_only_read_only_tools() -> None:
    """A ``read_only`` operator sees only the ``read_only``-required tool."""
    _register_three_tools_at_varying_roles()
    visible = [t.name for t in all_tools_for(_operator(TenantRole.READ_ONLY))]
    assert visible == ["read_only.tool"]


def test_all_tools_for_operator_returns_read_only_and_operator_tools() -> None:
    """A ``operator``-role operator sees the read_only and operator tools."""
    _register_three_tools_at_varying_roles()
    visible = [t.name for t in all_tools_for(_operator(TenantRole.OPERATOR))]
    assert visible == ["read_only.tool", "operator.tool"]


def test_all_tools_for_tenant_admin_returns_every_tool() -> None:
    """A ``tenant_admin`` operator sees every registered tool."""
    _register_three_tools_at_varying_roles()
    visible = [t.name for t in all_tools_for(_operator(TenantRole.TENANT_ADMIN))]
    assert visible == ["read_only.tool", "operator.tool", "admin.tool"]


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.READ_ONLY],
    indirect=True,
)
def test_tools_list_endpoint_returns_filtered_tools(
    client_with_operator: tuple[TestClient, Operator],
) -> None:
    """End-to-end: ``POST /mcp`` with method=tools/list returns RBAC-filtered list."""
    client, _op = client_with_operator
    _register_three_tools_at_varying_roles()

    response = _post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert response.status_code == 200
    body = response.json()
    tool_names = [t["name"] for t in body["result"]["tools"]]
    assert tool_names == ["read_only.tool"]
    # MEHO-internal fields stripped from the wire shape.
    for entry in body["result"]["tools"]:
        assert "required_role" not in entry
        assert "op_class" not in entry


# ---------------------------------------------------------------------------
# tools/call — dispatch + schema validation + RBAC
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_tools_call_dispatches_to_registered_handler(
    client_with_operator: tuple[TestClient, Operator],
) -> None:
    """``tools/call`` invokes the registered handler with the operator + args."""
    client, op = client_with_operator
    captured: dict[str, Any] = {}

    async def _echo(operator: Operator, arguments: dict[str, Any]) -> dict[str, Any]:
        captured["operator_sub"] = operator.sub
        captured["arguments"] = arguments
        return {"echo": arguments}

    register_mcp_tool(
        ToolDefinition(
            name="test.echo",
            description="Echo arguments back",
            inputSchema={
                "type": "object",
                "properties": {"msg": {"type": "string"}},
                "required": ["msg"],
            },
        ),
        _echo,
    )

    response = _post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "test.echo", "arguments": {"msg": "hello"}},
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == 2
    assert body["result"]["isError"] is False
    # Result body is packed as a single text-content block carrying the
    # JSON-serialised handler return value.
    text = body["result"]["content"][0]["text"]
    assert '"echo"' in text and '"msg"' in text and '"hello"' in text
    assert captured["operator_sub"] == op.sub
    assert captured["arguments"] == {"msg": "hello"}


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_tools_call_unknown_tool_returns_invalid_params(
    client_with_operator: tuple[TestClient, Operator],
) -> None:
    """Calling an unregistered tool → -32602 (per spec example, not -32601)."""
    client, _op = client_with_operator
    response = _post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "no.such.tool", "arguments": {}},
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "unknown tool" in body["error"]["message"].lower()


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_tools_call_malformed_arguments_returns_invalid_params(
    client_with_operator: tuple[TestClient, Operator],
) -> None:
    """``tools/call.arguments`` violating the tool's ``inputSchema`` → -32602."""
    client, _op = client_with_operator

    async def _stub(_op: Operator, _args: dict[str, Any]) -> dict[str, Any]:
        return {}

    register_mcp_tool(
        ToolDefinition(
            name="test.requires_msg",
            description="A tool whose msg arg is required",
            inputSchema={
                "type": "object",
                "properties": {"msg": {"type": "string"}},
                "required": ["msg"],
            },
        ),
        _stub,
    )

    response = _post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "test.requires_msg", "arguments": {}},  # missing msg
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "inputschema" in body["error"]["message"].lower()


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.READ_ONLY],
    indirect=True,
)
def test_tools_call_forbidden_for_under_privileged_operator(
    client_with_operator: tuple[TestClient, Operator],
) -> None:
    """A read_only operator calling a tool that requires operator → -32602 forbidden.

    The RBAC filter on ``tools/list`` already hides the tool from this
    operator; reaching this branch means the client knew the name
    independently. Surface as INVALID_PARAMS with a ``forbidden`` token.
    """
    client, _op = client_with_operator

    async def _stub(_op: Operator, _args: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True}

    register_mcp_tool(
        ToolDefinition(
            name="ops_only.tool",
            description="Operator-only tool",
            inputSchema={"type": "object", "properties": {}},
            required_role=TenantRole.OPERATOR,
        ),
        _stub,
    )

    response = _post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "ops_only.tool", "arguments": {}},
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "forbidden" in body["error"]["message"].lower()


# ---------------------------------------------------------------------------
# Resource template registry + resources/templates/list + resources/read
# ---------------------------------------------------------------------------


def test_register_mcp_resource_makes_it_listable() -> None:
    """``register_mcp_resource`` populates the registry."""

    async def _stub(_op: Operator, _params: dict[str, str]) -> dict[str, Any]:
        return {}

    defn = ResourceTemplateDefinition(
        uriTemplate="meho://test/{id}/info",
        name="test info",
        description="test resource template",
    )
    register_mcp_resource(defn, _stub)

    visible = all_resource_templates_for(_operator(TenantRole.OPERATOR))
    assert [r.uriTemplate for r in visible] == ["meho://test/{id}/info"]


def test_register_mcp_resource_rejects_duplicate() -> None:
    """Two registrations of the same uriTemplate → ``RuntimeError``."""

    async def _stub(_op: Operator, _params: dict[str, str]) -> dict[str, Any]:
        return {}

    defn = ResourceTemplateDefinition(
        uriTemplate="meho://dup/{id}/info",
        name="dup",
        description="dup",
    )
    register_mcp_resource(defn, _stub)

    with pytest.raises(RuntimeError, match="already registered"):
        register_mcp_resource(defn, _stub)


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_resources_templates_list_endpoint_returns_registered_templates(
    client_with_operator: tuple[TestClient, Operator],
) -> None:
    """``resources/templates/list`` returns registered templates in wire shape."""
    client, _op = client_with_operator

    async def _stub(_op: Operator, _params: dict[str, str]) -> dict[str, Any]:
        return {}

    register_mcp_resource(
        ResourceTemplateDefinition(
            uriTemplate="meho://test/{tenant_id}/info",
            name="test info",
            description="A test resource",
            mimeType="application/json",
        ),
        _stub,
    )

    response = _post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 10, "method": "resources/templates/list"},
    )

    assert response.status_code == 200
    body = response.json()
    templates = body["result"]["resourceTemplates"]
    assert len(templates) == 1
    assert templates[0]["uriTemplate"] == "meho://test/{tenant_id}/info"
    assert templates[0]["mimeType"] == "application/json"
    # required_role is MEHO-internal, never on the wire.
    assert "required_role" not in templates[0]


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_resources_list_endpoint_returns_empty_in_v02(
    client_with_operator: tuple[TestClient, Operator],
) -> None:
    """``resources/list`` is spec-conformant but empty in v0.2 (only templates exist)."""
    client, _op = client_with_operator
    response = _post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 11, "method": "resources/list"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["result"]["resources"] == []


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_resources_read_resolves_template_and_invokes_handler(
    client_with_operator: tuple[TestClient, Operator],
) -> None:
    """A concrete URI matches the registered template; handler runs with bound vars."""
    client, _op = client_with_operator
    captured: dict[str, Any] = {}

    async def _handler(
        operator: Operator,
        bound_params: dict[str, str],
    ) -> dict[str, Any]:
        captured["operator_sub"] = operator.sub
        captured["bound_params"] = bound_params
        return {"tenant_id": bound_params["tenant_id"], "kind": "info"}

    register_mcp_resource(
        ResourceTemplateDefinition(
            uriTemplate="meho://tenant/{tenant_id}/info",
            name="tenant info",
            description="Tenant identity",
        ),
        _handler,
    )

    response = _post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 12,
            "method": "resources/read",
            "params": {"uri": "meho://tenant/abc-123/info"},
        },
    )

    assert response.status_code == 200
    body = response.json()
    contents = body["result"]["contents"]
    assert contents[0]["uri"] == "meho://tenant/abc-123/info"
    assert contents[0]["mimeType"] == "application/json"
    assert '"abc-123"' in contents[0]["text"]
    assert captured["bound_params"] == {"tenant_id": "abc-123"}


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.READ_ONLY],
    indirect=True,
)
def test_resources_read_forbidden_for_under_privileged_operator(
    client_with_operator: tuple[TestClient, Operator],
) -> None:
    """A read_only operator reading an OPERATOR-required resource → -32602 forbidden.

    Parallels :func:`test_tools_call_forbidden_for_under_privileged_operator`
    for the resources surface: the URI resolves to a real template, but the
    call-time RBAC re-check rejects the under-privileged operator. Without
    this test the ``resources/read`` RBAC gate would have no end-to-end
    coverage — a regression that silently drops the re-check would pass
    every other test in this file.
    """
    client, _op = client_with_operator

    async def _stub(_op: Operator, _params: dict[str, str]) -> dict[str, Any]:
        return {"ok": True}

    register_mcp_resource(
        ResourceTemplateDefinition(
            uriTemplate="meho://secure/{id}/data",
            name="secure data",
            description="operator-only resource",
            required_role=TenantRole.OPERATOR,
        ),
        _stub,
    )

    response = _post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 14,
            "method": "resources/read",
            "params": {"uri": "meho://secure/abc/data"},
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "forbidden" in body["error"]["message"].lower()


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_resources_read_unknown_uri_returns_invalid_params(
    client_with_operator: tuple[TestClient, Operator],
) -> None:
    """A URI matching no template → INVALID_PARAMS with "resource not found".

    Spec-strict behaviour would be -32002; the current dispatcher maps
    through INVALID_PARAMS (-32602). Adjacent finding tracked in the
    handler docstring + PR description.
    """
    client, _op = client_with_operator
    response = _post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 13,
            "method": "resources/read",
            "params": {"uri": "meho://nope/no/match"},
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "resource not found" in body["error"]["message"].lower()


# ---------------------------------------------------------------------------
# URI-template matching
# ---------------------------------------------------------------------------


def test_match_uri_template_single_var() -> None:
    """``{var}`` captures a single path segment."""
    assert _match_uri_template(
        "meho://tenant/abc/info",
        "meho://tenant/{tenant_id}/info",
    ) == {"tenant_id": "abc"}


def test_match_uri_template_multi_var() -> None:
    """Multiple ``{var}`` placeholders all bind."""
    assert _match_uri_template(
        "meho://target/abc/host-42",
        "meho://target/{tenant_id}/{target_slug}",
    ) == {"tenant_id": "abc", "target_slug": "host-42"}


def test_match_uri_template_no_match_returns_none() -> None:
    """Literal mismatch → ``None``."""
    assert _match_uri_template("meho://kb/notes", "meho://tenant/{id}/info") is None


def test_match_uri_template_var_does_not_cross_path_segment() -> None:
    """``{var}`` doesn't eat across ``/`` — segment-bounded by design."""
    assert _match_uri_template("meho://tenant/a/b/info", "meho://tenant/{id}/info") is None


def test_get_resource_for_uri_returns_template_and_bound_params() -> None:
    """``get_resource_for_uri`` returns the matching template + bound vars."""

    async def _stub(_op: Operator, _params: dict[str, str]) -> dict[str, Any]:
        return {}

    register_mcp_resource(
        ResourceTemplateDefinition(
            uriTemplate="meho://kb/{slug}",
            name="kb",
            description="Knowledge base entry",
        ),
        _stub,
    )

    match = get_resource_for_uri("meho://kb/onboarding")
    assert match is not None
    defn, _handler, params = match
    assert defn.uriTemplate == "meho://kb/{slug}"
    assert params == {"slug": "onboarding"}


def test_get_resource_for_uri_returns_none_when_no_template_matches() -> None:
    """``get_resource_for_uri`` returns ``None`` when no template matches."""
    assert get_resource_for_uri("meho://unmatched/uri") is None


# ---------------------------------------------------------------------------
# Eager-import sweep
# ---------------------------------------------------------------------------


def test_eager_import_mcp_modules_handles_empty_subpackages() -> None:
    """The helper is callable when ``mcp/tools/`` and ``mcp/resources/`` are empty.

    Through G0.5-T3 both subpackages are empty (T4 adds the first tool /
    resource). The lifespan calls this on every startup, so an empty
    package must not raise.
    """
    # Should not raise.
    eager_import_mcp_modules()
