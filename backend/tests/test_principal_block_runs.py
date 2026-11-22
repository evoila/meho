# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A blocked person's hosted runs, approved agent calls and unreadable block list.

What these tests check:

* A hosted agent run stops once its person is blocked: a write tool
  (``add_to_memory``), a read tool (``search_knowledge``) and
  ``invoke_agent`` are each refused, the tool does not run, and the run
  ends failed with the refusal text. A run of a person blocked before it
  starts makes no model request. After unblock a new run works.
* The block list is read again after at most five seconds.
* The in-process agent waiter does not run an approved call while its
  asker is blocked.
* When the block list cannot be read, the console answers 503 and an
  operation call returns ``principal_block_check_unavailable`` and sends
  nothing to the vendor.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from httpx import ASGITransport
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import select

import meho_backplane.operations.meta_tools as meta_tools_module
from meho_backplane.agent import toolset
from meho_backplane.agent.approval_wait import _resume_approved_or_already_resumed
from meho_backplane.agent.invocation import (
    AgentInvoker,
    _finalize_child_run,
    _record_child_run,
    _resolve_child_definition,
)
from meho_backplane.agent.run import PydanticAgentRun
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.auth.principal_block import (
    BLOCK_CACHE_SECONDS,
    BLOCK_SCOPE_ALL,
    PRINCIPAL_BLOCKED_MESSAGE,
    block_principal,
    is_principal_blocked,
    reset_block_cache,
    unblock_principal,
)
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentRun as AgentRunRow
from meho_backplane.db.models import (
    AgentRunStatus,
    ApprovalRequest,
    ApprovalRequestStatus,
    AuditLog,
    PrincipalBlock,
    Tenant,
)
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations._validate import compute_params_hash
from meho_backplane.operations.approval_queue import create_pending_request
from meho_backplane.operations.dispatcher import dispatch
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth import SESSION_COOKIE_NAME, UISessionMiddleware
from meho_backplane.ui.auth.session_store import create_session, reset_fernet_cache_for_testing
from tests.test_agent_invocation import _TENANT_A, _make_operator, _seed_definition

pytestmark = pytest.mark.asyncio

_BLOCK_MODULE = "meho_backplane.auth.principal_block"
_REFUSAL = f"principal_blocked: {PRINCIPAL_BLOCKED_MESSAGE}"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("UI_SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.delenv("GIT_SHA", raising=False)
    monkeypatch.delenv("CHART_VERSION", raising=False)
    get_settings.cache_clear()
    reset_fernet_cache_for_testing()
    reset_block_cache()
    clear_registry()
    reset_dispatcher_caches()
    yield
    get_settings.cache_clear()
    reset_fernet_cache_for_testing()
    reset_block_cache()
    clear_registry()
    reset_dispatcher_caches()


def _admin(tenant_id: uuid.UUID = _TENANT_A) -> Operator:
    return Operator(
        sub="admin-person",
        name=None,
        email=None,
        raw_jwt="<test>",
        tenant_id=tenant_id,
        tenant_role=TenantRole.TENANT_ADMIN,
        principal_kind=PrincipalKind.USER,
    )


def _unreadable_block_list(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _broken(_sessionmaker: Any) -> frozenset[tuple[str, str]]:
        raise RuntimeError("database down")

    monkeypatch.setattr(f"{_BLOCK_MODULE}._load_entries", _broken)


# ---------------------------------------------------------------------------
# The five-second limit
# ---------------------------------------------------------------------------


async def test_block_list_is_read_again_after_at_most_five_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A block written by another process counts once five seconds have passed."""
    assert BLOCK_CACHE_SECONDS <= 5.0
    clock = [1000.0]
    monkeypatch.setattr(f"{_BLOCK_MODULE}.time.monotonic", lambda: clock[0])
    assert await is_principal_blocked("late-person", None) is False

    async with get_sessionmaker()() as session:
        session.add(
            PrincipalBlock(
                sub="late-person",
                scope=BLOCK_SCOPE_ALL,
                reason="written by another process",
                blocked_by_sub="other-admin",
                blocked_at=datetime.now(UTC),
            )
        )
        await session.commit()

    clock[0] += 5.0
    assert await is_principal_blocked("late-person", None) is True


# ---------------------------------------------------------------------------
# Hosted agent runs
# ---------------------------------------------------------------------------


def _model(tool_call: ToolCallPart, before_call: Callable[[], Any] | None = None) -> FunctionModel:
    """A model that makes *tool_call* once, then answers.

    *before_call* runs right before the model returns the tool call. As a
    child run (its system prompt holds ``CHILD``) the model only answers.
    """
    state = {"called": False}

    async def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        parts = [p for m in messages if isinstance(m, ModelRequest) for p in m.parts]
        if any("CHILD" in str(getattr(p, "content", "")) for p in parts):
            return ModelResponse(parts=[TextPart("child done")])
        if state["called"]:
            return ModelResponse(parts=[TextPart("done")])
        state["called"] = True
        if before_call is not None:
            await before_call()
        return ModelResponse(parts=[tool_call])

    return FunctionModel(fn)


def _invoker(model: FunctionModel) -> AgentInvoker:
    return AgentInvoker(
        runtime=PydanticAgentRun(
            model_factory=lambda: model,
            child_agent_resolver=_resolve_child_definition,
            child_run_recorder=_record_child_run,
            child_run_finalizer=_finalize_child_run,
        )
    )


def _spy_handler(monkeypatch: pytest.MonkeyPatch, name: str) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def _handler(_operator: Operator, arguments: dict[str, Any]) -> dict[str, Any]:
        calls.append(arguments)
        return {"status": "ok"}

    spec = next(s for s in toolset._META_TOOL_CATALOG if s.name == name)
    monkeypatch.setattr(spec, "handler", _handler)
    return calls


async def _child_runs(parent_run_id: uuid.UUID) -> list[AgentRunRow]:
    async with get_sessionmaker()() as session:
        query = select(AgentRunRow).where(AgentRunRow.parent_run_id == parent_run_id)
        return list((await session.execute(query)).scalars())


_TOOL_CALLS = [
    pytest.param(ToolCallPart("add_to_memory", {"content": "note"}), id="write-add_to_memory"),
    pytest.param(ToolCallPart("search_knowledge", {"query": "how"}), id="read-search_knowledge"),
    pytest.param(
        ToolCallPart("invoke_agent", {"agent_name": "child", "inputs": "sub"}), id="invoke_agent"
    ),
]


@pytest.mark.parametrize("tool_call", _TOOL_CALLS)
async def test_hosted_run_stops_when_its_person_is_blocked_and_works_after_unblock(
    monkeypatch: pytest.MonkeyPatch, tool_call: ToolCallPart
) -> None:
    await _seed_definition(
        name="runner",
        toolset={"meta_tools": ["add_to_memory", "search_knowledge", "invoke_agent"]},
    )
    await _seed_definition(name="child", system_prompt="CHILD: you answer.")
    spied = {
        name: _spy_handler(monkeypatch, name) for name in ("add_to_memory", "search_knowledge")
    }
    person = _make_operator()

    async def _block_now() -> None:
        await block_principal(_admin(), person.sub, "left the team")

    refused = await _invoker(_model(tool_call, _block_now)).run(person, "runner", "go")

    assert refused.status is AgentRunStatus.FAILED
    assert refused.error is not None
    assert _REFUSAL in refused.error
    assert spied["add_to_memory"] == []
    assert spied["search_knowledge"] == []
    assert await _child_runs(refused.run_id) == []
    async with get_sessionmaker()() as session:
        query = select(AuditLog).where(AuditLog.agent_session_id == refused.run_id)
        rows = list((await session.execute(query)).scalars())
    assert [(r.payload["op_id"], r.status_code) for r in rows] == [(tool_call.tool_name, 403)]

    await unblock_principal(_admin(), person.sub)
    works = await _invoker(_model(tool_call)).run(person, "runner", "go")

    assert works.status is AgentRunStatus.SUCCEEDED, works.error
    used = spied.get(tool_call.tool_name)
    if used is not None:
        assert len(used) == 1
    else:
        assert len(await _child_runs(works.run_id)) == 1


async def test_hosted_run_of_a_blocked_person_makes_no_model_request() -> None:
    await _seed_definition(name="runner")
    person = _make_operator()
    await block_principal(_admin(), person.sub, "left the team")
    requests: list[int] = []

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        requests.append(len(messages))
        return ModelResponse(parts=[TextPart("done")])

    outcome = await _invoker(FunctionModel(fn)).run(person, "runner", "go")

    assert outcome.status is AgentRunStatus.FAILED
    assert outcome.error is not None
    assert _REFUSAL in outcome.error
    assert requests == []


# ---------------------------------------------------------------------------
# The in-process agent waiter
# ---------------------------------------------------------------------------


async def test_agent_waiter_does_not_run_an_approved_call_while_its_asker_is_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with get_sessionmaker()() as session:
        session.add(Tenant(id=_TENANT_A, slug="waiter-a", name="Waiter A"))
        await session.commit()
    agent = _make_operator(sub="agent-asker", principal_kind=PrincipalKind.AGENT)
    params = {"path": "app/config", "value": "<test>"}
    async with get_sessionmaker()() as session:
        request = await create_pending_request(
            session,
            operator=agent,
            connector_id="vault-1.x",
            op_id="vault.kv.put",
            target=None,
            params=dict(params),
            params_hash=compute_params_hash(params),
            run_id=uuid.uuid4(),
        )
        request.status = ApprovalRequestStatus.APPROVED.value
        request.decided_at = datetime.now(UTC)
        request.reviewed_by = "approver-person"
        await session.commit()
        request_id = request.id
    vendor_calls: list[dict[str, Any]] = []

    async def _spy(_operator: Operator, arguments: dict[str, Any], **_kwargs: Any) -> Any:
        vendor_calls.append(arguments)
        return {"status": "ok"}

    monkeypatch.setattr(meta_tools_module, "call_operation_with_approval", _spy)
    await block_principal(_admin(), agent.sub, "left the team")

    envelope = await _resume_approved_or_already_resumed(
        operator=agent,
        call_arguments={
            "connector_id": "vault-1.x",
            "op_id": "vault.kv.put",
            "params": dict(params),
            "target": None,
        },
        awaiting_envelope={"op_id": "vault.kv.put", "extras": {}},
        approval_request_id=request_id,
    )

    assert vendor_calls == []
    assert envelope["status"] == "denied"
    assert envelope["extras"]["error_code"] == "principal_blocked"
    async with get_sessionmaker()() as session:
        stored = await session.get(ApprovalRequest, request_id)
    assert stored is not None
    assert stored.resumed_at is not None


# ---------------------------------------------------------------------------
# The block list cannot be read
# ---------------------------------------------------------------------------


async def test_console_answers_503_when_the_block_list_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with get_sessionmaker()() as session:
        session.add(Tenant(id=_TENANT_A, slug="console-a", name="Console A"))
        await session.commit()
    console = FastAPI()
    console.add_middleware(UISessionMiddleware)

    @console.get("/ui/ping")
    async def _ping() -> PlainTextResponse:
        return PlainTextResponse("page")

    async with get_sessionmaker()() as session, session.begin():
        decrypted = await create_session(
            session,
            operator_sub="console-person",
            tenant_id=_TENANT_A,
            access_token="<test>",
            refresh_token="<test>",
            lifetime=timedelta(hours=1),
        )
    _unreadable_block_list(monkeypatch)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=console), base_url="https://testserver"
    ) as ac:
        response = await ac.get(
            "/ui/ping", headers={"Cookie": f"{SESSION_COOKIE_NAME}={decrypted.id}"}
        )

    assert response.status_code == 503
    assert response.text == "MEHO cannot check the block list."


async def test_operation_call_is_refused_when_the_block_list_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _unreadable_block_list(monkeypatch)
    with respx.mock(assert_all_called=False) as router:
        vendor = router.route().mock(return_value=httpx.Response(200, json={}))
        result = await dispatch(
            operator=_make_operator(),
            connector_id="vault-1.x",
            op_id="vault.kv.get",
            target=None,
            params={},
        )

    assert result.status == "error"
    assert result.extras["error_code"] == "principal_block_check_unavailable"
    assert result.extras["execution_outcome"] == "not_started"
    assert not vendor.called
