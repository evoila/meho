# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A hosted agent's connector list also limits targets and operation search.

With a ``connectors`` list on the agent's toolset:

* ``list_targets`` shows only the targets whose connector is in the list. A
  target that picks no single connector is left out;
* the near-match targets of a ``no_target`` answer and the hits of an
  ``ambiguous_target`` answer name only targets of connectors in the list.
  The target resolver builds both, so every tool that looks a target up by
  name gives the same answer;
* ``list_operation_groups`` and ``search_operations`` refuse a
  ``connector_id`` outside the list before the handler runs, in both forms
  of the operation tools.

Without a list every answer stays the same. The connectors, the products
and the targets here are made up.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic_ai import RunContext
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RunUsage

import meho_backplane.agent.toolset as toolset_mod
from meho_backplane.agent.tool_audit import ToolRefusedRetry
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.db.models import Tenant
from meho_backplane.operations import register_typed_operation, reset_dispatcher_caches
from meho_backplane.operations._target_connector_limit import connector_limit
from meho_backplane.operations.deny_reasons import CONNECTOR_NOT_ALLOWED
from meho_backplane.settings import get_settings
from meho_backplane.targets.resolver import (
    AmbiguousTargetError,
    TargetNotFoundError,
    resolve_target,
)
from tests.mcp_test_fixtures import (
    build_operator,
    required_settings_env,  # noqa: F401 -- pytest-discovered autouse fixture
)

_FENCE = "fence-1.0"
_OTHER = "other-1.0"
_ONLY_FENCE = frozenset({_FENCE})
_PERSON = build_operator(TenantRole.OPERATOR, scopes=frozenset())

#: name -> (product, aliases). "nomatch" has no connector at all.
_TARGETS: dict[str, tuple[str, list[str]]] = {
    "lab-fence-a": ("fence", ["shared"]),
    "lab-fence-b": ("fence", ["shared"]),
    "lab-other-a": ("other", ["shared"]),
    "lab-nomatch-a": ("nomatch", []),
}


class _FenceConnector(Connector):
    product = "fence"
    version = "1.0"
    impl_id = "fence"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError


class _OtherConnector(_FenceConnector):
    product = "other"
    version = "1.0"
    impl_id = "other"


@pytest.fixture(autouse=True)
def _registry() -> Iterator[None]:
    reset_dispatcher_caches()
    clear_registry()
    register_connector_v2(product="fence", version="1.0", impl_id="fence", cls=_FenceConnector)
    register_connector_v2(product="other", version="1.0", impl_id="other", cls=_OtherConnector)
    yield
    reset_dispatcher_caches()
    clear_registry()


@pytest.fixture
async def targets() -> None:
    tenant_id = _PERSON.tenant_id
    async with get_sessionmaker()() as session, session.begin():
        if await session.get(Tenant, tenant_id) is None:
            session.add(Tenant(id=tenant_id, slug=f"t-{tenant_id.hex[:8]}", name="t"))
        for name, (product, aliases) in _TARGETS.items():
            session.add(
                TargetORM(
                    tenant_id=tenant_id,
                    name=name,
                    aliases=aliases,
                    product=product,
                    host=f"{name}.example.test",
                    port=443,
                    secret_ref="targets/fixture",
                    auth_model="shared_service_account",
                )
            )


def _agent() -> Operator:
    return _PERSON.model_copy(
        update={"sub": "agent:limited", "principal_kind": PrincipalKind.AGENT}
    )


def _unused_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    return ModelResponse(parts=[TextPart("unused")])


def _ctx() -> RunContext[Operator]:
    return RunContext(deps=_agent(), model=FunctionModel(_unused_model), usage=RunUsage())


def _tool(name: str, connectors: list[str] | None) -> Any:
    spec: dict[str, Any] = {"meta_tools": [name]}
    if connectors is not None:
        spec["connectors"] = connectors
    tools = {t.name: t for t in toolset_mod.resolve_agent_tools(spec, _agent())}
    return tools[name]


def _names(rows: list[dict[str, Any]]) -> list[str]:
    return sorted(row["name"] for row in rows)


# --- list_targets ------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("targets")
async def test_list_targets_shows_only_targets_of_connectors_in_the_list() -> None:
    limited = await _tool("list_targets", [_FENCE]).function(_ctx())
    assert _names(limited["targets"]) == ["lab-fence-a", "lab-fence-b"]
    both = await _tool("list_targets", [_FENCE, _OTHER]).function(_ctx())
    assert _names(both["targets"]) == ["lab-fence-a", "lab-fence-b", "lab-other-a"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("targets")
async def test_list_targets_with_an_empty_list_shows_no_target() -> None:
    answer = await _tool("list_targets", []).function(_ctx())
    assert answer["targets"] == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("targets")
async def test_list_targets_filter_on_a_connector_outside_the_list_shows_no_target() -> None:
    answer = await _tool("list_targets", [_FENCE]).function(_ctx(), connector_id=_OTHER)
    assert answer["targets"] == []


async def _pages(connectors: list[str] | None, limit: int) -> list[tuple[list[str], str | None]]:
    """Every page of ``list_targets`` from the start: (names, next_cursor)."""
    tool = _tool("list_targets", connectors)
    pages: list[tuple[list[str], str | None]] = []
    cursor: str | None = None
    while True:
        args: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            args["cursor"] = cursor
        answer = await tool.function(_ctx(), **args)
        pages.append(([t["name"] for t in answer["targets"]], answer["next_cursor"]))
        cursor = answer["next_cursor"]
        if cursor is None:
            return pages


@pytest.mark.asyncio
@pytest.mark.usefixtures("targets")
async def test_list_targets_pages_hold_and_name_only_targets_in_the_list() -> None:
    # The targets sort as lab-fence-a, lab-fence-b, lab-nomatch-a,
    # lab-other-a: the first two rows are outside the list.
    assert await _pages([_OTHER], 1) == [(["lab-other-a"], "lab-other-a"), ([], None)]
    # Here the rows outside the list come last, after a full page.
    assert await _pages([_FENCE], 1) == [
        (["lab-fence-a"], "lab-fence-a"),
        (["lab-fence-b"], "lab-fence-b"),
        ([], None),
    ]
    assert await _pages([_FENCE], 3) == [(["lab-fence-a", "lab-fence-b"], None)]


@pytest.mark.asyncio
@pytest.mark.usefixtures("targets")
async def test_list_targets_pages_with_an_empty_list_name_no_target() -> None:
    assert await _pages([], 1) == [([], None)]


@pytest.mark.asyncio
@pytest.mark.usefixtures("targets")
async def test_list_targets_pages_without_a_list_keep_the_row_cursor() -> None:
    assert await _pages(None, 2) == [
        (["lab-fence-a", "lab-fence-b"], "lab-fence-b"),
        (["lab-nomatch-a", "lab-other-a"], "lab-other-a"),
        ([], None),
    ]


@pytest.mark.asyncio
@pytest.mark.usefixtures("targets")
async def test_list_targets_without_a_list_shows_every_target() -> None:
    answer = await _tool("list_targets", None).function(_ctx())
    assert _names(answer["targets"]) == sorted(_TARGETS)


# --- no_target and ambiguous_target near-matches ------------------------------


async def _error_matches(query: str) -> tuple[str, list[str]]:
    async with get_sessionmaker()() as session:
        try:
            await resolve_target(session, _PERSON.tenant_id, query)
        except (TargetNotFoundError, AmbiguousTargetError) as exc:
            detail: dict[str, Any] = exc.detail  # type: ignore[assignment]
            return detail["error"], _names(detail["matches"])
    raise AssertionError(f"{query!r} resolved")


@pytest.mark.asyncio
@pytest.mark.usefixtures("targets")
async def test_resolver_answers_name_only_targets_in_the_list() -> None:
    assert await _error_matches("lab-") == ("no_target", sorted(_TARGETS))
    assert await _error_matches("shared") == (
        "ambiguous_target",
        ["lab-fence-a", "lab-fence-b", "lab-other-a"],
    )
    with connector_limit(_ONLY_FENCE):
        assert await _error_matches("lab-") == ("no_target", ["lab-fence-a", "lab-fence-b"])
        assert await _error_matches("shared") == (
            "ambiguous_target",
            ["lab-fence-a", "lab-fence-b"],
        )
    with connector_limit(frozenset()):
        assert await _error_matches("lab-") == ("no_target", [])
        assert await _error_matches("shared") == ("ambiguous_target", [])


@pytest.mark.asyncio
@pytest.mark.usefixtures("targets")
async def test_resolver_finds_a_target_outside_the_list_by_its_exact_name() -> None:
    """The lookup does not change: the dispatcher refuses such a target later."""
    with connector_limit(_ONLY_FENCE):
        async with get_sessionmaker()() as session:
            found = await resolve_target(session, _PERSON.tenant_id, "lab-other-a")
    assert found.name == "lab-other-a"


@pytest.mark.asyncio
@pytest.mark.usefixtures("targets")
@pytest.mark.parametrize(
    ("query", "code"),
    [("lab-", "no_target"), ("shared", "ambiguous_target")],
)
@pytest.mark.parametrize("tool", ["call_operation", "preview_operation"])
async def test_hosted_target_answers_name_only_targets_in_the_list(
    tool: str, query: str, code: str
) -> None:
    async def matches(connectors: list[str] | None) -> list[str]:
        answer = await _tool(tool, connectors).function(
            _ctx(), connector_id=_FENCE, op_id="fence.about", target={"name": query}
        )
        assert answer["status"] == "error"
        assert answer["extras"]["error_code"] == code
        return _names(answer["extras"]["matches"])

    limited = await matches([_FENCE])
    assert limited == ["lab-fence-a", "lab-fence-b"]
    without = await matches(None)
    assert "lab-other-a" in without


# --- list_operation_groups and search_operations -----------------------------


async def _about(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    del target, params
    return {}


async def _register_op(product: str) -> None:
    embedding = AsyncMock()
    embedding.encode_one.return_value = [0.1] * 384
    embedding.encode.return_value = [[0.1] * 384]
    embedding.dimension = 384
    await register_typed_operation(
        product=product,
        version="1.0",
        impl_id=product,
        op_id=f"{product}.about",
        handler=_about,
        summary="About the system.",
        description="Shows what the system is.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        safety_level="safe",
        requires_approval=False,
        embedding_service=embedding,
    )


_DISCOVERY_CALLS: list[tuple[str, dict[str, Any]]] = [
    ("search_operations", {"query": "about"}),
    ("list_operation_groups", {}),
]


@pytest.fixture(params=["false", "true"], ids=["hand-written", "from-mcp"])
def operation_tools_form(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_OPERATION_TOOLS_FROM_MCP", request.param)
    get_settings.cache_clear()


@pytest.mark.asyncio
@pytest.mark.usefixtures("operation_tools_form")
@pytest.mark.parametrize(("tool", "arguments"), _DISCOVERY_CALLS)
async def test_discovery_of_a_connector_outside_the_list_is_refused(
    tool: str, arguments: dict[str, Any]
) -> None:
    await _register_op("other")
    for connectors in ([_FENCE], []):
        with pytest.raises(ToolRefusedRetry) as refused:
            await _tool(tool, connectors).function(_ctx(), connector_id=_OTHER, **arguments)
        assert refused.value.reason_code == CONNECTOR_NOT_ALLOWED
        assert _OTHER not in str(refused.value)


@pytest.mark.asyncio
@pytest.mark.usefixtures("operation_tools_form")
@pytest.mark.parametrize(("tool", "arguments"), _DISCOVERY_CALLS)
async def test_discovery_of_a_connector_in_the_list_or_without_a_list_runs(
    tool: str, arguments: dict[str, Any]
) -> None:
    await _register_op("fence")
    await _register_op("other")
    in_list = await _tool(tool, [_FENCE]).function(_ctx(), connector_id=_FENCE, **arguments)
    without = await _tool(tool, None).function(_ctx(), connector_id=_OTHER, **arguments)
    for answer in (in_list, without):
        assert "hits" in answer or "groups" in answer
