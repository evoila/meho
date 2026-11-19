# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A hosted agent's connector list also limits what it reads back.

With the agent's list bound (``connector_limit``):

* a stored result keeps the list, and a read finds only a result stored
  under a list inside the reader's own (paging, a query and an inventory
  load); a result stored without a list is a miss;
* the fetch of an approved secret reveal is refused with
  ``connector_not_allowed`` when the request's connector, or the connector
  of its stored target, is not in the list; one audit row, nothing runs;
* ``query_topology`` is not registered for an agent with a list;
* an ``unknown_op`` answer and the agent tool's own refusal name no
  connector outside the list.

Without a list every answer stays the same. The connectors, the products
and the targets here are made up.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RunUsage
from sqlalchemy import select

import meho_backplane.agent.toolset as toolset_mod
import meho_backplane.operations.result_query as rq
import meho_backplane.targets.resolver as target_resolver
from meho_backplane.agent.tool_audit import ToolRefusedRetry
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.inventory.inventory_load import InventoryLoadError, load_inventories
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, AuditLog
from meho_backplane.mcp.tools._call_operation_reveal import fetch_reveal_over_mcp
from meho_backplane.operations import dispatch, register_typed_operation, reset_dispatcher_caches
from meho_backplane.operations._target_connector_limit import connector_limit
from meho_backplane.operations.deny_reasons import CONNECTOR_NOT_ALLOWED
from meho_backplane.operations.result_query import ResultHandleNotFoundError
from meho_backplane.settings import get_settings
from tests._result_store_helpers import MemoryResultStore
from tests._reveal_fetch_helpers import (
    CONNECTOR_ID,
    READ_OP,
    SECRET_VALUE,
    approved_reveal,
    register_vault_read,
)
from tests.mcp_test_fixtures import (
    build_operator,
    required_settings_env,  # noqa: F401 -- pytest-discovered autouse fixture
)

_OTHER = "clusteros-ssh-1.0"
_ONLY_VAULT = frozenset({CONNECTOR_ID})
_PERSON = build_operator(TenantRole.OPERATOR, scopes=frozenset())


class _ClusterConnector(Connector):
    product = "clusteros"
    version = "1.0"
    impl_id = "clusteros-ssh"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError


class _HostConnector(_ClusterConnector):
    product = "hostos"
    version = "1.0"
    impl_id = "hostos-ssh"


class _Target:
    def __init__(self, product: str) -> None:
        self.id = uuid.uuid4()
        self.tenant_id = _PERSON.tenant_id
        self.name = f"{product}-target-a"
        self.host = f"{product}-a.example.invalid"
        self.port = 22
        self.auth_model = "shared_service_account"
        self.product = product
        self.preferred_impl_id = None
        self.fingerprint = None
        self.version = None


@pytest.fixture(autouse=True)
def _registry() -> Iterator[None]:
    reset_dispatcher_caches()
    clear_registry()
    register_connector_v2(
        product="clusteros", version="1.0", impl_id="clusteros-ssh", cls=_ClusterConnector
    )
    register_connector_v2(product="hostos", version="1.0", impl_id="hostos-ssh", cls=_HostConnector)
    yield
    reset_dispatcher_caches()
    clear_registry()


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> MemoryResultStore:
    memory = MemoryResultStore()
    monkeypatch.setattr(rq, "get_result_handle_store", lambda: memory)
    return memory


async def _spill(store: MemoryResultStore, operator: Operator) -> UUID:
    handle_id = uuid.uuid4()
    assert operator.tenant_id is not None
    stored = await store.spill(
        tenant_id=operator.tenant_id,
        operator_sub=operator.sub,
        handle_id=handle_id,
        op_id="clusteros.list",
        rows=[{"name": "row-1"}, {"name": "row-2"}],
        total_rows=2,
        ttl_seconds=60,
        max_rows=10,
    )
    assert stored.stored_rows == 2
    return handle_id


async def _reads(operator: Operator, handle_id: UUID) -> bool:
    try:
        await rq.read_result_window(operator, handle_id, 0, 10)
    except ResultHandleNotFoundError:
        return False
    return True


# --- stored results -----------------------------------------------------------


@pytest.mark.asyncio
async def test_result_stored_under_a_list_is_read_only_under_a_list_that_holds_it(
    store: MemoryResultStore,
) -> None:
    with connector_limit(_ONLY_VAULT):
        handle_id = await _spill(store, _PERSON)
        assert await _reads(_PERSON, handle_id)
    with connector_limit(frozenset({CONNECTOR_ID, _OTHER})):
        assert await _reads(_PERSON, handle_id)
    with connector_limit(frozenset({_OTHER})):
        assert not await _reads(_PERSON, handle_id)
    with connector_limit(frozenset()):
        assert not await _reads(_PERSON, handle_id)
    assert await _reads(_PERSON, handle_id)


@pytest.mark.asyncio
async def test_result_stored_without_a_list_is_a_miss_under_a_list(
    store: MemoryResultStore,
) -> None:
    handle_id = await _spill(store, _PERSON)
    with connector_limit(_ONLY_VAULT):
        assert not await _reads(_PERSON, handle_id)
        with pytest.raises(ResultHandleNotFoundError):
            await rq.run_result_query(_PERSON, handle_id, rq.ResultQuerySpec())
    assert await _reads(_PERSON, handle_id)


@pytest.mark.asyncio
async def test_inventory_load_refuses_a_result_stored_without_a_list() -> None:
    store = MemoryResultStore()
    assert _PERSON.tenant_id is not None
    handle_id = uuid.uuid4()
    store.seed_document(
        tenant_id=_PERSON.tenant_id,
        operator_sub=_PERSON.sub,
        handle_id=handle_id,
        document={"schema_version": "x"},
    )
    with connector_limit(_ONLY_VAULT), pytest.raises(InventoryLoadError, match="not found"):
        await load_inventories(_PERSON, [str(handle_id)], store=store)
    # Without a list the read goes on to the inventory check.
    with pytest.raises(InventoryLoadError) as raised:
        await load_inventories(_PERSON, [str(handle_id)], store=store)
    assert "not found" not in str(raised.value)


# --- through the hosted agent's tools ----------------------------------------


def _agent() -> Operator:
    return _PERSON.model_copy(
        update={"sub": "agent:limited", "principal_kind": PrincipalKind.AGENT}
    )


def _unused_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    return ModelResponse(parts=[TextPart("unused")])


def _ctx(operator: Operator) -> RunContext[Operator]:
    return RunContext(deps=operator, model=FunctionModel(_unused_model), usage=RunUsage())


def _tools(meta_tools: list[str], connectors: list[str] | None) -> dict[str, Any]:
    spec: dict[str, Any] = {"meta_tools": meta_tools}
    if connectors is not None:
        spec["connectors"] = connectors
    return {t.name: t for t in toolset_mod.resolve_agent_tools(spec, _agent())}


@pytest.mark.asyncio
async def test_agent_result_query_reads_only_results_stored_under_its_list(
    store: MemoryResultStore,
) -> None:
    with connector_limit(_ONLY_VAULT):
        own = await _spill(store, _agent())
    other = await _spill(store, _agent())
    limited = _tools(["result_query"], [CONNECTOR_ID])["result_query"]
    answer = await limited.function(_ctx(_agent()), handle_id=str(own))
    assert [row["name"] for row in answer["rows"]] == ["row-1", "row-2"]
    with pytest.raises(ModelRetry, match="is not readable"):
        await limited.function(_ctx(_agent()), handle_id=str(other))
    unlimited = _tools(["result_query"], None)["result_query"]
    answer = await unlimited.function(_ctx(_agent()), handle_id=str(other))
    assert len(answer["rows"]) == 2


def test_query_topology_is_not_registered_with_a_list() -> None:
    resolution = toolset_mod.resolve_agent_toolset(
        {"meta_tools": ["query_topology", "result_query"], "connectors": [CONNECTOR_ID]},
        _agent(),
    )
    assert [t.name for t in resolution.tools] == ["result_query"]
    assert resolution.disabled["query_topology"] == "outside_connector_list"
    without = _tools(["query_topology"], None)
    assert list(without) == ["query_topology"]


@pytest.mark.asyncio
async def test_agent_tool_refusal_names_no_connector_outside_the_list() -> None:
    call = _tools(["call_operation"], [CONNECTOR_ID])["call_operation"]
    with pytest.raises(ToolRefusedRetry) as refused:
        await call.function(_ctx(_agent()), connector_id=_OTHER, op_id="clusteros.list", params={})
    assert _OTHER not in str(refused.value)
    assert CONNECTOR_ID in str(refused.value)


# --- the fetch of an approved secret reveal ----------------------------------


@pytest.fixture
async def vault(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_REVEAL_FETCH", "true")
    get_settings.cache_clear()
    await register_vault_read()


async def _fetch(request_id: UUID) -> dict[str, Any]:
    return await fetch_reveal_over_mcp(
        _PERSON,
        {"connector_id": CONNECTOR_ID, "op_id": READ_OP, "approval_request_id": str(request_id)},
    )


async def _reveal_audit_outcomes(request_id: UUID) -> list[Any]:
    async with get_sessionmaker()() as session:
        rows = (
            (await session.execute(select(AuditLog).where(AuditLog.path == "approval.reveal")))
            .scalars()
            .all()
        )
    return [
        row.payload.get("outcome")
        for row in rows
        if str(row.payload.get("approval_request_id")) == str(request_id)
    ]


async def _store_target(request_id: UUID, monkeypatch: pytest.MonkeyPatch, product: str) -> None:
    """Give the stored request a target of *product*."""
    target = _Target(product)
    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalRequest, request_id)
        assert row is not None
        row.target_id = target.id
        await session.commit()

    async def resolve(session: Any, tenant_id: Any, target_id: Any, **_: Any) -> Any:
        return target if target_id == target.id else None

    monkeypatch.setattr(target_resolver, "resolve_target_by_id", resolve)


def _assert_not_allowed(answer: dict[str, Any], *, outside: str) -> None:
    """Refused, no value, and the connector *outside* the list is not named."""
    assert answer["status"] == "denied"
    assert answer["extras"]["error_code"] == CONNECTOR_NOT_ALLOWED
    assert SECRET_VALUE not in str(answer)
    assert outside not in str(answer)


@pytest.mark.asyncio
@pytest.mark.usefixtures("vault")
async def test_reveal_fetch_of_an_allowed_connector_runs() -> None:
    request_id = await approved_reveal(_PERSON)
    with connector_limit(_ONLY_VAULT):
        answer = await _fetch(request_id)
    assert answer["status"] == "ok", answer
    assert answer["result"]["data"]["password"] == SECRET_VALUE


@pytest.mark.asyncio
@pytest.mark.usefixtures("vault")
@pytest.mark.parametrize("allowed", [frozenset({_OTHER}), frozenset()])
async def test_reveal_fetch_of_a_connector_outside_the_list_is_refused(
    allowed: frozenset[str],
) -> None:
    request_id = await approved_reveal(_PERSON)
    with connector_limit(allowed):
        answer = await _fetch(request_id)
    _assert_not_allowed(answer, outside=CONNECTOR_ID)
    assert await _reveal_audit_outcomes(request_id) == [CONNECTOR_NOT_ALLOWED]
    # Nothing ran: the person still gets the value without the list.
    assert (await _fetch(request_id))["status"] == "ok"


@pytest.mark.asyncio
@pytest.mark.usefixtures("vault")
async def test_reveal_fetch_of_a_target_of_another_connector_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_id = await approved_reveal(_PERSON)
    await _store_target(request_id, monkeypatch, "clusteros")
    with connector_limit(_ONLY_VAULT):
        answer = await _fetch(request_id)
    _assert_not_allowed(answer, outside="clusteros")
    assert await _reveal_audit_outcomes(request_id) == [CONNECTOR_NOT_ALLOWED]


@pytest.mark.asyncio
@pytest.mark.usefixtures("vault")
async def test_reveal_fetch_of_a_target_of_an_allowed_connector_is_not_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_id = await approved_reveal(_PERSON)
    await _store_target(request_id, monkeypatch, "vault")
    with connector_limit(_ONLY_VAULT):
        answer = await _fetch(request_id)
    assert answer["extras"].get("error_code") != CONNECTOR_NOT_ALLOWED
    assert CONNECTOR_NOT_ALLOWED not in await _reveal_audit_outcomes(request_id)


@pytest.mark.asyncio
@pytest.mark.usefixtures("vault")
async def test_reveal_fetch_of_a_target_that_is_gone_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_id = await approved_reveal(_PERSON)
    await _store_target(request_id, monkeypatch, "vault")
    monkeypatch.setattr(target_resolver, "resolve_target_by_id", _no_target)
    with connector_limit(_ONLY_VAULT):
        answer = await _fetch(request_id)
    _assert_not_allowed(answer, outside="clusteros")


async def _no_target(*args: Any, **kwargs: Any) -> None:
    return None


# --- an unknown operation ----------------------------------------------------


async def _op(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True}


async def _register(product: str, impl_id: str, op_id: str) -> None:
    embedding = AsyncMock()
    embedding.encode_one.return_value = [0.1] * 384
    embedding.encode.return_value = [[0.1] * 384]
    embedding.dimension = 384
    await register_typed_operation(
        product=product,
        version="1.0",
        impl_id=impl_id,
        op_id=op_id,
        handler=_op,
        summary=f"{op_id} summary.",
        description=f"{op_id} description.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=embedding,
    )


@pytest.mark.asyncio
async def test_unknown_op_names_only_connectors_in_the_list() -> None:
    await _register("hostos", "hostos-ssh", "hostos.about")
    await _register("clusteros", "clusteros-ssh", "shared.about")

    async def call() -> Any:
        return await dispatch(
            operator=_agent(),
            connector_id="hostos-ssh-1.0",
            op_id="shared.about",
            target=None,
            params={},
        )

    without = await call()
    assert without.extras["error_code"] == "unknown_op"
    assert without.extras["connectors_with_op"] == [_OTHER]
    with connector_limit(frozenset({"hostos-ssh-1.0"})):
        limited = await call()
    assert limited.extras["error_code"] == "unknown_op"
    assert limited.extras["connectors_with_op"] == []
    assert "clusteros" not in limited.model_dump_json()
    with connector_limit(frozenset({"hostos-ssh-1.0", _OTHER})):
        both = await call()
    assert both.extras["connectors_with_op"] == [_OTHER]
