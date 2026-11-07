# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Hosted agents get the MCP working surface, or a written reason (#5852).

* Every one of the 26 working-surface MCP tools is either in the hosted
  catalog or in the "left out" list, and the table in
  ``docs/codebase/agent-runtime.md`` names each one with the same answer.
* The five tools added here (``add_to_memory``, ``add_to_knowledge``,
  ``list_doc_collections``, ``search_docs``, ``read_docs``) are opt-in, keep
  the MCP schema, role floor and capability, and register only when the
  tenant has the capability.
* Each added tool runs in a real hosted run under the agent's own role (a
  ``tenant_admin`` caller is narrowed to ``operator``) and writes an audit
  row keyed by the run id.
* The MCP handler's own checks run on that role: an agent cannot write
  ``tenant`` Memory or set curated knowledge metadata, even for a
  ``tenant_admin`` caller.
* A docs backend outage reaches the model as a failed tool result, not a
  failed run.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import select

from meho_backplane.agent import tool_audit, toolset
from meho_backplane.agent.invocation import AgentInvoker
from meho_backplane.agent.run import PydanticAgentRun
from meho_backplane.agent.toolset import (
    META_TOOL_NAMES,
    OPT_IN_META_TOOL_NAMES,
    resolve_agent_toolset,
)
from meho_backplane.agent.toolset_discovery import DOCS_META_TOOL_NAMES, WRITE_META_TOOL_NAMES
from meho_backplane.auth.corpus import CorpusUnavailable
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentRunStatus, AuditLog
from meho_backplane.docs_search import (
    CollectionNotReadyError,
    DocsReadResult,
    DocsSearchResult,
)
from meho_backplane.mcp.registry import (
    ToolSurface,
    eager_import_mcp_modules,
    get_tool,
    registered_tool_names,
)
from meho_backplane.memory.schemas import MemoryScope
from meho_backplane.memory.service import MemoryService
from meho_backplane.settings import get_settings
from tests._kb_embedding_stub import fake_embedding_service
from tests.test_agent_invocation import _make_operator, _seed_definition

_ADDED_TOOLS: tuple[str, ...] = (*WRITE_META_TOOL_NAMES, *DOCS_META_TOOL_NAMES)

#: Working-surface MCP tools a hosted run does not get. The reason for each
#: is in the table in ``docs/codebase/agent-runtime.md``.
_LEFT_OUT: frozenset[str] = frozenset(
    {
        "ask_docs",
        "meho_runbook_start",
        "meho_runbook_next",
        "meho_runbook_abort",
        "meho_runbook_list_runs",
        "meho_runbook_list_templates",
        "meho_runbook_show_template",
    }
)

#: MCP name -> hosted name, where the two differ.
_HOSTED_NAME: dict[str, str] = {
    "meho_broadcast_announce": "broadcast_announce",
    "meho_broadcast_recent": "broadcast_recent",
    "meho_broadcast_watch": "broadcast_watch",
}

_DOCS_CAPABILITY = frozenset({"meho-docs"})

_AGENT_DOC = Path(__file__).resolve().parents[2] / "docs" / "codebase" / "agent-runtime.md"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def _stub_embedding() -> Iterator[None]:
    with patch(
        "meho_backplane.retrieval.indexer.get_embedding_service",
        return_value=fake_embedding_service(),
    ):
        yield


def _admin_caller() -> Operator:
    """A ``tenant_admin`` caller with the docs add-on. The run narrows it to ``operator``."""
    return _make_operator(role=TenantRole.TENANT_ADMIN, sub="admin-caller").model_copy(
        update={"capabilities": _DOCS_CAPABILITY}
    )


def _scripted(*steps: ToolCallPart) -> FunctionModel:
    """A model that makes one tool call per turn from *steps*, then answers."""
    remaining = list(steps)

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if remaining:
            return ModelResponse(parts=[remaining.pop(0)])
        return ModelResponse(parts=[TextPart("done")])

    return FunctionModel(fn)


async def _run(toolset_spec: dict[str, Any], *steps: ToolCallPart) -> Any:
    await _seed_definition(toolset=toolset_spec, turn_budget=6)
    model = _scripted(*steps)
    invoker = AgentInvoker(runtime=PydanticAgentRun(model_factory=lambda: model))
    return await invoker.run(_admin_caller(), "reader", "go")


async def _tool_rows(run_id: UUID, name: str) -> list[AuditLog]:
    async with get_sessionmaker()() as session:
        result = await session.execute(
            select(AuditLog)
            .where(AuditLog.agent_session_id == run_id)
            .where(AuditLog.path == f"/agent/tools/call/{name}")
            .order_by(AuditLog.occurred_at)
        )
        return list(result.scalars())


def _record_roles(monkeypatch: pytest.MonkeyPatch, name: str) -> list[TenantRole]:
    """Wrap the catalog handler of *name* so a test sees the role each call ran with.

    The wrapped handler is the governed MCP bridge, so the real gates and
    the real MCP handler still run.
    """
    spec = next(s for s in toolset._META_TOOL_CATALOG if s.name == name)
    inner: Callable[[Operator, dict[str, Any]], Awaitable[dict[str, Any]]] = spec.handler
    roles: list[TenantRole] = []

    async def recording(operator: Operator, arguments: dict[str, Any]) -> dict[str, Any]:
        roles.append(operator.tenant_role)
        return await inner(operator, arguments)

    monkeypatch.setattr(spec, "handler", recording)
    return roles


# ---------------------------------------------------------------------------
# The 26 tools: hosted, or a written reason
# ---------------------------------------------------------------------------


def _working_surface() -> set[str]:
    """The 26 working-surface tools. Add-on tools are not part of it."""
    eager_import_mcp_modules()
    names = set()
    for name in registered_tool_names():
        entry = get_tool(name)
        assert entry is not None
        definition = entry[0]
        if definition.surface is ToolSurface.WORKING and definition.required_addon_family is None:
            names.add(name)
    return names


def _doc_table() -> dict[str, str]:
    """``{MCP tool name: "yes" | "no"}`` from the parity table in the doc."""
    text = _AGENT_DOC.read_text(encoding="utf-8")
    section = text.split("### Hosted tools compared with the MCP working surface (#5852)", 1)[1]
    section = section.split("\n### ", 1)[0]
    rows = re.findall(r"^\| `([a-z_]+)` \| (yes|no)\b", section, flags=re.MULTILINE)
    return dict(rows)


def test_every_working_surface_tool_is_hosted_or_left_out() -> None:
    working = _working_surface()
    assert len(working) == 26
    hosted = {name for name in working if _HOSTED_NAME.get(name, name) in META_TOOL_NAMES}
    assert working - hosted == _LEFT_OUT


def test_doc_table_names_each_working_surface_tool_once() -> None:
    table = _doc_table()
    assert set(table) == _working_surface()
    assert {name for name, hosted in table.items() if hosted == "no"} == _LEFT_OUT


def test_no_runbook_tool_in_the_catalog() -> None:
    """Runbook steps need a person to confirm them (#2077)."""
    assert not any("runbook" in name for name in META_TOOL_NAMES)
    assert "ask_docs" not in META_TOOL_NAMES


# ---------------------------------------------------------------------------
# Registration: opt-in, MCP schema, role floor, capability
# ---------------------------------------------------------------------------


def test_added_tools_are_opt_in() -> None:
    caller = _admin_caller()
    assert set(_ADDED_TOOLS) <= OPT_IN_META_TOOL_NAMES
    default = resolve_agent_toolset({}, caller)
    assert not {t.name for t in default.tools} & set(_ADDED_TOOLS)
    named = resolve_agent_toolset({"meta_tools": list(_ADDED_TOOLS)}, caller)
    assert [t.name for t in named.tools] == list(_ADDED_TOOLS)


@pytest.mark.parametrize("name", _ADDED_TOOLS)
def test_added_tool_keeps_mcp_schema_role_and_capability(name: str) -> None:
    entry = get_tool(name)
    assert entry is not None
    definition, _handler = entry
    assert definition.surface is ToolSurface.WORKING
    (spec,) = [m for m in toolset._META_TOOL_CATALOG if m.name == name]
    assert spec.parameter_schema == definition.to_wire()["inputSchema"]
    assert spec.required_role == definition.required_role
    assert spec.required_capability == definition.required_capability
    assert spec.description.startswith(definition.description)


def test_docs_tools_need_the_docs_capability() -> None:
    without = _make_operator()
    resolution = resolve_agent_toolset({"meta_tools": list(_ADDED_TOOLS)}, without)
    assert [t.name for t in resolution.tools] == list(WRITE_META_TOOL_NAMES)
    for name in DOCS_META_TOOL_NAMES:
        assert resolution.disabled[name] == "capability_missing"


def test_read_only_role_gets_no_added_tool() -> None:
    read_only = _make_operator(role=TenantRole.READ_ONLY, sub="op-read-only").model_copy(
        update={"capabilities": _DOCS_CAPABILITY}
    )
    resolution = resolve_agent_toolset({"meta_tools": list(_ADDED_TOOLS)}, read_only)
    assert resolution.tools == []
    for name in _ADDED_TOOLS:
        assert resolution.disabled[name] == "role_below_floor"


def test_search_docs_hosted_description_points_at_read_docs() -> None:
    (spec,) = [m for m in toolset._META_TOOL_CATALOG if m.name == "search_docs"]
    assert "In this hosted run there is no `resources/read`" in spec.description


# ---------------------------------------------------------------------------
# Real hosted runs: the agent's own role, and an audit row per call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_embedding")
async def test_add_to_memory_runs_as_the_agent_and_is_audited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roles = _record_roles(monkeypatch, "add_to_memory")
    outcome = await _run(
        {"meta_tools": ["add_to_memory"]},
        # Tenant scope needs tenant_admin. The caller is one, the agent is not.
        ToolCallPart("add_to_memory", {"scope": "tenant", "body": "shared note"}),
        ToolCallPart(
            "add_to_memory", {"scope": "user-tenant", "body": "my note", "slug": "agent-note"}
        ),
    )

    assert outcome.status is AgentRunStatus.SUCCEEDED
    assert roles == [TenantRole.OPERATOR, TenantRole.OPERATOR]
    rows = await _tool_rows(outcome.run_id, "add_to_memory")
    assert [r.status_code for r in rows] == [400, 200]
    assert all(r.method == tool_audit.AGENT_TOOL_AUDIT_METHOD for r in rows)
    assert all(r.operator_sub == "admin-caller" for r in rows)
    assert all(r.actor_sub == "agent:reader" for r in rows)
    assert all(r.payload["op_class"] == "write" for r in rows)
    # Arguments are hashed, never stored.
    assert all("my note" not in str(r.payload) for r in rows)

    reader = _make_operator(sub="admin-caller")
    assert await MemoryService().list_memories(reader, scope=MemoryScope.TENANT) == []
    saved = await MemoryService().list_memories(reader, scope=MemoryScope.USER_TENANT)
    assert [e.slug for e in saved] == ["agent-note"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_embedding")
async def test_add_to_knowledge_runs_as_the_agent_and_is_audited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roles = _record_roles(monkeypatch, "add_to_knowledge")
    outcome = await _run(
        {"meta_tools": ["add_to_knowledge"]},
        # Only a tenant admin may mark an entry as curated.
        ToolCallPart(
            "add_to_knowledge",
            {"slug": "vault-login", "body": "use oidc", "metadata": {"source_kind": "curated"}},
        ),
        ToolCallPart("add_to_knowledge", {"slug": "vault-login", "body": "use oidc"}),
    )

    assert outcome.status is AgentRunStatus.SUCCEEDED
    assert roles == [TenantRole.OPERATOR, TenantRole.OPERATOR]
    rows = await _tool_rows(outcome.run_id, "add_to_knowledge")
    assert [r.status_code for r in rows] == [400, 200]
    assert all(r.operator_sub == "admin-caller" for r in rows)
    assert all(r.payload["op_class"] == "write" for r in rows)


_COLLECTION = "vendor-docs"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("list_doc_collections", {}),
        ("search_docs", {"query": "snapshot depth", "collection": _COLLECTION}),
        ("read_docs", {"collection": _COLLECTION, "read_handle": "opaque-handle"}),
    ],
)
async def test_docs_tool_runs_as_the_agent_and_is_audited(
    monkeypatch: pytest.MonkeyPatch, name: str, arguments: dict[str, Any]
) -> None:
    roles = _record_roles(monkeypatch, name)
    docs_module = "meho_backplane.mcp.tools.docs"
    with (
        patch(f"{docs_module}._resolve_collection_or_error", AsyncMock(return_value=object())),
        patch(f"{docs_module}.search_docs", AsyncMock(return_value=DocsSearchResult())),
        patch(f"{docs_module}.resolve_readable_collection", AsyncMock(return_value=object())),
        patch(
            f"{docs_module}.read_docs",
            AsyncMock(return_value=DocsReadResult(mode="around", text="x", disclosure="full")),
        ),
    ):
        outcome = await _run({"meta_tools": [name]}, ToolCallPart(name, arguments))

    assert outcome.status is AgentRunStatus.SUCCEEDED
    assert roles == [TenantRole.OPERATOR]
    rows = await _tool_rows(outcome.run_id, name)
    assert [r.status_code for r in rows] == [200]
    assert rows[0].operator_sub == "admin-caller"
    assert rows[0].payload["op_class"] == "read"
    assert "opaque-handle" not in str(rows[0].payload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        CorpusUnavailable("corpus unreachable: ConnectError"),
        CollectionNotReadyError(_COLLECTION, "rebuilding"),
    ],
)
async def test_docs_backend_down_fails_the_call_not_the_run(error: Exception) -> None:
    docs_module = "meho_backplane.mcp.tools.docs"
    down = AsyncMock(side_effect=error)
    with (
        patch(f"{docs_module}._resolve_collection_or_error", AsyncMock(return_value=object())),
        patch(f"{docs_module}.search_docs", down),
    ):
        outcome = await _run(
            {"meta_tools": ["search_docs"]},
            ToolCallPart("search_docs", {"query": "x", "collection": _COLLECTION}),
        )

    assert outcome.status is AgentRunStatus.SUCCEEDED
    rows = await _tool_rows(outcome.run_id, "search_docs")
    # 500, like the MCP internal error for the same outage.
    assert [(r.status_code, r.payload["error_type"]) for r in rows] == [(500, "ToolFailed")]
