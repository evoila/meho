# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A hosted agent with a connector list gets the same answer for a target
outside its list as for a name that does not exist.

With a ``connectors`` list on the agent's toolset, a name or alias that
fits only targets outside the list answers ``no_target``, with near-match
targets from inside the list only. This holds for ``call_operation``,
``preview_operation``, the ``target`` filter of ``meho_connector_list`` and
the docs tools: all of them look the target up with the target resolver.

Without a list every answer stays the same. The connectors, the products
and the targets here are made up.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RunUsage
from sqlalchemy import event, select

import meho_backplane.agent.toolset as toolset_mod
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.db.engine import get_engine, get_sessionmaker
from meho_backplane.db.models import IngestedAuthProfile, Tenant
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.docs_search.service import DocsScope
from meho_backplane.mcp.server import McpInvalidParamsError
from meho_backplane.mcp.tools import docs as docs_tools
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations._target_connector_limit import connector_limit
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
_ONLY_FENCE = frozenset({_FENCE})
_PERSON = build_operator(TenantRole.OPERATOR, scopes=frozenset())

#: name -> (product, aliases). Only "lab-fence-a" is inside the list.
_TARGETS: dict[str, tuple[str, list[str]]] = {
    "lab-fence-a": ("fence", ["fence-alias"]),
    "lab-other-a": ("other", ["other-alias", "pair"]),
    "lab-other-b": ("other", ["pair"]),
}

#: A name or alias of a target outside the list, and a name with no target
#: at all that has the same near-match targets.
_HIDDEN_AND_MISSING: list[tuple[str, str]] = [
    ("lab-other-a", "lab-other-z"),
    ("other-alias", "nothing-alias"),
    ("pair", "pairless"),
]


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


async def _add_targets(targets: dict[str, tuple[str, list[str]]]) -> None:
    tenant_id = _PERSON.tenant_id
    async with get_sessionmaker()() as session, session.begin():
        if await session.get(Tenant, tenant_id) is None:
            session.add(Tenant(id=tenant_id, slug=f"t-{tenant_id.hex[:8]}", name="t"))
        for name, (product, aliases) in targets.items():
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


@pytest.fixture
async def targets() -> None:
    await _add_targets(_TARGETS)


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


def _same_form(answer: Any, query: str) -> str:
    """*answer* as text, with *query* and the run time taken out."""
    if isinstance(answer, dict):
        answer = {k: v for k, v in answer.items() if k not in {"duration_ms", "audit_id"}}
    text = json.dumps(answer, sort_keys=True, default=str)
    return text.replace(query, "<query>")


async def _resolver_answer(query: str) -> tuple[str, list[str]] | str:
    """The resolver's error code and match names for *query*, or the found name."""
    async with get_sessionmaker()() as session:
        try:
            found = await resolve_target(session, _PERSON.tenant_id, query)
        except (TargetNotFoundError, AmbiguousTargetError) as exc:
            detail: dict[str, Any] = exc.detail  # type: ignore[assignment]
            return detail["error"], sorted(m["name"] for m in detail["matches"])
    return found.name


# --- the resolver -------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("targets")
@pytest.mark.parametrize(("hidden", "missing"), _HIDDEN_AND_MISSING)
async def test_resolver_answers_a_hidden_target_like_a_missing_name(
    hidden: str, missing: str
) -> None:
    with connector_limit(_ONLY_FENCE):
        assert await _resolver_answer(hidden) == ("no_target", [])
        assert await _resolver_answer(missing) == ("no_target", [])


@pytest.mark.asyncio
@pytest.mark.usefixtures("targets")
async def test_resolver_finds_targets_inside_the_list_by_name_and_alias() -> None:
    with connector_limit(_ONLY_FENCE):
        assert await _resolver_answer("lab-fence-a") == "lab-fence-a"
        assert await _resolver_answer("fence-alias") == "lab-fence-a"


@pytest.mark.asyncio
@pytest.mark.usefixtures("targets")
async def test_resolver_answers_without_a_list_are_unchanged() -> None:
    assert await _resolver_answer("lab-other-a") == "lab-other-a"
    assert await _resolver_answer("other-alias") == "lab-other-a"
    assert await _resolver_answer("pair") == ("ambiguous_target", ["lab-other-a", "lab-other-b"])
    assert await _resolver_answer("lab-other-z") == ("no_target", [])
    assert await _resolver_answer("lab-") == ("no_target", sorted(_TARGETS))


@pytest.mark.asyncio
async def test_near_matches_are_filtered_before_the_cut_to_five() -> None:
    """Six close targets outside the list come first; the one inside still shows."""
    hidden = {f"near-{i}": ("other", []) for i in range(6)}
    await _add_targets({**hidden, "near-9": ("fence", [])})
    with connector_limit(_ONLY_FENCE):
        assert await _resolver_answer("near-") == ("no_target", ["near-9"])
    error, matches = await _resolver_answer("near-")  # type: ignore[misc]
    assert (error, len(matches)) == ("no_target", 5)


# --- call_operation and preview_operation -------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("targets")
@pytest.mark.parametrize(("hidden", "missing"), _HIDDEN_AND_MISSING)
@pytest.mark.parametrize("tool", ["call_operation", "preview_operation"])
async def test_call_answers_a_hidden_target_like_a_missing_name(
    tool: str, hidden: str, missing: str
) -> None:
    async def answer(name: str) -> Any:
        return await _tool(tool, [_FENCE]).function(
            _ctx(), connector_id=_FENCE, op_id="fence.about", target={"name": name}
        )

    hidden_answer = await answer(hidden)
    assert hidden_answer["extras"]["error_code"] == "no_target"
    assert "lab-other" not in _same_form(hidden_answer, hidden)
    assert _same_form(hidden_answer, hidden) == _same_form(await answer(missing), missing)


@pytest.mark.asyncio
@pytest.mark.usefixtures("targets")
@pytest.mark.parametrize("tool", ["call_operation", "preview_operation"])
async def test_call_without_a_list_still_finds_the_target(tool: str) -> None:
    answer = await _tool(tool, None).function(
        _ctx(), connector_id=_FENCE, op_id="fence.about", target={"name": "other-alias"}
    )
    assert answer["extras"].get("error_code") not in {"no_target", "ambiguous_target"}


# --- meho_connector_list target filter ----------------------------------------


async def _connector_list_refusal(connectors: list[str] | None, target: str) -> str:
    with pytest.raises(ModelRetry) as refused:
        await _tool("meho_connector_list", connectors).function(_ctx(), target=target)
    return str(refused.value)


@pytest.mark.asyncio
@pytest.mark.usefixtures("targets")
@pytest.mark.parametrize(("hidden", "missing"), _HIDDEN_AND_MISSING)
async def test_connector_list_answers_a_hidden_target_like_a_missing_name(
    hidden: str, missing: str
) -> None:
    hidden_text = await _connector_list_refusal([_FENCE], hidden)
    assert "no_target" in hidden_text
    assert "other" not in hidden_text.replace(hidden, "")
    missing_text = await _connector_list_refusal([_FENCE], missing)
    assert _same_form(hidden_text, hidden) == _same_form(missing_text, missing)


@pytest.mark.asyncio
@pytest.mark.usefixtures("targets")
async def test_connector_list_target_fit_without_a_list_is_unchanged() -> None:
    answer = await _tool("meho_connector_list", None).function(_ctx(), target="other-alias")
    assert answer["target_fit"]["target"] == "lab-other-a"
    assert answer["target_fit"]["product"] == "other"
    in_list = await _tool("meho_connector_list", [_FENCE]).function(_ctx(), target="fence-alias")
    assert in_list["target_fit"]["target"] == "lab-fence-a"


# --- docs tools ---------------------------------------------------------------


async def _docs_refusal(operator: Operator, target: str) -> tuple[str, dict[str, Any]]:
    with pytest.raises(McpInvalidParamsError) as refused:
        await docs_tools._with_target_or_error(
            "search_docs",
            operator,
            DocsScope(collection_key="docs"),
            target,
            op_id="meho.docs.search",
        )
    return str(refused.value), refused.value.data or {}


@pytest.mark.asyncio
@pytest.mark.usefixtures("targets")
@pytest.mark.parametrize(("hidden", "missing"), _HIDDEN_AND_MISSING)
async def test_docs_tools_answer_a_hidden_target_like_a_missing_name(
    hidden: str, missing: str
) -> None:
    with connector_limit(_ONLY_FENCE):
        hidden_text, hidden_data = await _docs_refusal(_agent(), hidden)
        missing_text, missing_data = await _docs_refusal(_agent(), missing)
    assert hidden_data["error_code"] == "no_target"
    assert hidden_data["matches"] == []
    assert _same_form(hidden_text, hidden) == _same_form(missing_text, missing)
    assert _same_form(hidden_data, hidden) == _same_form(missing_data, missing)


@pytest.mark.asyncio
@pytest.mark.usefixtures("targets")
async def test_docs_tools_without_a_list_still_find_the_target() -> None:
    scope = await docs_tools._with_target_or_error(
        "search_docs",
        _PERSON,
        DocsScope(collection_key="docs"),
        "other-alias",
        op_id="meho.docs.search",
    )
    assert scope.target == "lab-other-a"


# --- list_targets -------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_targets_reads_many_targets_in_few_reads() -> None:
    """A narrow list over many targets reads the targets and the login types once."""
    await _add_targets({f"many-{i:02d}": ("other", []) for i in range(40)})
    await _add_targets({"zz-fence": ("fence", [])})
    statements: list[str] = []

    def _record(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        statements.append(statement.lower())

    sync_engine = get_engine().sync_engine
    event.listen(sync_engine, "before_cursor_execute", _record)
    try:
        answer = await _tool("list_targets", [_FENCE]).function(_ctx(), limit=1)
    finally:
        event.remove(sync_engine, "before_cursor_execute", _record)
    assert [t["name"] for t in answer["targets"]] == ["zz-fence"]
    target_reads = [s for s in statements if s.startswith("select") and "from target" in s]
    login_type_reads = [s for s in statements if "from ingested_auth_profile" in s]
    assert len(target_reads) == 1
    assert len(login_type_reads) == 1


@pytest.mark.asyncio
async def test_list_targets_shows_a_target_of_a_connector_from_a_saved_api_file() -> None:
    """The connector exists only as a saved login type, as on a replica that never ingested it."""
    await _add_targets({"lab-saved-a": ("saved", []), "lab-fence-a": ("fence", [])})
    async with get_sessionmaker()() as session, session.begin():
        saved = (
            await session.execute(select(TargetORM).where(TargetORM.name == "lab-saved-a"))
        ).scalar_one()
        saved.version = "1.0"
        session.add(
            IngestedAuthProfile(
                tenant_id=None,
                product="saved",
                version="1.0",
                impl_id="saved",
                auth_scheme="basic",
                secret_fields=["username", "password"],
                operator_sub="test-operator",
            )
        )
    answer = await _tool("list_targets", ["saved-1.0"]).function(_ctx())
    assert [t["name"] for t in answer["targets"]] == ["lab-saved-a"]
