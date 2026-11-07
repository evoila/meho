# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A finished agent run records its tokens; a tenant price prices unknown models (#5853).

Drives :class:`~meho_backplane.agent.invocation.AgentInvoker` with a
:class:`~pydantic_ai.models.function.FunctionModel` that reports a known
usage, and checks the durable ``agent_run`` row and the person's budget rows:

* A succeeded run stores its four token counts. A failed run stores none.
* A run on a model MEHO has no price for uses the tenant's price: the run
  gets a cost, and the three budget rows grow by it.
* The tenant price matches the full id (``provider:model``) or the model
  name alone.
* With no tenant price, the run costs 0 and MEHO logs the same warning as
  before.
* A model in ``MODEL_PRICING`` keeps MEHO's price, whatever the tenant row
  holds. Another tenant's price is never used. A broken stored entry counts
  as no price.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
import structlog
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage
from sqlalchemy import select

from meho_backplane.agent.invocation import AgentInvoker
from meho_backplane.agent.run import PydanticAgentRun
from meho_backplane.agents.schemas import AgentDefinitionCreate, AgentModelTier
from meho_backplane.agents.service import AgentDefinitionService
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentPrincipal, AgentRunStatus, IdentityBudget, Tenant
from meho_backplane.db.models import AgentRun as AgentRunRow
from meho_backplane.operations import identity_budget, reset_dispatcher_caches
from meho_backplane.operations.identity_budget import MODEL_PRICING
from meho_backplane.retrieval.embedding import EMBEDDING_DIMENSION
from meho_backplane.settings import get_settings

pytestmark = pytest.mark.asyncio

_TENANT = UUID("44444444-4444-4444-4444-44444444aaaa")
_OTHER_TENANT = UUID("55555555-5555-5555-5555-55555555bbbb")
_OPEN_MODEL = "openai:open-model-test"
_AGENT = "price-reader"


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("AGENT_DEFAULT_MODEL", _OPEN_MODEL)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    clear_registry()
    reset_dispatcher_caches()
    # The miss warning is logged once per process and model; start clean.
    monkeypatch.setattr(identity_budget, "_PRICING_MISS_LOGGED", set())
    yield
    clear_registry()
    reset_dispatcher_caches()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * EMBEDDING_DIMENSION
    service.encode.return_value = [[0.1] * EMBEDDING_DIMENSION]
    service.dimension = EMBEDDING_DIMENSION
    return service


def _operator(sub: str) -> Operator:
    return Operator(
        sub=sub,
        name="Agent Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
    )


async def _seed(
    prices: dict[str, object] | None = None,
    other_tenant_prices: dict[str, object] | None = None,
) -> None:
    async with get_sessionmaker()() as session:
        session.add(Tenant(id=_TENANT, slug="tenant-price", name="T", agent_model_prices=prices))
        session.add(
            Tenant(
                id=_OTHER_TENANT,
                slug="tenant-other",
                name="O",
                agent_model_prices=other_tenant_prices,
            )
        )
        session.add(
            AgentPrincipal(
                id=uuid4(),
                tenant_id=_TENANT,
                name=_AGENT,
                keycloak_client_id=f"agent:{_AGENT}",
                keycloak_internal_id=f"kc-internal-{_AGENT}",
                owner_sub="seed-admin",
                revoked=False,
                created_by_sub="seed-admin",
            )
        )
        await session.commit()
    await AgentDefinitionService().create(
        tenant_id=_TENANT,
        created_by_sub="seed-admin",
        payload=AgentDefinitionCreate(
            name=_AGENT,
            identity_ref=f"agent:{_AGENT}",
            model_tier=AgentModelTier.STANDARD,
            system_prompt="You are a price test.",
            toolset={},
            turn_budget=5,
        ),
    )


def _model(*, input_tokens: int, output_tokens: int, cache_read_tokens: int = 0) -> FunctionModel:
    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(
            parts=[TextPart("done")],
            usage=RequestUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read_tokens,
            ),
        )

    return FunctionModel(fn)


async def _run(sub: str, model: FunctionModel) -> AgentRunRow:
    invoker = AgentInvoker(runtime=PydanticAgentRun(model_factory=lambda: model))
    outcome = await invoker.run(_operator(sub), _AGENT, "go")
    async with get_sessionmaker()() as session:
        return (
            await session.execute(select(AgentRunRow).where(AgentRunRow.id == outcome.run_id))
        ).scalar_one()


async def _buckets(sub: str) -> list[IdentityBudget]:
    async with get_sessionmaker()() as session:
        return list(
            (
                await session.execute(
                    select(IdentityBudget).where(IdentityBudget.principal_sub == sub)
                )
            )
            .scalars()
            .all()
        )


def _cost(price_in: str, price_out: str, input_tokens: int, output_tokens: int) -> Decimal:
    return (Decimal(price_in) * input_tokens + Decimal(price_out) * output_tokens) / Decimal(
        1_000_000
    )


async def test_succeeded_run_stores_its_token_counts(stub_embedding_service: AsyncMock) -> None:
    await _seed()
    row = await _run(
        "tokens-1", _model(input_tokens=1_200, output_tokens=300, cache_read_tokens=200)
    )
    assert row.status == AgentRunStatus.SUCCEEDED.value
    # The framework counts cached tokens inside input_tokens; the row does not.
    assert row.input_tokens == 1_000
    assert row.cache_read_tokens == 200
    assert row.cache_write_tokens == 0
    assert row.output_tokens == 300


async def test_failed_run_stores_no_token_counts(stub_embedding_service: AsyncMock) -> None:
    await _seed()

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(
            parts=[ToolCallPart("nonexistent", {})],
            usage=RequestUsage(input_tokens=100, output_tokens=10),
        )

    row = await _run("tokens-failed", FunctionModel(fn))
    assert row.status == AgentRunStatus.FAILED.value
    assert row.input_tokens is None
    assert row.output_tokens is None
    assert row.cache_read_tokens is None
    assert row.cache_write_tokens is None


async def test_tenant_price_gives_an_unknown_model_a_cost(
    stub_embedding_service: AsyncMock,
) -> None:
    await _seed(prices={_OPEN_MODEL: {"input": "0.40", "output": "1.60"}})
    sub = "price-full-id"
    row = await _run(sub, _model(input_tokens=10_000, output_tokens=2_000))

    expected = _cost("0.40", "1.60", 10_000, 2_000)
    assert expected > 0
    assert row.cost == expected
    buckets = await _buckets(sub)
    assert {b.window_kind for b in buckets} == {"daily", "weekly", "monthly"}
    for bucket in buckets:
        assert bucket.cost_consumed == expected
        assert bucket.tokens_consumed == Decimal(12_000)


async def test_cached_tokens_cost_the_tenant_input_price(
    stub_embedding_service: AsyncMock,
) -> None:
    await _seed(prices={_OPEN_MODEL: {"input": "1", "output": "2"}})
    row = await _run(
        "price-cached",
        _model(input_tokens=3_000, output_tokens=1_000, cache_read_tokens=2_000),
    )
    assert row.input_tokens == 1_000
    assert row.cache_read_tokens == 2_000
    assert row.cost == _cost("1", "2", 3_000, 1_000)


async def test_tenant_price_matches_the_model_name_alone(
    stub_embedding_service: AsyncMock,
) -> None:
    await _seed(prices={"open-model-test": {"input": "1", "output": "2"}})
    row = await _run("price-bare-name", _model(input_tokens=1_000, output_tokens=1_000))
    assert row.cost == _cost("1", "2", 1_000, 1_000)


async def test_no_price_costs_zero_and_still_warns(stub_embedding_service: AsyncMock) -> None:
    await _seed()
    sub = "price-none"
    with structlog.testing.capture_logs() as logs:
        row = await _run(sub, _model(input_tokens=1_000, output_tokens=1_000))
    assert row.cost == Decimal(0)
    misses = [e for e in logs if e["event"] == "identity_budget_pricing_miss"]
    assert [e["model_id"] for e in misses] == [_OPEN_MODEL]
    for bucket in await _buckets(sub):
        assert bucket.cost_consumed == Decimal(0)


async def test_other_tenants_price_is_not_used(stub_embedding_service: AsyncMock) -> None:
    await _seed(other_tenant_prices={_OPEN_MODEL: {"input": "5", "output": "5"}})
    row = await _run("price-other-tenant", _model(input_tokens=1_000, output_tokens=1_000))
    assert row.cost == Decimal(0)


async def test_broken_stored_price_counts_as_no_price(stub_embedding_service: AsyncMock) -> None:
    await _seed(prices={_OPEN_MODEL: {"input": "not-a-number", "output": "1"}})
    with structlog.testing.capture_logs() as logs:
        row = await _run("price-broken", _model(input_tokens=1_000, output_tokens=1_000))
    assert row.cost == Decimal(0)
    events = {e["event"] for e in logs}
    assert "tenant_model_price_unreadable" in events
    assert "identity_budget_pricing_miss" in events


async def test_meho_price_wins_over_a_stored_tenant_price(
    stub_embedding_service: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    known = "anthropic:claude-sonnet-5-5"
    monkeypatch.setenv("AGENT_DEFAULT_MODEL", known)
    get_settings.cache_clear()
    await _seed(prices={known: {"input": "0", "output": "0"}})
    row = await _run("price-known-model", _model(input_tokens=1_000, output_tokens=1_000))
    pricing = MODEL_PRICING[known]
    assert row.cost == (pricing.input * 1_000 + pricing.output * 1_000) / Decimal(1_000_000)
