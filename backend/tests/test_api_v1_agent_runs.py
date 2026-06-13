# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.api.v1.agent_runs`.

Coverage matrix (Task #811 / G11.1-T4 acceptance criteria):

* Sync run blocks and returns the final output at 200; a run that exceeds
  the server-side timeout converts to async and returns a handle at 202.
* Async run (``async=true``) returns a handle at 202; polling the handle
  later returns the terminal status.
* SSE: ``POST /agents/{name}/run/events`` returns 200 + ``text/event-stream``
  and the body carries ``turn`` / ``final`` event frames.
* RBAC: ``read_only`` is rejected (403) on the run route; ``operator``
  passes.
* Tenant scoping: an absent agent name is 404; a poll of another tenant's
  run handle is 404 (existence is not leaked across tenants).
* A disabled definition is 409 on run.
* The run record is persisted durably (outlives the request).

The tests drive the production ``meho_backplane.main:app`` so the real
middleware chain (RequestContext -> Audit -> router) is exercised, with a
deterministic :class:`~meho_backplane.agent.run.AgentRun` injected via
:func:`~meho_backplane.agent.invocation.reset_agent_invoker_for_testing`
so no real LLM is hit.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any
from uuid import UUID

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from httpx import ASGITransport
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import select

from meho_backplane.agent.invocation import AgentInvoker, reset_agent_invoker_for_testing
from meho_backplane.agent.run import PydanticAgentRun
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentDefinition, Tenant
from meho_backplane.main import app
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair, mint_token, mock_discovery_and_jwks, public_jwks
from ._vault_fakes import install_fake_vault

_TENANT_A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_TENANT_B = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires + reset the lru cache."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()
    clear_jwks_cache()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()
    reset_agent_invoker_for_testing(None)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    install_fake_vault(monkeypatch)
    yield TestClient(app)


def _final_text(text: str) -> FunctionModel:
    """A model that answers immediately with *text* (no tool calls)."""

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(text)])

    return FunctionModel(fn)


def _install_invoker(text: str = "final answer") -> None:
    """Install a deterministic invoker over a no-tool model."""
    reset_agent_invoker_for_testing(
        AgentInvoker(runtime=PydanticAgentRun(model_factory=lambda: _final_text(text)))
    )


def _token(
    key: Any,
    *,
    sub: str = "op-user",
    role: TenantRole = TenantRole.OPERATOR,
    tenant_id: UUID = _TENANT_A,
) -> str:
    return mint_token(key, sub=sub, tenant_role=role.value, tenant_id=str(tenant_id))


async def _seed_tenants() -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        for tid, slug in ((_TENANT_A, "tenant-a"), (_TENANT_B, "tenant-b")):
            if await session.get(Tenant, tid) is None:
                session.add(Tenant(id=tid, slug=slug, name=f"Tenant {slug}"))
        await session.commit()


async def _seed_definition(
    *,
    name: str = "triage",
    tenant_id: UUID = _TENANT_A,
    enabled: bool = True,
) -> None:
    await _seed_tenants()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            AgentDefinition(
                tenant_id=tenant_id,
                name=name,
                identity_ref=f"agent:{name}",
                model_tier="standard",
                system_prompt="You triage incidents.",
                toolset={},
                turn_budget=5,
                output_schema=None,
                enabled=enabled,
                created_by_sub="seed-admin",
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_sync_run_returns_final_output(client: TestClient) -> None:
    """A short sync run blocks and returns the final output at 200."""
    await _seed_definition()
    _install_invoker("triaged: ok")
    key = make_rsa_keypair("kid-sync")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post(
            "/api/v1/agents/triage/run",
            json={"input": "what happened?"},
            headers={"Authorization": f"Bearer {_token(key)}"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "succeeded"
    assert body["output"] == {"text": "triaged: ok"}
    assert UUID(body["run_id"])


@respx.mock
@pytest.mark.asyncio
async def test_async_run_returns_handle_then_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    """async=true returns a 202 handle; polling it later shows the terminal state.

    Driven over an httpx ``ASGITransport`` rather than the sync
    :class:`TestClient` so the POST and the polls share one event loop —
    the durability contract (the background run outlives the request that
    started it) only holds when the run's loop is the persistent app loop.
    The sync ``TestClient`` runs each request in its own short-lived portal,
    which would tear the background task down on the POST's return.
    """
    install_fake_vault(monkeypatch)
    await _seed_definition()
    _install_invoker("async result")
    key = make_rsa_keypair("kid-async")
    mock_discovery_and_jwks(respx.mock, public_jwks(key))
    headers = {"Authorization": f"Bearer {_token(key)}"}

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://testserver",
    ) as ac:
        resp = await ac.post(
            "/api/v1/agents/triage/run",
            json={"input": "go", "async": True},
            headers=headers,
        )
        assert resp.status_code == 202, resp.text
        handle = resp.json()["run_id"]

        body: dict[str, Any] = {}
        for _ in range(100):
            poll = await ac.get(f"/api/v1/agents/runs/{handle}", headers=headers)
            assert poll.status_code == 200, poll.text
            body = poll.json()
            if body["status"] == "succeeded":
                break
            await asyncio.sleep(0.02)

    assert body["run_id"] == handle
    assert body["status"] == "succeeded"
    assert body["output"] == {"text": "async result"}


@pytest.mark.asyncio
async def test_sync_run_converts_to_async_on_timeout(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sync run past the server-side timeout returns a 202 handle."""
    await _seed_definition()
    _install_invoker("eventually")
    monkeypatch.setenv("AGENT_SYNC_TIMEOUT_SECONDS", "0.0000001")
    get_settings.cache_clear()
    key = make_rsa_keypair("kid-timeout")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post(
            "/api/v1/agents/triage/run",
            json={"input": "go"},
            headers={"Authorization": f"Bearer {_token(key)}"},
        )
    assert resp.status_code == 202, resp.text
    assert resp.json()["converted_to_async"] is True


@pytest.mark.asyncio
async def test_list_runs_filters_by_work_ref_from_header(client: TestClient) -> None:
    """A run launched with the Meho-Work-Ref header is findable via ?work_ref.

    Exercises the full boundary: the chassis binds ``work_ref_var`` from the
    inbound header, ``_create_run_row`` stamps it onto the ``agent_run`` row,
    and ``GET /agents/runs?work_ref=`` returns only the matching run
    (work_ref I3-T2 #1662).
    """
    await _seed_definition()
    _install_invoker("triaged: ok")
    key = make_rsa_keypair("kid-list")
    headers = {"Authorization": f"Bearer {_token(key)}"}
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        # One run tagged with a change ticket, one without.
        tagged = client.post(
            "/api/v1/agents/triage/run",
            json={"input": "go"},
            headers={**headers, "Meho-Work-Ref": "gh:evoila/meho#11"},
        )
        assert tagged.status_code == 200, tagged.text
        tagged_run_id = tagged.json()["run_id"]
        untagged = client.post(
            "/api/v1/agents/triage/run",
            json={"input": "go"},
            headers=headers,
        )
        assert untagged.status_code == 200, untagged.text

        filtered = client.get(
            "/api/v1/agents/runs",
            params={"work_ref": "gh:evoila/meho#11"},
            headers=headers,
        )
        assert filtered.status_code == 200, filtered.text
        rows = filtered.json()
        assert [row["run_id"] for row in rows] == [tagged_run_id]
        assert rows[0]["work_ref"] == "gh:evoila/meho#11"

        # No filter returns both runs.
        unfiltered = client.get("/api/v1/agents/runs", headers=headers)
        assert unfiltered.status_code == 200, unfiltered.text
        assert len(unfiltered.json()) == 2


@pytest.mark.asyncio
async def test_list_runs_is_tenant_isolated(client: TestClient) -> None:
    """The agent-run list never returns another tenant's runs."""
    await _seed_definition(name="triage", tenant_id=_TENANT_A)
    await _seed_definition(name="triage", tenant_id=_TENANT_B)
    _install_invoker("triaged: ok")
    key = make_rsa_keypair("kid-iso")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        # Tenant B runs an agent under the same work_ref.
        b_headers = {
            "Authorization": f"Bearer {_token(key, sub='op-b', tenant_id=_TENANT_B)}",
            "Meho-Work-Ref": "gh:evoila/meho#11",
        }
        b_run = client.post(
            "/api/v1/agents/triage/run",
            json={"input": "go"},
            headers=b_headers,
        )
        assert b_run.status_code == 200, b_run.text

        # Tenant A lists with the same work_ref filter -- must see nothing.
        a_headers = {"Authorization": f"Bearer {_token(key, sub='op-a', tenant_id=_TENANT_A)}"}
        a_list = client.get(
            "/api/v1/agents/runs",
            params={"work_ref": "gh:evoila/meho#11"},
            headers=a_headers,
        )
        assert a_list.status_code == 200, a_list.text
        assert a_list.json() == []


@pytest.mark.asyncio
async def test_run_events_streams_sse(client: TestClient) -> None:
    """The SSE events route returns text/event-stream with turn + final frames."""
    await _seed_definition()
    _install_invoker("streamed answer")
    key = make_rsa_keypair("kid-sse")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        with client.stream(
            "POST",
            "/api/v1/agents/triage/run/events",
            json={"input": "go"},
            headers={"Authorization": f"Bearer {_token(key)}"},
        ) as resp:
            assert resp.status_code == 200, resp.read()
            assert resp.headers["content-type"].startswith("text/event-stream")
            body = "".join(chunk for chunk in resp.iter_text())

    assert "event: turn" in body
    assert "event: final" in body
    assert "streamed answer" in body


@pytest.mark.asyncio
async def test_read_only_rejected_on_run(client: TestClient) -> None:
    """A read_only operator is rejected (403) on the run route."""
    await _seed_definition()
    _install_invoker()
    key = make_rsa_keypair("kid-ro")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post(
            "/api/v1/agents/triage/run",
            json={"input": "go"},
            headers={"Authorization": f"Bearer {_token(key, role=TenantRole.READ_ONLY)}"},
        )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_unknown_agent_is_404(client: TestClient) -> None:
    """Running an absent agent name returns 404 agent_not_found."""
    await _seed_tenants()
    _install_invoker()
    key = make_rsa_keypair("kid-404")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post(
            "/api/v1/agents/nope/run",
            json={"input": "go"},
            headers={"Authorization": f"Bearer {_token(key)}"},
        )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "agent_not_found"


@pytest.mark.asyncio
async def test_disabled_agent_is_409(client: TestClient) -> None:
    """Running a disabled agent returns 409 agent_disabled."""
    await _seed_definition(enabled=False)
    _install_invoker()
    key = make_rsa_keypair("kid-409")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post(
            "/api/v1/agents/triage/run",
            json={"input": "go"},
            headers={"Authorization": f"Bearer {_token(key)}"},
        )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "agent_disabled"


@pytest.mark.asyncio
async def test_cross_tenant_run_handle_is_404(client: TestClient) -> None:
    """Tenant B cannot poll tenant A's run handle (404, no existence leak)."""
    await _seed_definition(tenant_id=_TENANT_A)
    _install_invoker("x")
    key = make_rsa_keypair("kid-xtenant")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        # A *sync* run completes inside the request (no background task), so
        # the durable row exists by the time tenant B probes it.
        started = client.post(
            "/api/v1/agents/triage/run",
            json={"input": "go"},
            headers={"Authorization": f"Bearer {_token(key, tenant_id=_TENANT_A)}"},
        )
        handle = started.json()["run_id"]

        poll = client.get(
            f"/api/v1/agents/runs/{handle}",
            headers={"Authorization": f"Bearer {_token(key, sub='op-b', tenant_id=_TENANT_B)}"},
        )
    assert poll.status_code == 404
    assert poll.json()["detail"] == "agent_run_not_found"


@pytest.mark.asyncio
async def test_unknown_run_handle_is_404(client: TestClient) -> None:
    """Polling an unknown run handle returns 404."""
    await _seed_tenants()
    _install_invoker()
    key = make_rsa_keypair("kid-unknown")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.get(
            "/api/v1/agents/runs/00000000-0000-0000-0000-000000000000",
            headers={"Authorization": f"Bearer {_token(key)}"},
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_run_record_is_persisted(client: TestClient) -> None:
    """A sync run persists a durable agent_run row that outlives the request."""
    from meho_backplane.db.models import AgentRun as AgentRunRow

    await _seed_definition()
    _install_invoker("persisted")
    key = make_rsa_keypair("kid-persist")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post(
            "/api/v1/agents/triage/run",
            json={"input": "go"},
            headers={"Authorization": f"Bearer {_token(key)}"},
        )
    handle = UUID(resp.json()["run_id"])

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(select(AgentRunRow).where(AgentRunRow.id == handle))
        row = result.scalar_one()
    assert row.tenant_id == _TENANT_A
    assert row.status == "succeeded"


# ---------------------------------------------------------------------------
# G11.5-T6 #1080 -- pre-execution budget gate contract on the REST boundary
# ---------------------------------------------------------------------------
#
# The four contract surfaces the gate maps onto (REST 429, MCP -32602,
# scheduler scheduler_invoke_refused, SSE pre-stream 429) are tested in
# their respective suites; the two below pin the REST shapes (sync run +
# SSE pre-stream). The global kill switch is the simplest deterministic
# trigger -- no DB seeding, no per-window arithmetic; the gate refuses
# every run inside ``_enforce_pre_run_budget`` and the surface maps the
# resulting :class:`BudgetExceededError` onto the documented HTTP shape.


@pytest.mark.asyncio
async def test_rest_run_returns_429_when_budget_exceeded_pre_execution(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /agents/{name}/run returns 429 + structured ``budget_exceeded`` body.

    Contract (G11.5-T6 #1080): when the pre-execution budget gate
    refuses, the REST surface raises ``HTTPException(429, detail={...})``
    so FastAPI emits ``{"detail": {"error": "budget_exceeded",
    "reason": <str>}}``. The status code distinguishes a quota refusal
    from 4xx auth / 4xx not-found / 4xx disabled (all of which use a
    plain-string detail), and the structured body carries the gate's
    reason so clients can log + back off intelligently.
    """
    await _seed_definition()
    _install_invoker()
    monkeypatch.setenv("AGENT_RUNS_DISABLED_GLOBAL", "true")
    get_settings.cache_clear()

    key = make_rsa_keypair("kid-budget-429")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post(
            "/api/v1/agents/triage/run",
            json={"input": "go"},
            headers={"Authorization": f"Bearer {_token(key)}"},
        )
    assert resp.status_code == 429, resp.text
    body = resp.json()
    assert isinstance(body["detail"], dict), body
    assert body["detail"]["error"] == "budget_exceeded"
    assert "global kill switch" in body["detail"]["reason"]


@pytest.mark.asyncio
async def test_rest_sse_returns_429_pre_stream_when_budget_exceeded(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /agents/{name}/run/events refuses with 429 *before* opening the SSE stream.

    Contract (G11.5-T6 #1080): the SSE route calls
    :meth:`AgentInvoker.ensure_runnable` before yielding the streaming
    response. A budget refusal there must surface as a normal 4xx
    (``Content-Type`` is JSON, not ``text/event-stream``) so an
    :class:`EventSource` client does not auto-reconnect into a hot loop
    on a torn stream. The body shape mirrors the sync-run contract:
    ``{"detail": {"error": "budget_exceeded", "reason": <str>}}``.
    """
    await _seed_definition()
    _install_invoker()
    monkeypatch.setenv("AGENT_RUNS_DISABLED_GLOBAL", "true")
    get_settings.cache_clear()

    key = make_rsa_keypair("kid-budget-sse-429")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post(
            "/api/v1/agents/triage/run/events",
            json={"input": "go"},
            headers={"Authorization": f"Bearer {_token(key)}"},
        )
    assert resp.status_code == 429, resp.text
    # Crucially NOT text/event-stream -- the EventSource auto-reconnect
    # hot-loop guard relies on the surface returning a clean HTTP error
    # before the StreamingResponse is constructed.
    assert not resp.headers["content-type"].startswith("text/event-stream")
    body = resp.json()
    assert isinstance(body["detail"], dict), body
    assert body["detail"]["error"] == "budget_exceeded"
    assert "global kill switch" in body["detail"]["reason"]
