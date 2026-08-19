# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""End-to-end ingest -> drain -> matcher -> agent run (#2881, criterion 3).

A signed external event POSTed to ``/api/v1/events/ingest/{slug}`` lands on
``event_outbox``; one drain tick matches it against a ``kind=event`` trigger
(#2878) whose ``event_filter`` the ingest envelope contains, and fires an
agent run through the shared ``run_scheduled`` seam -- no real LLM (a
``FunctionModel`` stands in), no real Keycloak (the token + verify seams are
stubbed the same way the matcher's own tests stub them).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
import uuid
from collections.abc import Iterator
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import select

from meho_backplane.agent.invocation import AgentInvoker
from meho_backplane.agent.run import PydanticAgentRun
from meho_backplane.agents.schemas import AgentDefinitionCreate, AgentModelTier
from meho_backplane.agents.service import AgentDefinitionService
from meho_backplane.api.v1.events_ingest import router
from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentPrincipal, AgentRun, EventOutbox, Tenant
from meho_backplane.events.drain import run_one_drain_tick
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.scheduler.repository import create_event_trigger
from meho_backplane.settings import get_settings

from ._event_source_helpers import (
    _insert_event_source,
    _settings_env,  # noqa: F401  (autouse fixture)
)

_TENANT = uuid.UUID("33333333-3333-3333-3333-333333333333")
_SECRET_VALUE = "e2e-hmac-key"


@pytest.fixture(autouse=True)
def _agent_secret_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # identity_ref ``agent:reactor`` -> ``AGENT_REACTOR`` client-credentials env.
    monkeypatch.setenv("MEHO_AGENT_SECRET_AGENT_REACTOR", "test-secret")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _stub_autonomous_auth(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Stub ``run_scheduled``'s Keycloak token + JWT-verify seams (offline)."""
    monkeypatch.setattr(
        "meho_backplane.agent.invocation.get_client_credentials_token",
        AsyncMock(return_value="agent-token"),
    )
    monkeypatch.setattr(
        "meho_backplane.agent.invocation.verify_jwt_for_audience",
        AsyncMock(
            return_value=Operator(
                sub="agent-reactor",
                name=None,
                email=None,
                raw_jwt="agent-token",
                tenant_id=_TENANT,
                tenant_role=TenantRole.OPERATOR,
            ),
        ),
    )
    yield


@pytest.fixture(autouse=True)
def _stub_ingest_seams(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    async def _read_secret(_operator: object, _ref: str) -> SecretStr:
        return SecretStr(_SECRET_VALUE)

    async def _no_op(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(
        "meho_backplane.events.ingest.service.read_event_source_secret", _read_secret
    )
    monkeypatch.setattr("meho_backplane.events.ingest.service.enforce_ingest_rate_limit", _no_op)
    monkeypatch.setattr("meho_backplane.events.ingest.service.publish_event", _no_op)
    yield


def _final_text(text: str) -> FunctionModel:
    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(text)])

    return FunctionModel(fn)


def _make_invoker() -> AgentInvoker:
    return AgentInvoker(runtime=PydanticAgentRun(model_factory=lambda: _final_text("handled")))


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(router)
    return app


async def _seed_tenant_agent_trigger_source() -> None:
    sm = get_sessionmaker()
    async with sm() as session:
        if await session.get(Tenant, _TENANT) is None:
            session.add(Tenant(id=_TENANT, slug="tenant-e2e", name="Tenant E2E"))
            await session.commit()
        session.add(
            AgentPrincipal(
                id=uuid.uuid4(),
                tenant_id=_TENANT,
                name="reactor",
                keycloak_client_id="agent:reactor",
                keycloak_internal_id="kc-internal-reactor",
                owner_sub="seed-admin",
                revoked=False,
                created_by_sub="seed-admin",
            )
        )
        await session.commit()

    entry = await AgentDefinitionService().create(
        tenant_id=_TENANT,
        created_by_sub="seed-admin",
        payload=AgentDefinitionCreate(
            name="reactor",
            identity_ref="agent:reactor",
            model_tier=AgentModelTier.STANDARD,
            system_prompt="You react to ingested events.",
            toolset={},
            turn_budget=2,
            enabled=True,
        ),
    )
    async with sm() as session:
        await create_event_trigger(
            session,
            tenant_id=_TENANT,
            agent_definition_id=entry.id,
            event_filter={"source": {"slug": "e2e-src"}},
            inputs={"prompt": "react"},
            identity_sub="__scheduler__",
            created_by_sub="seed-admin",
            in_flight_policy="fail_into_audit",
        )
        await session.commit()

    await _insert_event_source(
        tenant_id=_TENANT,
        slug="e2e-src",
        name="e2e-src",
        kind="alertmanager",
        auth_strategy="hmac-sha256",
        secret_ref=f"tenants/{_TENANT}/event-sources/e2e-src",
        status="active",
    )


def _signed(body: bytes) -> dict[str, str]:
    ts = f"{time.time()}"
    sig = hmac.new(_SECRET_VALUE.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return {"X-Meho-Signature": sig, "X-Meho-Timestamp": ts}


async def _all_runs() -> list[AgentRun]:
    async with get_sessionmaker()() as session:
        return list((await session.execute(select(AgentRun))).scalars().all())


async def _wait_for_runs(expected: int, *, timeout: float = 3.0) -> list[AgentRun]:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        runs = await _all_runs()
        if len(runs) >= expected:
            return runs
        await asyncio.sleep(0.05)
    return await _all_runs()


@pytest.mark.asyncio
async def test_posted_event_matches_trigger_and_fires_agent_run() -> None:
    await _seed_tenant_agent_trigger_source()

    body = json.dumps({"type": "deploy", "severity": "critical"}).encode()
    client = TestClient(_build_app())
    resp = client.post("/api/v1/events/ingest/e2e-src", content=body, headers=_signed(body))
    assert resp.status_code == 202
    event_id = resp.json()["event_id"]

    # One drain tick routes the outbox row through the #2878 matcher.
    processed = await run_one_drain_tick(invoker=_make_invoker())
    assert processed == 1

    async with get_sessionmaker()() as session:
        outbox = await session.get(EventOutbox, event_id)
        assert outbox is not None
        assert outbox.processed_at is not None

    runs = await _wait_for_runs(1)
    assert len(runs) == 1
    assert runs[0].tenant_id == _TENANT
    assert runs[0].work_ref.startswith(f"event:{event_id}:")
