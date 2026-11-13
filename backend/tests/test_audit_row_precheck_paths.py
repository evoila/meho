# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The audit row check stops runbook, scheduler and hosted agent calls too.

Each of these paths sets its own caller and audit values before it calls
``call_operation``: a runbook step uses the run's assignee and ticket id, a
scheduled fire uses the agent login and the trigger's ticket id, and a
hosted agent run uses the person who started it. Each test drives the path
from its own entry point with a value the audit table refuses, and checks
that the call was refused with ``audit_row_invalid`` and that the fake
target got zero calls.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import select

from meho_backplane.agent.invocation import AgentInvoker
from meho_backplane.agent.run import PydanticAgentRun
from meho_backplane.agents.schemas import AgentDefinitionCreate, AgentModelTier
from meho_backplane.agents.service import AgentDefinitionService
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AgentPermission,
    AgentPrincipal,
    AuditLog,
    ScheduledTriggerKind,
    Target,
    Tenant,
)
from meho_backplane.operations import register_typed_operation, reset_dispatcher_caches
from meho_backplane.operations._audit import step_id_var, work_ref_var
from meho_backplane.operations.audit_row_precheck import INDEX_TEXT_MAX_BYTES
from meho_backplane.retrieval.embedding import EMBEDDING_DIMENSION
from meho_backplane.runbooks import run_service as run_service_module
from meho_backplane.runbooks.run_service import PreviousStepFailedError, RunbookRunService
from meho_backplane.runbooks.runs_schemas import NextStepRequest, StartRunRequest
from meho_backplane.runbooks.schemas import (
    DraftTemplateRequest,
    OperationCallStep,
    OperationCallVerify,
    PublishTemplateRequest,
    RunbookTemplateBody,
)
from meho_backplane.runbooks.service import RunbookTemplateService
from meho_backplane.scheduler import loop as scheduler_loop
from meho_backplane.settings import get_settings

pytestmark = pytest.mark.asyncio

TENANT_ID = UUID("00000000-0000-0000-0000-0000000064a8")
CONNECTOR_ID = "demo-1.x"
OP_ID = "demo.thing.write"
AGENT_NAME = "writer"
AGENT_LOGIN = f"agent:{AGENT_NAME}"
TOO_LONG = "t" * (INDEX_TEXT_MAX_BYTES + 1)

#: What the fake target saw: one entry per call that reached it.
target_calls: list[dict[str, Any]] = []


async def recording_handler(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Typed handler standing in for a vendor change."""
    target_calls.append(params)
    return {"ok": True}


class _DemoConnector(Connector):
    product = "demo"
    version = "1.x"
    impl_id = "demo"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> Any:  # type: ignore[override]
        raise NotImplementedError


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    reset_dispatcher_caches()
    clear_registry()
    target_calls.clear()
    work_token = work_ref_var.set(None)
    step_token = step_id_var.set(None)
    yield
    step_id_var.reset(step_token)
    work_ref_var.reset(work_token)
    reset_dispatcher_caches()
    clear_registry()
    target_calls.clear()


@pytest.fixture
async def demo_op() -> None:
    embedding = AsyncMock()
    embedding.encode_one.return_value = [0.1] * EMBEDDING_DIMENSION
    embedding.encode.return_value = [[0.1] * EMBEDDING_DIMENSION]
    embedding.dimension = EMBEDDING_DIMENSION
    register_connector_v2(product="demo", version="", impl_id="", cls=_DemoConnector)
    await register_typed_operation(
        product="demo",
        version="1.x",
        impl_id="demo",
        op_id=OP_ID,
        handler=recording_handler,
        summary="Demo op.",
        description="Demo op for the audit row check.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=embedding,
    )
    async with get_sessionmaker()() as session:
        session.add(Tenant(id=TENANT_ID, slug="precheck", name="Precheck"))
        session.add(
            Target(
                tenant_id=TENANT_ID,
                name="box",
                product="demo",
                host="box.example",
                auth_model="shared_service_account",
            )
        )
        await session.commit()


async def _dispatch_rows() -> list[AuditLog]:
    async with get_sessionmaker()() as session:
        query = select(AuditLog).where(AuditLog.method == "DISPATCH", AuditLog.path == OP_ID)
        return list((await session.execute(query)).scalars().all())


# ---------------------------------------------------------------------------
# Runbook step
# ---------------------------------------------------------------------------


async def test_a_runbook_step_is_refused_before_the_call(
    demo_op: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run's ticket id is too long for its index: the step call is refused."""
    results: list[dict[str, Any]] = []
    real_call_operation = run_service_module.call_operation

    async def _recording_call_operation(operator: Operator, arguments: Any) -> Any:
        result = await real_call_operation(operator, arguments)
        results.append(result)
        return result

    monkeypatch.setattr(run_service_module, "call_operation", _recording_call_operation)
    body = RunbookTemplateBody(
        title="One call",
        description="calls the demo op",
        target_kind="demo",
        steps=[
            OperationCallStep(
                id="call-it",
                title="Call it",
                body="Call the demo op",
                type="operation_call",
                op_id=OP_ID,
                params={},
                verify=OperationCallVerify(
                    type="operation_call", op_id=OP_ID, params={}, expect={"ok": True}
                ),
            )
        ],
    )
    templates = RunbookTemplateService()
    await templates.create_draft(
        TENANT_ID, "op-runbook", DraftTemplateRequest(slug="one-call", body=body)
    )
    await templates.publish(TENANT_ID, PublishTemplateRequest(slug="one-call", version=1))
    runs = RunbookRunService()
    start = await runs.start_run(
        TENANT_ID,
        "op-runbook",
        StartRunRequest(template_slug="one-call", target="box", params={}, work_ref=TOO_LONG),
    )

    # The refused call fails the step, so the run does not go on.
    with pytest.raises(PreviousStepFailedError):
        await runs.next_step(
            TENANT_ID,
            "op-runbook",
            start.run_id,
            NextStepRequest(last_verified=True, verify_response=None),
        )

    assert len(results) == 1
    assert results[0]["status"] == "error"
    assert results[0]["extras"]["error_code"] == "audit_row_invalid"
    assert results[0]["extras"]["field"] == "work_ref"
    assert target_calls == []
    assert await _dispatch_rows() == []


# ---------------------------------------------------------------------------
# Hosted agent runs: started by a person, and fired by the scheduler
# ---------------------------------------------------------------------------


def _call_op_model(tool_returns: list[str]) -> FunctionModel:
    """A model that calls ``call_operation`` once and keeps what it got back."""

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        returns = [
            str(part.content)
            for message in messages
            if isinstance(message, ModelRequest)
            for part in message.parts
            if getattr(part, "part_kind", "") == "tool-return"
        ]
        if not returns:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "call_operation",
                        {"connector_id": CONNECTOR_ID, "op_id": OP_ID, "params": {}},
                    )
                ]
            )
        tool_returns.extend(returns)
        return ModelResponse(parts=[TextPart("done")])

    return FunctionModel(fn)


async def _seed_agent() -> None:
    async with get_sessionmaker()() as session:
        session.add(
            AgentPrincipal(
                id=uuid4(),
                tenant_id=TENANT_ID,
                name=AGENT_NAME,
                keycloak_client_id=AGENT_LOGIN,
                keycloak_internal_id=f"kc-internal-{AGENT_NAME}",
                owner_sub="seed-admin",
                revoked=False,
                created_by_sub="seed-admin",
            )
        )
        session.add(
            AgentPermission(
                tenant_id=TENANT_ID,
                principal_sub=AGENT_LOGIN,
                op_pattern="*",
                verdict="auto-execute",
                created_by_sub="seed-admin",
            )
        )
        await session.commit()
    await AgentDefinitionService().create(
        tenant_id=TENANT_ID,
        created_by_sub="seed-admin",
        payload=AgentDefinitionCreate(
            name=AGENT_NAME,
            identity_ref=AGENT_LOGIN,
            model_tier=AgentModelTier.STANDARD,
            system_prompt="You call MEHO operations.",
            toolset={"meta_tools": ["call_operation"]},
            turn_budget=5,
            enabled=True,
        ),
    )


def _operator(sub: str, kind: PrincipalKind) -> Operator:
    return Operator(
        sub=sub,
        name="Precheck Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=kind,
        client_id=sub if kind is PrincipalKind.AGENT else None,
    )


def _assert_agent_call_refused(tool_returns: list[str], field: str) -> None:
    assert len(tool_returns) == 1
    assert "audit_row_invalid" in tool_returns[0]
    assert f"'field': '{field}'" in tool_returns[0]
    assert target_calls == []


async def test_a_hosted_agent_call_is_refused_before_the_call(demo_op: None) -> None:
    """The person who starts the run has a login id too long for its index."""
    await _seed_agent()
    tool_returns: list[str] = []
    invoker = AgentInvoker(
        runtime=PydanticAgentRun(model_factory=lambda: _call_op_model(tool_returns))
    )

    await invoker.run(_operator(TOO_LONG, PrincipalKind.USER), AGENT_NAME, "go")

    _assert_agent_call_refused(tool_returns, "operator_sub")
    assert await _dispatch_rows() == []


async def test_a_scheduled_agent_call_is_refused_before_the_call(
    demo_op: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The trigger's ticket id is too long for its index: the run's call is refused."""
    await _seed_agent()
    monkeypatch.setattr(
        "meho_backplane.agent.invocation.get_client_credentials_token",
        AsyncMock(return_value="agent-token"),
    )
    monkeypatch.setattr(
        "meho_backplane.agent.invocation.verify_jwt_for_audience",
        AsyncMock(return_value=_operator(AGENT_LOGIN, PrincipalKind.AGENT)),
    )
    tool_returns: list[str] = []
    invoker = AgentInvoker(
        runtime=PydanticAgentRun(model_factory=lambda: _call_op_model(tool_returns))
    )
    prepared = SimpleNamespace(
        name=AGENT_NAME,
        identity_ref=AGENT_LOGIN,
        agent_client_id=AGENT_LOGIN,
        agent_client_secret=SecretStr("not-a-secret"),
        inputs_str="go",
        work_ref=TOO_LONG,
    )

    launched = await scheduler_loop._dispatch_invocation(
        SimpleNamespace(id=uuid4(), kind=ScheduledTriggerKind.ONE_OFF.value),  # type: ignore[arg-type]
        prepared,  # type: ignore[arg-type]
        invoker,
    )

    assert launched is True
    _assert_agent_call_refused(tool_returns, "work_ref")
    assert await _dispatch_rows() == []
