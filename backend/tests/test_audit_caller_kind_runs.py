# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""With ``CUSTOM_POLICY`` on, the kind of caller of runs that MEHO starts or resumes.

Whole-path runs through the real dispatcher and gate, then the test reads
the audit rows:

* an approved call that a person resumes: the row of the run carries the
  kind of the login that runs it (here the approver, a person's token), the
  same login as its ``operator_sub``. The ``approval.request`` row and the
  waiting request keep the requester's kind;
* a step that waits again while a person resumes an agent's run: the new
  request keeps the agent's kind, not the person's;
* a hosted agent run that a person started from the web console: every
  row is ``service_agent``. The acting agent counts before the web session;
* a check of the check runner: ``system``, the kind its login has;
* a person's token call that MEHO stopped at shutdown: its "unfinished"
  row keeps the kind, saved when the call started.

Fixture values are made up.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
import respx
from pydantic_ai.messages import ToolCallPart
from sqlalchemy import select

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.checks.runner import _run_evaluation, _SensorSnapshot
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentRunStatus, AuditLog
from meho_backplane.operations import dispatcher, shutdown_drain
from meho_backplane.operations._validate import compute_params_hash
from meho_backplane.operations.approval_queue import create_pending_request, requester_of
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth.refresh import verify_access_token_with_refresh
from meho_backplane.ui.auth.session_store import DecryptedSession
from tests._oidc_jwt_helpers import (
    AUDIENCE,
    ISSUER,
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)
from tests.test_agent_grant_required import _reset_state, stub_embedding_service
from tests.test_agent_invocation import _TENANT_A, _seed_definition, _seed_echo_op
from tests.test_agent_tool_audit import _invoker, _scripted
from tests.test_approved_run_rights import (
    _OP,
    _approve_on_route,
    _park,
    _request,
    _requester,
    client,
    op,
)
from tests.test_sensor_runner import _OK_ASSERTION
from tests.test_shutdown_drain import _block_body

__all__ = ["_reset_state", "client", "op", "stub_embedding_service"]

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("CUSTOM_POLICY", "true")
    monkeypatch.delenv("AGENT_GRANT_REQUIRED", raising=False)
    get_settings.cache_clear()
    clear_jwks_cache()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()


async def _rows(**where: Any) -> list[AuditLog]:
    query = select(AuditLog).order_by(AuditLog.occurred_at)
    for column, value in where.items():
        query = query.where(getattr(AuditLog, column) == value)
    async with get_sessionmaker()() as session:
        return list((await session.execute(query)).scalars().all())


@pytest.mark.usefixtures("op")
async def test_an_approved_call_runs_under_the_kind_of_who_runs_it(
    client: tuple[Any, Any],
) -> None:
    requester = _requester("agent")
    request_id = await _park(requester)

    response = await _approve_on_route(client, request_id)
    assert response.status_code == 200, response.text
    assert response.json()["dispatch_status"] == "ok", response.text

    # The waiting request and its request row: the requester, an agent login.
    assert (await _request(request_id)).proposed_effect["caller_kind"] == "service_agent"
    (asked,) = await _rows(path="approval.request")
    assert (asked.operator_sub, asked.caller_kind) == (requester.sub, "service_agent")
    # The run: the approver, who runs it with a person's token.
    (ran,) = [row for row in await _rows(path=_OP) if row.payload["result_status"] == "ok"]
    assert ran.operator_sub == "reviewer-1"
    assert ran.caller_kind == "personal_agent"


@pytest.mark.usefixtures("op")
async def test_a_step_that_waits_again_keeps_the_kind_of_the_requester() -> None:
    first = await _request(await _park(_requester("agent")))
    assert first.proposed_effect["caller_kind"] == "service_agent"
    reviewer = Operator(
        sub="reviewer-1",
        raw_jwt="<test-raw-jwt>",
        tenant_id=first.tenant_id,
        tenant_role=TenantRole.OPERATOR,
        token_checked=True,
    )
    params = {"path": "secret/next"}

    async with get_sessionmaker()() as session:
        second = await create_pending_request(
            session,
            operator=reviewer,
            connector_id=first.connector_id,
            op_id=_OP,
            target=None,
            params=params,
            params_hash=compute_params_hash(params),
            proposed_effect={"op_id": _OP, "safety_level": "caution"},
            requester=requester_of(first),
        )
        await session.commit()

    assert (await _request(second.id)).proposed_effect["caller_kind"] == "service_agent"


async def test_a_hosted_run_started_in_the_web_console_writes_service_agent(
    stub_embedding_service: AsyncMock,
) -> None:
    await _seed_definition()
    await _seed_echo_op(stub_embedding_service)
    key = make_rsa_keypair("kid-hosted-run-caller-kind")
    now = datetime.now(UTC)
    stored = DecryptedSession(
        id=uuid.uuid4(),
        operator_sub="person-web",
        tenant_id=_TENANT_A,
        created_at=now,
        expires_at=now + timedelta(hours=1),
        access_token=mint_token(key, sub="person-web", tenant_id=str(_TENANT_A)),
        refresh_token="not-a-real-refresh-token",
        last_seen_at=now,
    )
    with respx.mock as router:
        mock_discovery_and_jwks(router, public_jwks(key))
        _, person = await verify_access_token_with_refresh(stored, expected_audience=AUDIENCE)
    assert person.web_session is True

    call = ToolCallPart(
        "call_operation",
        {"connector_id": "vault-1.x", "op_id": "vault.kv.read", "params": {"path": "secret/foo"}},
    )
    groups = ToolCallPart("list_operation_groups", {"connector_id": "vault-1.x"})
    outcome = await _invoker(_scripted(groups, call)).run(person, "reader", "go")
    assert outcome.status is AgentRunStatus.SUCCEEDED

    rows = await _rows(agent_session_id=outcome.run_id)
    assert {row.path for row in rows} >= {
        "vault.kv.read",
        "/agent/tools/call/list_operation_groups",
    }
    assert {(row.operator_sub, row.actor_sub) for row in rows} == {("person-web", "agent:reader")}
    assert {row.caller_kind for row in rows} == {"service_agent"}


async def test_a_check_of_the_check_runner_writes_system(
    stub_embedding_service: AsyncMock,
) -> None:
    await _seed_definition()
    await _seed_echo_op(stub_embedding_service)
    snap = _SensorSnapshot(
        id=uuid.uuid4(),
        tenant_id=_TENANT_A,
        name="example-check",
        connector_id="vault-1.x",
        op_id="vault.kv.read",
        target=None,
        params={"path": "secret/foo"},
        assertion=_OK_ASSERTION,
        identity_sub="__sensor__",
    )

    await _run_evaluation(snap)

    rows = await _rows(path="vault.kv.read")
    assert [(row.operator_sub, row.caller_kind) for row in rows] == [("__sensor__", "system")]


async def test_a_call_stopped_at_shutdown_keeps_its_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    person = Operator(
        sub="person-token",
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_A,
        tenant_role=TenantRole.OPERATOR,
        token_checked=True,
    )
    shutdown_drain.reset_drain_state()
    entered, _release = _block_body(monkeypatch)
    running = asyncio.create_task(
        dispatcher.dispatch(
            operator=person, connector_id="demo-1.x", op_id="demo.slow", target=None, params={}
        )
    )
    try:
        await entered.wait()
        shutdown_drain.begin_drain()
        still_running = await shutdown_drain.wait_for_running_operations(time.monotonic() + 0.2)
        assert await shutdown_drain.record_unfinished_operations(still_running) == 1
    finally:
        running.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await running
        shutdown_drain.reset_drain_state()

    (row,) = await _rows(path="demo.slow")
    assert row.payload["result_status"] == "unfinished"
    assert (row.operator_sub, row.caller_kind) == ("person-token", "personal_agent")
