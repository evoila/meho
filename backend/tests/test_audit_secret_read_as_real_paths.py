# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``secret_read_as`` on the real path of three kinds of caller.

Each test runs the call the way MEHO runs it, against a stand-in Vault, and
reads the audit row of the call that read the secret:

* a call that waits for approval, a second person approves it, and the
  approvals surface runs it: the row names the approver, whose login the store
  accepted for that run, not the person who asked;
* an agent call that waits for approval and that the agent resumes itself:
  the row names the agent's login, not the person who approved;
* a hosted agent run that a person started: the row names the person, whose
  login the store accepted, not the agent.

Every login is made up at run time. Each test also checks that the value is
the ``sub`` of the token the stand-in store got at its login for that run.
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import select

from meho_backplane.agent.approval_wait import resume_or_surface_awaiting_approval
from meho_backplane.agent.invocation import AgentInvoker
from meho_backplane.agent.run import PydanticAgentRun
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.auth.secret_read_as import token_subject
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.vault import VaultConnector
from meho_backplane.connectors.vault.ops import register_vault_typed_operations, vault_kv_read
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, AuditLog
from meho_backplane.operations import dispatch, register_typed_operation, reset_dispatcher_caches
from meho_backplane.operations.approval_queue import (
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.settings import get_settings
from tests._people_behind_helpers import give_known_person
from tests._vault_fakes import _FakeClient, install_fake_vault
from tests.test_agent_invocation import _TENANT_A, _seed_definition

pytestmark = pytest.mark.asyncio

_CONNECTOR_ID = "vault-1.x"
#: A test operation that reads one KV secret and always waits for approval.
_APPROVED_OP = "vault.demo.approved_read"
_PARAMS = {"mount": "secret", "path": "demo/app"}


def _jwt(sub: str) -> str:
    """A compact token with a made-up ``sub`` claim."""
    payload = base64.urlsafe_b64encode(json.dumps({"sub": sub}).encode()).decode().rstrip("=")
    return f"e30.{payload}.sig"


def _login(prefix: str) -> str:
    """A made-up login id, new for each test."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _operator(sub: str, kind: PrincipalKind = PrincipalKind.USER) -> Operator:
    return Operator(
        sub=sub,
        name=None,
        email=None,
        raw_jwt=_jwt(sub),
        tenant_id=_TENANT_A,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=kind,
        client_id=sub if kind is PrincipalKind.AGENT else None,
    )


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_KV_TENANT_SCOPE_PREFIX", "")
    monkeypatch.delenv("AUDIT_SECRET_READ_AS", raising=False)
    get_settings.cache_clear()
    reset_dispatcher_caches()
    clear_registry()
    register_connector_v2(product="vault", version="1.x", impl_id="vault", cls=VaultConnector)
    yield
    reset_dispatcher_caches()
    clear_registry()
    get_settings.cache_clear()


@pytest.fixture
def embedding() -> AsyncMock:
    from meho_backplane.retrieval.embedding import EMBEDDING_DIMENSION

    service = AsyncMock()
    service.encode_one.return_value = [0.1] * EMBEDDING_DIMENSION
    service.encode.return_value = [[0.1] * EMBEDDING_DIMENSION]
    service.dimension = EMBEDDING_DIMENSION
    return service


def _store(monkeypatch: pytest.MonkeyPatch) -> _FakeClient:
    fake = install_fake_vault(monkeypatch)
    fake.secrets.kv.v2.secret = {"password": "made-up-value-0zzz"}
    return fake


def _accepted_logins(fake: _FakeClient) -> list[str]:
    """The ``sub`` of each token the stand-in store accepted at a login."""
    return [token_subject(call["jwt"], "") for call in fake.auth.jwt.login_calls]


async def _register_approved_op(embedding: AsyncMock) -> None:
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id=_APPROVED_OP,
        handler=vault_kv_read,
        summary="Read one secret after approval.",
        description="Test op that reads a KV secret and always waits for approval.",
        parameter_schema={"type": "object"},
        safety_level="safe",
        requires_approval=True,
        when_to_use=None,
        embedding_service=embedding,
    )


async def _park(requester: Operator) -> Any:
    """The requester's call waits for approval. Returns the dispatch answer."""
    await give_known_person(requester)
    parked = await dispatch(
        operator=requester,
        connector_id=_CONNECTOR_ID,
        op_id=_APPROVED_OP,
        target=None,
        params=dict(_PARAMS),
    )
    assert parked.status == "awaiting_approval", parked
    return parked


async def _approve(request_id: uuid.UUID, approver: Operator) -> None:
    async with get_sessionmaker()() as session:
        await approve_request(session, request_id, operator=approver)
        await session.commit()


async def _run_rows(op_id: str) -> list[AuditLog]:
    """The rows of the calls that ran *op_id* (not the approval rows)."""
    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(AuditLog).where(AuditLog.path == op_id))).scalars()
        return [row for row in rows if row.status_code == 200]


async def test_approved_call_names_the_approver_whose_login_read_the_secret(
    monkeypatch: pytest.MonkeyPatch, embedding: AsyncMock
) -> None:
    fake = _store(monkeypatch)
    await _register_approved_op(embedding)
    requester = _operator(_login("requester"))
    approver = _operator(_login("approver"))

    parked = await _park(requester)
    assert fake.auth.jwt.login_calls == []
    request_id = uuid.UUID(parked.extras["approval_request_id"])
    await _approve(request_id, approver)
    async with get_sessionmaker()() as session:
        request = await session.get(ApprovalRequest, request_id)
        assert request is not None
        result = await resume_dispatch_after_approval(operator=approver, request=request)

    assert result.status == "ok", result.error
    assert _accepted_logins(fake) == [approver.sub]
    rows = await _run_rows(_APPROVED_OP)
    assert len(rows) == 1
    assert rows[0].operator_sub == approver.sub
    assert rows[0].secret_read_as == approver.sub
    assert rows[0].secret_read_as != requester.sub


async def test_agent_that_resumes_its_own_call_names_its_own_login(
    monkeypatch: pytest.MonkeyPatch, embedding: AsyncMock
) -> None:
    fake = _store(monkeypatch)
    await _register_approved_op(embedding)
    agent = _operator(_login("agent:reader"), PrincipalKind.AGENT)
    reviewer = _operator(_login("reviewer"))

    parked = await _park(agent)
    await _approve(uuid.UUID(parked.extras["approval_request_id"]), reviewer)
    resumed = await resume_or_surface_awaiting_approval(
        operator=agent,
        call_arguments={
            "connector_id": _CONNECTOR_ID,
            "op_id": _APPROVED_OP,
            "params": dict(_PARAMS),
            "target": None,
        },
        awaiting_envelope=parked.model_dump(mode="json"),
        timeout_seconds=2.0,
    )

    assert resumed["status"] == "ok", resumed
    assert _accepted_logins(fake) == [agent.sub]
    rows = await _run_rows(_APPROVED_OP)
    assert len(rows) == 1
    assert rows[0].operator_sub == agent.sub
    assert rows[0].secret_read_as == agent.sub
    assert rows[0].secret_read_as != reviewer.sub


def _call_then_answer(call: ToolCallPart) -> FunctionModel:
    """A model that makes *call* once, then answers."""
    remaining = [call]

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if remaining:
            return ModelResponse(parts=[remaining.pop(0)])
        return ModelResponse(parts=[TextPart("done")])

    return FunctionModel(fn)


async def test_hosted_run_names_the_person_who_started_it(
    monkeypatch: pytest.MonkeyPatch, embedding: AsyncMock
) -> None:
    fake = _store(monkeypatch)
    await register_vault_typed_operations(embedding_service=embedding)
    await _seed_definition()
    person = _operator(_login("person"))
    call = ToolCallPart(
        "call_operation",
        {"connector_id": _CONNECTOR_ID, "op_id": "vault.kv.read", "params": dict(_PARAMS)},
    )

    invoker = AgentInvoker(runtime=PydanticAgentRun(model_factory=lambda: _call_then_answer(call)))
    outcome = await invoker.run(person, "reader", "go")

    assert _accepted_logins(fake) == [person.sub]
    async with get_sessionmaker()() as session:
        rows = list(
            (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.agent_session_id == outcome.run_id,
                        AuditLog.path == "vault.kv.read",
                    )
                )
            ).scalars()
        )
    assert len(rows) == 1, [(r.method, r.path, r.status_code) for r in rows]
    assert rows[0].status_code == 200
    assert rows[0].operator_sub == person.sub
    assert rows[0].actor_sub == "agent:reader"
    assert rows[0].secret_read_as == person.sub
