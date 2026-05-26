# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the agent-invocation audit row (G11.4-T5 #1074).

Acceptance criteria covered:

* ``test_agent_session_id_propagates_to_dispatcher_audit_rows`` -- the
  load-bearing AC: per-tool-call dispatcher audit rows fired from
  inside an agent loop are keyed by the run's id on
  ``audit_log.agent_session_id``.
* ``test_agent_model_provider_meta_lands_on_audit_payload`` -- the
  ``model`` / ``provider`` meta is forensically attached to every
  per-tool-call audit row's payload, so a consumer reading one row
  can attribute "which model said this" without joining ``agent_run``.
* ``test_audit_session_id_not_set_outside_agent_run`` -- the
  contextvar discipline: a top-level HTTP / MCP dispatch (no agent
  loop in scope) leaves ``agent_session_id`` NULL on the audit row,
  exactly the chassis-era contract.
* ``test_reconstruct_sense_replay_still_works_for_agent_rows`` -- the
  G8.2 #1011 recursive-CTE :func:`replay_session` reconstruct sense
  is unchanged: an agent run's per-tool-call rows surface under their
  session id with the loop's per-turn ordering. No regression.

The tests exercise the dispatcher's audit-write path directly by
binding the new contextvars (the same shape the agent invoker uses)
and seeding a typed operation; this isolates the propagation
contract from the LLM seam's framework dependency.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.audit_query import replay_session
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, OperationResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.operations import (
    AgentRunAuditMeta,
    agent_run_audit_meta_var,
    agent_session_id_var,
    dispatch,
    register_typed_operation,
    reset_dispatcher_caches,
)
from meho_backplane.retrieval.embedding import EMBEDDING_DIMENSION
from meho_backplane.settings import get_settings

pytestmark = pytest.mark.asyncio


_TENANT_A = uuid.UUID("11111111-1111-1111-1111-111111111111")


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the env vars :class:`Settings` requires."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_state() -> Iterator[None]:
    """Clear connector + dispatcher caches around each test."""
    clear_registry()
    reset_dispatcher_caches()
    yield
    clear_registry()
    reset_dispatcher_caches()


@pytest.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    """One :class:`AsyncSession` per test for direct row inspection."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    """Embedding stub for the typed-op descriptor's embedding column.

    Mirrors the agent-runtime tests' fixture verbatim -- the value is
    irrelevant (no retrieval runs) but the shape must match the
    :class:`~meho_backplane.retrieval.embedding.EmbeddingService` ABC.
    """
    service = AsyncMock()
    service.encode_one.return_value = [0.0] * EMBEDDING_DIMENSION
    service.encode.return_value = [[0.0] * EMBEDDING_DIMENSION]
    service.dimension = EMBEDDING_DIMENSION
    return service


def _operator() -> Operator:
    """Test operator with the operator role in tenant A."""
    return Operator(
        sub="alice@example.com",
        name="Alice",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_A,
        tenant_role=TenantRole.OPERATOR,
    )


class _StubConnector(Connector):
    """A connector class registered so dispatch lookups succeed.

    The typed-op handler the tests dispatch through
    (:func:`_echo_handler`) is a module-level function; the connector
    is only needed so the dispatcher's resolver lookup finds *some*
    class for the ``(product, version, impl_id)`` triple.
    """

    product = "stub"
    version = "1.x"
    impl_id = "stub"

    async def fingerprint(self, target: Any) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(  # type: ignore[override]
        self,
        target: Any,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        raise NotImplementedError


async def _echo_handler(
    operator: Operator,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Typed handler: return the params verbatim."""
    return {"ok": True, "echo": params, "operator_sub": operator.sub}


async def _seed_typed_op(stub_embedding_service: AsyncMock) -> None:
    """Register the stub connector + a typed op the dispatcher can find."""
    register_connector_v2(product="stub", version="", impl_id="", cls=_StubConnector)
    await register_typed_operation(
        product="stub",
        version="1.x",
        impl_id="stub",
        op_id="stub.tool_call",
        handler=_echo_handler,
        summary="Stub tool call for agent-audit tests.",
        description="Echo params; used to assert the audit row's agent_session_id.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=stub_embedding_service,
    )


# ---------------------------------------------------------------------------
# agent_session_id propagation
# ---------------------------------------------------------------------------


async def test_agent_session_id_propagates_to_dispatcher_audit_rows(
    db_session: AsyncSession,
    stub_embedding_service: AsyncMock,
) -> None:
    """A dispatch fired inside an agent loop is keyed by the run id.

    The load-bearing acceptance criterion: per-tool-call audit rows
    written by the dispatcher carry the run's ``agent_session_id``,
    so the G8.2-T3 #1011 :func:`replay_session` can reconstruct the
    full session graph by that key.
    """
    await _seed_typed_op(stub_embedding_service)
    operator = _operator()
    run_id = uuid.uuid4()

    # Mirror the AgentInvoker's wiring: bind the session contextvar
    # around the dispatch call; the meta is a separate contextvar.
    session_token = agent_session_id_var.set(run_id)
    meta_token = agent_run_audit_meta_var.set(
        AgentRunAuditMeta(model="claude-sonnet-4-5", provider="anthropic"),
    )
    try:
        result = await dispatch(
            operator=operator,
            connector_id="stub-1.x",
            op_id="stub.tool_call",
            target=None,
            params={"q": "hello"},
        )
    finally:
        agent_run_audit_meta_var.reset(meta_token)
        agent_session_id_var.reset(session_token)

    assert result.status == "ok"

    # The audit row landed under the session id.
    stmt = select(AuditLog).where(AuditLog.agent_session_id == run_id)
    rows = (await db_session.scalars(stmt)).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.agent_session_id == run_id
    assert row.operator_sub == operator.sub
    assert row.path == "stub.tool_call"


async def test_agent_model_provider_meta_lands_on_audit_payload(
    db_session: AsyncSession,
    stub_embedding_service: AsyncMock,
) -> None:
    """The model+provider attribution is recorded on every per-tool-call row.

    Per the acceptance criteria: "per-tool-call ... model+provider, and
    cost on its audit rows". The meta lives in the row's JSON
    ``payload`` (not a dedicated column) -- minimal migration footprint;
    same pattern the dispatcher uses for ``redaction_policy_id``.
    """
    await _seed_typed_op(stub_embedding_service)
    operator = _operator()
    run_id = uuid.uuid4()

    session_token = agent_session_id_var.set(run_id)
    meta_token = agent_run_audit_meta_var.set(
        AgentRunAuditMeta(
            model="claude-sonnet-4-5",
            provider="anthropic",
            cost="0.00321",
        ),
    )
    try:
        await dispatch(
            operator=operator,
            connector_id="stub-1.x",
            op_id="stub.tool_call",
            target=None,
            params={"q": "hello"},
        )
    finally:
        agent_run_audit_meta_var.reset(meta_token)
        agent_session_id_var.reset(session_token)

    row = await db_session.scalar(
        select(AuditLog).where(AuditLog.agent_session_id == run_id),
    )
    assert row is not None
    payload = row.payload
    assert isinstance(payload, dict)
    assert payload["agent_model"] == "claude-sonnet-4-5"
    assert payload["agent_provider"] == "anthropic"
    assert payload["agent_cost"] == "0.00321"
    # The session id is also mirrored into the JSON payload so the
    # broadcast-event surface (which serialises ``payload``) carries
    # the same attribution as the audit-row column.
    assert payload["agent_session_id"] == str(run_id)


async def test_audit_session_id_not_set_outside_agent_run(
    db_session: AsyncSession,
    stub_embedding_service: AsyncMock,
) -> None:
    """A top-level dispatch (no agent in scope) leaves the column NULL.

    Defense against contextvar leakage: a dispatch fired outside an
    agent loop must not stamp some prior run's session id. The
    chassis HTTP / MCP dispatch path produces ``agent_session_id=NULL``
    -- the v0.1 contract -- unless the agent invoker explicitly bound
    the contextvar.
    """
    await _seed_typed_op(stub_embedding_service)
    operator = _operator()

    # No contextvar binding here.
    await dispatch(
        operator=operator,
        connector_id="stub-1.x",
        op_id="stub.tool_call",
        target=None,
        params={"q": "hello"},
    )

    rows = (await db_session.scalars(select(AuditLog))).all()
    assert len(rows) == 1
    assert rows[0].agent_session_id is None
    assert rows[0].payload.get("agent_model") is None  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Reconstruct-sense replay (#1011) still works for agent rows
# ---------------------------------------------------------------------------


async def test_reconstruct_sense_replay_still_works_for_agent_rows(
    db_session: AsyncSession,
    stub_embedding_service: AsyncMock,
) -> None:
    """G8.2-T3 #1011 :func:`replay_session` reconstructs an agent's session.

    Acceptance criterion: "Reconstruct-sense replay (#1011) still
    works for agent rows (no regression)". Dispatch two ops under
    the same session id; replay must return the full set as a flat
    forest (no parent_audit_id linkage between them in this minimal
    test, so each is a root), ordered chronologically.
    """
    await _seed_typed_op(stub_embedding_service)
    operator = _operator()
    run_id = uuid.uuid4()

    session_token = agent_session_id_var.set(run_id)
    meta_token = agent_run_audit_meta_var.set(
        AgentRunAuditMeta(model="claude-sonnet-4-5", provider="anthropic"),
    )
    try:
        await dispatch(
            operator=operator,
            connector_id="stub-1.x",
            op_id="stub.tool_call",
            target=None,
            params={"q": "first"},
        )
        await dispatch(
            operator=operator,
            connector_id="stub-1.x",
            op_id="stub.tool_call",
            target=None,
            params={"q": "second"},
        )
    finally:
        agent_run_audit_meta_var.reset(meta_token)
        agent_session_id_var.reset(session_token)

    # Same session and tenant the rows were written under.
    roots = await replay_session(run_id, tenant_id=_TENANT_A, session=db_session)
    assert len(roots) == 2
    # Two top-level dispatches in the same session -- both surface
    # under the session id, each as a root with no children.
    assert all(node.agent_session_id == run_id for node in roots)
    assert all(node.children == [] for node in roots)
    # Chronological order: the "first" dispatch precedes "second".
    assert roots[0].ts <= roots[1].ts
