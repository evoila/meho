# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the claim-driven MCP agent-surface filter (Initiative #3153, #3154).

The surface gate is the third axis, orthogonal to the role gate
(:func:`role_at_least`) and the tenant-capability gate
(:func:`capability_satisfied`) and AND-composed with both. Every
registered tool carries an explicit
:class:`~meho_backplane.mcp.registry.ToolSurface`
(``working`` | ``operator``); a registration that omits it fails at
construction — i.e. at module-import time. ``tools/list`` filters by the
session's OAuth scopes: the default working surface is always listed, and
the operator planes are additionally listed only when the session carries
``mcp:admin`` (:data:`~meho_backplane.mcp.registry.MCP_ADMIN_SCOPE`).
``tools/call`` re-checks the same gate so knowing a tool's name can't
bypass the elevation requirement.

Acceptance criteria covered (issue #3154):

* AC1 — every registered tool carries an explicit surface; a
  registration without one fails at construction (no silent default).
* AC2 — ``all_tools_for`` for a non-elevated session returns exactly the
  working surface (the full sorted list is pinned); an ``mcp:admin``
  session additionally lists the operator planes (the full sorted list is
  pinned).
* AC3 — the docs capability gate AND-composes with the surface filter:
  an elevated session without ``meho-docs`` still doesn't see the docs
  tools.
* AC4 — ``tools/call`` on an operator-surface tool is rejected for a
  non-elevated session (403-class, handler never runs) and dispatches
  once the session is elevated — the listing filter is enforced at call
  time, not cosmetic.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from pydantic import ValidationError

from meho_backplane.auth.jwt import _extract_scopes
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.mcp import ToolDefinition, ToolSurface, register_mcp_tool
from meho_backplane.mcp.handlers import handle_tools_call, handle_tools_list
from meho_backplane.mcp.registry import (
    MCP_ADMIN_SCOPE,
    all_tools_for,
    surface_visible,
)
from meho_backplane.mcp.server import McpInvalidParamsError
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

_DOCS_CAPABILITY = "meho-docs"

#: The three working-surface docs tools — visible only to a session that
#: ALSO holds the ``meho-docs`` capability. These are the docs entries that
#: drop out of the *default* working surface when the capability is absent.
_DOCS_WORKING_TOOLS: frozenset[str] = frozenset({"search_docs", "ask_docs", "list_doc_collections"})

#: Every ``meho-docs``-capability-gated tool across BOTH surfaces: the three
#: working docs tools plus the two operator-surface doc-collection lifecycle
#: tools. These all drop out when the capability is absent, regardless of
#: elevation — so an elevated session without ``meho-docs`` loses all five.
_DOCS_CAP_GATED: frozenset[str] = _DOCS_WORKING_TOOLS | {
    "create_doc_collections",
    "delete_doc_collections",
}

#: The default working surface: the full, exact set every session lists
#: (do-work + coordinate). Pinned so any drift — a new tool that forgets
#: to classify, or a reclassification — fails here loudly (Initiative
#: #3153 definition of done).
_WORKING_SURFACE: frozenset[str] = frozenset(
    {
        "add_to_knowledge",
        "add_to_memory",
        "ask_docs",
        "call_operation",
        "list_doc_collections",
        "list_operation_groups",
        "list_targets",
        "meho_broadcast_announce",
        "meho_broadcast_recent",
        "meho_broadcast_watch",
        "meho_connector_list",
        "meho_runbook_abort",
        "meho_runbook_list_runs",
        "meho_runbook_list_templates",
        "meho_runbook_next",
        "meho_runbook_show_template",
        "meho_runbook_start",
        "meho_status",
        "preview_operation",
        "query_topology",
        "result_query",
        "search_docs",
        "search_knowledge",
        "search_memory",
        "search_operations",
    }
)

#: The operator planes an ``mcp:admin``-elevated session additionally
#: lists (governance + lifecycle). Pinned for the same reason.
_OPERATOR_SURFACE: frozenset[str] = frozenset(
    {
        "create_doc_collections",
        "delete_doc_collections",
        "meho_agent_principals_list",
        "meho_agent_principals_register",
        "meho_agent_principals_revoke",
        "meho_agents_create",
        "meho_agents_delete",
        "meho_agents_edit",
        "meho_agents_grant_create",
        "meho_agents_grant_list",
        "meho_agents_grant_revoke",
        "meho_agents_grant_show",
        "meho_agents_list",
        "meho_agents_list_runs",
        "meho_agents_run",
        "meho_agents_run_status",
        "meho_agents_show",
        "meho_approvals_get",
        "meho_approvals_list",
        "meho_audit_replay",
        "meho_broadcast_overrides_list",
        "meho_broadcast_overrides_remove",
        "meho_broadcast_overrides_set",
        "meho_connector_delete",
        "meho_connector_disable",
        "meho_connector_edit_group",
        "meho_connector_edit_op",
        "meho_connector_enable",
        "meho_connector_enable_reads",
        "meho_connector_ingest",
        "meho_connector_ingest_status",
        "meho_connector_review",
        "meho_memory_promote",
        "meho_runbook_deprecate_template",
        "meho_runbook_discard_template",
        "meho_runbook_draft_template",
        "meho_runbook_edit_template",
        "meho_runbook_publish_template",
        "meho_runbook_reassign",
        "meho_scheduler_cancel",
        "meho_scheduler_create",
        "meho_scheduler_list",
        "meho_sensor_create",
        "meho_sensor_delete",
        "meho_sensor_list",
        "meho_sensor_results",
        "meho_targets_register",
        "meho_topology_annotate",
        "meho_topology_bulk_import",
        "meho_topology_create_node",
        "meho_topology_delete_node",
        "meho_topology_unannotate",
        "query_audit",
    }
)


def _operator(
    *,
    role: TenantRole = TenantRole.TENANT_ADMIN,
    capabilities: frozenset[str] = frozenset(),
    scopes: frozenset[str] = frozenset(),
) -> Operator:
    """Build a fixture operator with the requested role / capability / scope."""
    return Operator(
        sub="op-test",
        name="Test",
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=OPERATOR_TENANT_ID,
        tenant_role=role,
        capabilities=capabilities,
        scopes=scopes,
    )


# ---------------------------------------------------------------------------
# AC1 — every tool is classified; omission fails at construction
# ---------------------------------------------------------------------------


def test_tooldefinition_surface_is_required_no_default() -> None:
    """A ToolDefinition without ``surface`` raises — no silent default bucket.

    The registry is populated at module-import time, so this
    construction-time failure is what makes an unclassified registration
    fail app boot / test collection loudly rather than defaulting into a
    surface silently.
    """
    with pytest.raises(ValidationError) as exc:
        ToolDefinition(
            feature=None,
            name="unclassified.tool",
            description="No surface declared",
            inputSchema={"type": "object", "properties": {}},
        )
    # The missing field is precisely ``surface`` (not some other required arg).
    assert any(
        err["loc"] == ("surface",) and err["type"] == "missing" for err in exc.value.errors()
    )


def test_every_registered_tool_carries_a_surface() -> None:
    """Every production tool resolves to a known ToolSurface member."""
    from meho_backplane.mcp import registry as mcp_registry

    for name, (defn, _handler) in mcp_registry._TOOLS.items():
        assert isinstance(defn.surface, ToolSurface), name


def test_surface_partition_is_exhaustive_and_disjoint() -> None:
    """The pinned working / operator sets partition the whole registry."""
    from meho_backplane.mcp import registry as mcp_registry

    assert _WORKING_SURFACE.isdisjoint(_OPERATOR_SURFACE)
    assert set(mcp_registry._TOOLS) == _WORKING_SURFACE | _OPERATOR_SURFACE


def test_surface_dropped_from_wire_shape() -> None:
    """``to_wire`` never leaks the MEHO-internal ``surface`` field."""
    defn = ToolDefinition(
        feature=None,
        name="wire.probe",
        description="probe",
        inputSchema={"type": "object", "properties": {}},
        surface=ToolSurface.OPERATOR,
    )
    wire = defn.to_wire()
    assert "surface" not in wire
    assert defn.surface is ToolSurface.OPERATOR  # still readable server-side


# ---------------------------------------------------------------------------
# surface_visible — the shared gate predicate
# ---------------------------------------------------------------------------


def test_surface_visible_working_always_admits() -> None:
    """The working surface is visible to every session, elevated or not."""
    assert surface_visible(_operator(), ToolSurface.WORKING) is True
    assert (
        surface_visible(_operator(scopes=frozenset({MCP_ADMIN_SCOPE})), ToolSurface.WORKING) is True
    )


def test_surface_visible_operator_requires_elevation() -> None:
    """The operator surface is visible only to an ``mcp:admin`` session."""
    assert surface_visible(_operator(), ToolSurface.OPERATOR) is False
    assert surface_visible(_operator(scopes=frozenset({"other"})), ToolSurface.OPERATOR) is False
    elevated = _operator(scopes=frozenset({MCP_ADMIN_SCOPE}))
    assert surface_visible(elevated, ToolSurface.OPERATOR) is True


# ---------------------------------------------------------------------------
# AC2 — tools/list pins: default working surface, mcp:admin adds operator
# ---------------------------------------------------------------------------


def test_default_session_lists_exactly_the_working_surface() -> None:
    """AC2: a non-elevated session lists exactly the working surface.

    Role is held at ``tenant_admin`` and the ``meho-docs`` capability is
    granted, so the only thing gating the operator planes out is the
    absence of the ``mcp:admin`` scope — isolating the surface filter as
    the sole cause.
    """
    op = _operator(role=TenantRole.TENANT_ADMIN, capabilities=frozenset({_DOCS_CAPABILITY}))
    names = {defn.name for defn in all_tools_for(op)}
    assert names == _WORKING_SURFACE


def test_elevated_session_lists_working_plus_operator() -> None:
    """AC2: an ``mcp:admin`` session additionally lists the operator planes."""
    op = _operator(
        role=TenantRole.TENANT_ADMIN,
        capabilities=frozenset({_DOCS_CAPABILITY}),
        scopes=frozenset({MCP_ADMIN_SCOPE}),
    )
    names = {defn.name for defn in all_tools_for(op)}
    assert names == _WORKING_SURFACE | _OPERATOR_SURFACE


# ---------------------------------------------------------------------------
# AC3 — the docs capability gate AND-composes with the surface filter
# ---------------------------------------------------------------------------


def test_docs_capability_composes_with_surface_when_elevated() -> None:
    """AC3: an elevated session without ``meho-docs`` still can't see docs tools.

    Both gates apply independently: the ``mcp:admin`` scope opens the
    operator planes, but the ``meho-docs`` capability gate still hides
    every docs tool across both surfaces (the three working docs tools
    plus the two operator-surface doc-collection lifecycle tools).
    """
    op = _operator(role=TenantRole.TENANT_ADMIN, scopes=frozenset({MCP_ADMIN_SCOPE}))
    names = {defn.name for defn in all_tools_for(op)}
    assert names == (_WORKING_SURFACE | _OPERATOR_SURFACE) - _DOCS_CAP_GATED
    assert names.isdisjoint(_DOCS_CAP_GATED)


def test_docs_capability_composes_with_surface_when_default() -> None:
    """AC3: a non-elevated session without ``meho-docs`` sees working minus docs.

    The operator-surface doc-collection lifecycle tools are already hidden
    by the surface gate, so only the three working docs tools drop out.
    """
    op = _operator(role=TenantRole.TENANT_ADMIN)
    names = {defn.name for defn in all_tools_for(op)}
    assert names == _WORKING_SURFACE - _DOCS_WORKING_TOOLS


# ---------------------------------------------------------------------------
# AC4 — call-time + list-time enforcement through the real dispatchers
#
# These drive ``handle_tools_list`` / ``handle_tools_call`` directly (the
# functions the ``/mcp`` JSON-RPC route dispatches to) rather than through a
# ``TestClient``: it exercises the identical enforcement code without paying
# for the FastAPI lifespan (whose embedding-model preload is heavy and
# network-bound). A lone operator-surface stub with ``required_role=READ_ONLY``
# isolates the surface gate — role + capability both pass, so only elevation
# decides visibility.
# ---------------------------------------------------------------------------


_STUB_OP_TOOL = "surface_test.operator_tool"


@pytest.fixture
def operator_stub() -> Iterator[dict[str, int]]:
    """Register a lone operator-surface stub tool; track handler invocations.

    The autouse ``isolated_registry`` fixture re-registers the production
    surface before each test and clears it after, so the stub is layered on
    top for this test only — no explicit teardown needed here.
    """
    handler_calls: dict[str, int] = {"count": 0}

    async def _stub_handler(_op: Operator, _args: dict[str, Any]) -> dict[str, Any]:
        handler_calls["count"] += 1
        return {"ok": True}

    register_mcp_tool(
        ToolDefinition(
            feature=None,
            name=_STUB_OP_TOOL,
            description="Operator-surface stub",
            inputSchema={"type": "object", "properties": {}},
            required_role=TenantRole.READ_ONLY,
            surface=ToolSurface.OPERATOR,
        ),
        _stub_handler,
    )
    yield handler_calls


async def test_tools_list_omits_operator_tool_for_default_session(
    operator_stub: dict[str, int],
) -> None:
    """AC2/AC4: the operator-surface stub is absent from a default listing."""
    result = await handle_tools_list(_operator(role=TenantRole.READ_ONLY), None)
    names = [t["name"] for t in result["tools"]]
    assert _STUB_OP_TOOL not in names


async def test_tools_list_includes_operator_tool_for_elevated_session(
    operator_stub: dict[str, int],
) -> None:
    """AC2: the operator-surface stub appears once the session is elevated."""
    op = _operator(role=TenantRole.READ_ONLY, scopes=frozenset({MCP_ADMIN_SCOPE}))
    result = await handle_tools_list(op, None)
    names = [t["name"] for t in result["tools"]]
    assert _STUB_OP_TOOL in names


async def test_tools_call_operator_tool_rejected_for_default_session(
    operator_stub: dict[str, int],
) -> None:
    """AC4: naming an operator-surface tool directly still 403s when not elevated.

    The rejection is a 403-class surface error (``McpInvalidParamsError``,
    which the JSON-RPC route renders as INVALID_PARAMS) whose message names
    the ``mcp:admin`` elevation — NOT a handler error: the handler must never
    run, so learning the name out-of-band can't bypass the gate.
    """
    op = _operator(role=TenantRole.READ_ONLY)
    with pytest.raises(McpInvalidParamsError) as exc:
        await handle_tools_call(op, {"name": _STUB_OP_TOOL, "arguments": {}})
    message = str(exc.value).lower()
    assert "forbidden" in message
    assert "mcp:admin" in message
    assert operator_stub["count"] == 0


async def test_tools_call_operator_tool_dispatches_for_elevated_session(
    operator_stub: dict[str, int],
) -> None:
    """AC4 (positive): an elevated session reaches the operator-tool handler."""
    op = _operator(role=TenantRole.READ_ONLY, scopes=frozenset({MCP_ADMIN_SCOPE}))
    response = await handle_tools_call(op, {"name": _STUB_OP_TOOL, "arguments": {}})
    assert response["isError"] is False
    assert operator_stub["count"] == 1


# ---------------------------------------------------------------------------
# scope extraction — claims → Operator.scopes (drives the filter)
# ---------------------------------------------------------------------------


class _StubSettings:
    """Minimal settings stand-in carrying only the scopes claim name."""

    def __init__(self, claim_name: str = "scope") -> None:
        self.jwt_scopes_claim_name = claim_name


def test_extract_scopes_space_delimited_string() -> None:
    """The RFC 9068 space-delimited ``scope`` string splits into the set."""
    claims = {"scope": "openid mcp:admin profile"}
    result = _extract_scopes(claims, _StubSettings())  # type: ignore[arg-type]
    assert result == frozenset({"openid", "mcp:admin", "profile"})


def test_extract_scopes_list_claim_tolerated() -> None:
    """A JSON array of scope strings is tolerated (non-standard realms)."""
    claims = {"scope": ["openid", "mcp:admin"]}
    result = _extract_scopes(claims, _StubSettings())  # type: ignore[arg-type]
    assert result == frozenset({"openid", "mcp:admin"})


def test_extract_scopes_absent_claim_is_empty_fail_closed() -> None:
    """An absent claim resolves to the empty set (fail-closed — no elevation)."""
    assert _extract_scopes({}, _StubSettings()) == frozenset()  # type: ignore[arg-type]


def test_extract_scopes_malformed_claim_is_empty_fail_closed() -> None:
    """A non-string, non-array claim (e.g. an object) → empty set."""
    claims = {"scope": {"unexpected": "object"}}
    assert _extract_scopes(claims, _StubSettings()) == frozenset()  # type: ignore[arg-type]


def test_extract_scopes_honours_configured_claim_name() -> None:
    """The claim name is settings-controlled."""
    claims = {"scp": "mcp:admin"}
    result = _extract_scopes(claims, _StubSettings(claim_name="scp"))  # type: ignore[arg-type]
    assert result == frozenset({"mcp:admin"})
    # The default claim name finds nothing in this token.
    assert _extract_scopes(claims, _StubSettings()) == frozenset()  # type: ignore[arg-type]


def test_operator_default_scopes_is_empty_frozenset() -> None:
    """Constructing an Operator without scopes defaults to the empty set."""
    from uuid import UUID

    op = Operator(
        sub="op",
        raw_jwt="x",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0a0"),
        tenant_role=TenantRole.READ_ONLY,
    )
    assert op.scopes == frozenset()
