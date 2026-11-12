# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Live agent runs pick their model through ``AGENT_MODEL_BACKEND`` (#5850).

The live :class:`~meho_backplane.agent.invocation.AgentInvoker` builds its
runtime with the settings resolver. These tests run it against fake model
servers: only the ``httpx2`` transport of the vendor SDK is replaced, so the
tests read exactly what would go on the wire. No network is used.

* A run with ``AGENT_MODEL_BACKEND=openai`` sends every request to the
  OpenAI-compatible endpoint, with the bare model name (no ``openai:``
  prefix), and none to Anthropic. The fake server answers "model not
  found" for any other name, like a real one.
* A run with the default setting still goes to Anthropic, as before.
* The run row and the audit model names are the model that answered.
* A missing key fails the run without any request and without a fallback.
* The stored tier maps to the resolver tier; the ``openai:`` prefix is
  removed only at the start of a name.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import httpx2
import pytest
from pydantic import ValidationError
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.openai import OpenAIChatModel
from sqlalchemy import select

from meho_backplane.agent.invocation import AgentInvoker, _record_child_run
from meho_backplane.agent.models import (
    AgentTier,
    OpenAICompatVendor,
    agent_tier_for,
    bare_openai_model_name,
    openai_compat_backend_builder,
    settings_model_id,
    settings_model_resolver,
    vcf_paif_backend_builder,
)
from meho_backplane.agent.run import AgentDefinition, AgentRunEventKind, PydanticAgentRun
from meho_backplane.agents.schemas import AgentDefinitionCreate, AgentModelTier
from meho_backplane.agents.service import AgentDefinitionService
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentPrincipal, AgentRunStatus, Tenant
from meho_backplane.operations import agent_run as run_lifecycle
from meho_backplane.operations._audit import AgentRunAuditMeta, agent_run_audit_meta_var
from meho_backplane.operations.budget_enforcement import TIER_DOWNGRADE_LADDER
from meho_backplane.settings import get_settings

from ._httpx2_capture import OPENAI_TEXT_REPLY, capture_httpx2_requests, no_httpx2_egress

pytestmark = pytest.mark.asyncio

_TENANT = UUID("55555555-5555-4555-8555-555555555555")
_OPENAI_BASE_URL = "http://model-server.invalid/v1"
_OPENAI_MODEL = "meta-llama/Llama-3.1-8B-Instruct"

#: A minimal non-streamed Anthropic Messages API reply: one text block.
_ANTHROPIC_TEXT_REPLY: dict[str, Any] = {
    "id": "msg_test",
    "type": "message",
    "role": "assistant",
    "model": "claude-sonnet-5-5",
    "content": [{"type": "text", "text": "ok"}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 1, "output_tokens": 1},
}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the env vars :class:`Settings` requires; clear every model setting."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    for name in (
        "AGENT_MODEL_BACKEND",
        "AGENT_DEFAULT_MODEL",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_DEFAULT_MODEL",
        "VCF_PAIF_MODEL",
        "BEDROCK_DEFAULT_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)
    # A request the Anthropic SDK would send must never reach the network.
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://anthropic.invalid")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _use_openai_backend(monkeypatch: pytest.MonkeyPatch, model: str) -> None:
    monkeypatch.setenv("AGENT_MODEL_BACKEND", "openai")
    monkeypatch.setenv("OPENAI_BASE_URL", _OPENAI_BASE_URL)
    monkeypatch.setenv("OPENAI_API_KEY", "fake-endpoint-key")
    monkeypatch.setenv("OPENAI_DEFAULT_MODEL", model)
    get_settings.cache_clear()


def _make_operator() -> Operator:
    return Operator(
        sub="op-model-backend",
        name="Model Backend Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
        client_id=None,
    )


async def _seed_definition(name: str, model_tier: AgentModelTier = AgentModelTier.STANDARD) -> Any:
    """Insert a tenant, the agent principal and an enabled definition."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        if await session.get(Tenant, _TENANT) is None:
            session.add(Tenant(id=_TENANT, slug="tenant-model", name="Tenant model"))
        existing = await session.execute(
            select(AgentPrincipal).where(
                AgentPrincipal.tenant_id == _TENANT,
                AgentPrincipal.keycloak_client_id == f"agent:{name}",
            )
        )
        if existing.scalar_one_or_none() is None:
            session.add(
                AgentPrincipal(
                    id=uuid4(),
                    tenant_id=_TENANT,
                    name=name,
                    keycloak_client_id=f"agent:{name}",
                    keycloak_internal_id=f"kc-internal-{name}",
                    owner_sub="seed-admin",
                    revoked=False,
                    created_by_sub="seed-admin",
                )
            )
        await session.commit()
    return await AgentDefinitionService().create(
        tenant_id=_TENANT,
        created_by_sub="seed-admin",
        payload=AgentDefinitionCreate(
            name=name,
            identity_ref=f"agent:{name}",
            model_tier=model_tier,
            system_prompt="Answer briefly.",
            toolset={},
            turn_budget=3,
            enabled=True,
        ),
    )


def _openai_server(
    monkeypatch: pytest.MonkeyPatch,
    *,
    served_model: str,
    seen_meta: list[AgentRunAuditMeta | None],
) -> list[httpx2.Request]:
    """A fake OpenAI-compatible server that serves only *served_model*.

    Any other model name gets the 404 a real server sends. Each request
    also records the audit model names bound while the run sends it: the
    same names every audit row of the run's tool calls gets.
    """

    def _respond(request: httpx2.Request) -> httpx2.Response:
        seen_meta.append(agent_run_audit_meta_var.get())
        model = json.loads(request.content).get("model")
        if model != served_model:
            return httpx2.Response(
                404,
                json={
                    "error": {
                        "message": f"The model `{model}` does not exist.",
                        "type": "NotFoundError",
                        "code": 404,
                    }
                },
            )
        return httpx2.Response(200, json={**OPENAI_TEXT_REPLY, "model": served_model})

    return capture_httpx2_requests(monkeypatch, _respond)


async def _row(run_id: UUID) -> Any:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        return await run_lifecycle.get_run(session, run_id)


# ---------------------------------------------------------------------------
# Live runs on an OpenAI-compatible endpoint
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("configured", [_OPENAI_MODEL, f"openai:{_OPENAI_MODEL}"])
async def test_direct_run_uses_openai_endpoint_and_records_its_model(
    monkeypatch: pytest.MonkeyPatch, configured: str
) -> None:
    """A hosted run with ``AGENT_MODEL_BACKEND=openai`` finishes on the endpoint.

    The default setting form ``openai:<name>`` and the bare name both reach
    the wire as the bare name. No request goes anywhere else (also not to
    Anthropic). The run row and the audit model names are the endpoint's.
    """
    _use_openai_backend(monkeypatch, configured)
    await _seed_definition("openai-reader")
    seen_meta: list[AgentRunAuditMeta | None] = []
    sent = _openai_server(monkeypatch, served_model=_OPENAI_MODEL, seen_meta=seen_meta)

    outcome = await AgentInvoker().run(_make_operator(), "openai-reader", "say ok")

    assert outcome.status is AgentRunStatus.SUCCEEDED, outcome.error
    assert outcome.output == {"text": "ok"}
    assert len(sent) == 1
    assert all(str(request.url).startswith(_OPENAI_BASE_URL) for request in sent)
    assert sent[0].headers["Authorization"] == "Bearer fake-endpoint-key"
    assert json.loads(sent[0].content)["model"] == _OPENAI_MODEL
    row = await _row(outcome.run_id)
    assert (row.provider, row.model) == ("openai", _OPENAI_MODEL)
    assert seen_meta == [AgentRunAuditMeta(model=_OPENAI_MODEL, provider="openai")]


async def test_scheduled_run_uses_openai_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """The scheduled path (also event-triggered runs and the investigator).

    ``run_scheduled`` logs in as the agent and then launches through
    ``_launch_scheduled_run``; that launch picks the model the same way.
    """
    _use_openai_backend(monkeypatch, f"openai:{_OPENAI_MODEL}")
    entry = await _seed_definition("openai-scheduled")
    seen_meta: list[AgentRunAuditMeta | None] = []
    sent = _openai_server(monkeypatch, served_model=_OPENAI_MODEL, seen_meta=seen_meta)

    outcome = await AgentInvoker()._launch_scheduled_run(
        "openai-scheduled",
        "say ok",
        operator=_make_operator(),
        entry=entry,
        settings=get_settings(),
    )

    assert outcome.status is AgentRunStatus.SUCCEEDED, outcome.error
    assert [str(request.url) for request in sent] == [f"{_OPENAI_BASE_URL}/chat/completions"]
    row = await _row(outcome.run_id)
    assert (row.provider, row.model) == ("openai", _OPENAI_MODEL)
    assert seen_meta == [AgentRunAuditMeta(model=_OPENAI_MODEL, provider="openai")]


async def test_streamed_run_uses_openai_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """The streamed (SSE) path picks the model the same way."""
    _use_openai_backend(monkeypatch, f"openai:{_OPENAI_MODEL}")
    await _seed_definition("openai-stream")
    seen_meta: list[AgentRunAuditMeta | None] = []
    sent = _openai_server(monkeypatch, served_model=_OPENAI_MODEL, seen_meta=seen_meta)

    events = [
        (run_id, event)
        async for run_id, event in AgentInvoker().stream_events(
            _make_operator(), "openai-stream", "say ok"
        )
    ]

    assert events[-1][1].kind is AgentRunEventKind.FINAL
    assert len(sent) == 1
    assert str(sent[0].url).startswith(_OPENAI_BASE_URL)
    row = await _row(events[-1][0])
    assert (row.provider, row.model) == ("openai", _OPENAI_MODEL)
    assert seen_meta == [AgentRunAuditMeta(model=_OPENAI_MODEL, provider="openai")]


async def test_openai_backend_without_key_fails_with_no_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing ``OPENAI_API_KEY`` fails the run; nothing falls back to Anthropic.

    An Anthropic key is set, so a fallback would be possible. The run must
    still fail, send no request at all, and its row must name the backend
    the setting chose.
    """
    _use_openai_backend(monkeypatch, f"openai:{_OPENAI_MODEL}")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-anthropic-key")
    get_settings.cache_clear()
    await _seed_definition("openai-no-key")

    with no_httpx2_egress():
        outcome = await AgentInvoker().run(_make_operator(), "openai-no-key", "say ok")

    assert outcome.status is AgentRunStatus.FAILED
    assert outcome.error is not None
    assert "OPENAI_API_KEY" in outcome.error
    row = await _row(outcome.run_id)
    assert (row.provider, row.model) == ("openai", _OPENAI_MODEL)


# ---------------------------------------------------------------------------
# The Anthropic setting works as before
# ---------------------------------------------------------------------------


async def test_default_backend_still_runs_on_anthropic(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without ``AGENT_MODEL_BACKEND`` a run goes to Anthropic, as before."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-anthropic-key")
    get_settings.cache_clear()
    await _seed_definition("anthropic-reader")
    seen_meta: list[AgentRunAuditMeta | None] = []

    def _respond(request: httpx2.Request) -> httpx2.Response:
        seen_meta.append(agent_run_audit_meta_var.get())
        return httpx2.Response(200, json=_ANTHROPIC_TEXT_REPLY)

    sent = capture_httpx2_requests(monkeypatch, _respond)

    outcome = await AgentInvoker().run(_make_operator(), "anthropic-reader", "say ok")

    assert outcome.status is AgentRunStatus.SUCCEEDED, outcome.error
    assert [(request.url.host, request.url.path) for request in sent] == [
        ("anthropic.invalid", "/v1/messages")
    ]
    body = json.loads(sent[0].content)
    assert body["model"] == "claude-sonnet-5-5"
    assert body["max_tokens"] == 4096
    row = await _row(outcome.run_id)
    assert (row.provider, row.model) == ("anthropic", "claude-sonnet-5-5")
    assert seen_meta == [AgentRunAuditMeta(model="claude-sonnet-5-5", provider="anthropic")]


# ---------------------------------------------------------------------------
# Child runs
# ---------------------------------------------------------------------------


async def test_child_run_row_records_parent_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """A child run started by an agent records the model of its parent run."""
    _use_openai_backend(monkeypatch, f"openai:{_OPENAI_MODEL}")
    await _seed_definition("child-agent")
    definition = AgentDefinition(name="child-agent", system_prompt="x", tier=AgentTier.SUMMARIZE)
    token = agent_run_audit_meta_var.set(AgentRunAuditMeta(model=_OPENAI_MODEL, provider="openai"))
    try:
        child_id, _owner = await _record_child_run(
            operator=_make_operator(), definition=definition, parent_run_id=None
        )
    finally:
        agent_run_audit_meta_var.reset(token)

    row = await _row(child_id)
    assert (row.provider, row.model) == ("openai", _OPENAI_MODEL)


# ---------------------------------------------------------------------------
# Settings resolver, tier map and prefix handling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tier", list(AgentTier))
async def test_settings_resolver_sends_every_tier_to_the_configured_backend(
    monkeypatch: pytest.MonkeyPatch, tier: AgentTier
) -> None:
    """Every tier resolves to the backend ``AGENT_MODEL_BACKEND`` names."""
    resolver = settings_model_resolver()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-anthropic-key")
    get_settings.cache_clear()
    anthropic = resolver.resolve(_make_operator(), tier)
    assert isinstance(anthropic, AnthropicModel)
    assert (anthropic.system, anthropic.model_name) == ("anthropic", "claude-sonnet-5-5")

    _use_openai_backend(monkeypatch, f"openai:{_OPENAI_MODEL}")
    openai = resolver.resolve(_make_operator(), tier)
    assert isinstance(openai, OpenAIChatModel)
    assert (openai.system, openai.model_name) == ("openai", _OPENAI_MODEL)


@pytest.mark.parametrize(
    ("backend", "expected"),
    [
        ("anthropic", "anthropic:claude-sonnet-5-5"),
        ("openai", "openai:gpt-4o-mini"),
        ("vcf-paif", "openai:meta-llama/Llama-3.1-8B-Instruct"),
        ("bedrock", "bedrock:us.anthropic.claude-sonnet-4-5-v1:0"),
    ],
)
async def test_settings_model_id_names_the_configured_backend(
    monkeypatch: pytest.MonkeyPatch, backend: str, expected: str
) -> None:
    """The record names for a run that could not build its model."""
    monkeypatch.setenv("AGENT_MODEL_BACKEND", backend)
    get_settings.cache_clear()
    assert settings_model_id() == expected


@pytest.mark.parametrize("raw", ["Anthropic", " openai "])
async def test_agent_model_backend_setting_is_trimmed_and_lowercased(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("AGENT_MODEL_BACKEND", raw)
    get_settings.cache_clear()
    assert get_settings().agent_model_backend == raw.strip().lower()


@pytest.mark.parametrize("raw", ["claude", "openai-compat", ""])
async def test_unknown_agent_model_backend_stops_start(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """A typo must not send runs to a backend nobody picked."""
    monkeypatch.setenv("AGENT_MODEL_BACKEND", raw)
    get_settings.cache_clear()
    with pytest.raises(ValidationError, match="AGENT_MODEL_BACKEND must be one of"):
        get_settings()


async def test_stored_tier_maps_to_resolver_tier_by_cost() -> None:
    """fast -> triage, standard -> summarize, deep -> investigate.

    The budget gate steps one tier cheaper; the map keeps that order, so a
    step down from ``deep`` lands on the tier of ``standard``, and from
    ``standard`` on the tier of ``fast``.
    """
    assert agent_tier_for(AgentModelTier.FAST) is AgentTier.TRIAGE
    assert agent_tier_for(AgentModelTier.STANDARD) is AgentTier.SUMMARIZE
    assert agent_tier_for(AgentModelTier.DEEP) is AgentTier.INVESTIGATE
    assert TIER_DOWNGRADE_LADDER[agent_tier_for("deep")] is agent_tier_for("standard")
    assert TIER_DOWNGRADE_LADDER[agent_tier_for("standard")] is agent_tier_for("fast")
    with pytest.raises(ValueError, match="unknown agent model tier"):
        agent_tier_for("turbo")


@pytest.mark.parametrize("model_tier", list(AgentModelTier))
async def test_stored_definition_gets_its_tier(model_tier: AgentModelTier) -> None:
    """Every stored definition reaches the runtime with a resolver tier."""
    entry = await _seed_definition(f"tier-{model_tier.value}", model_tier)
    definition = AgentInvoker._to_agent_definition(entry)
    assert definition.tier is agent_tier_for(model_tier)


async def test_default_invoker_runtime_carries_the_settings_resolver() -> None:
    runtime = AgentInvoker()._runtime
    assert isinstance(runtime, PydanticAgentRun)
    assert runtime.model_resolver is not None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("openai:gpt-4o-mini", "gpt-4o-mini"),
        ("gpt-4o-mini", "gpt-4o-mini"),
        ("llama3.1:8b", "llama3.1:8b"),
        ("openai:llama3.1:8b", "llama3.1:8b"),
        ("meta-llama/Llama-3.1-8B-Instruct", "meta-llama/Llama-3.1-8B-Instruct"),
        ("anthropic:claude-sonnet-5-5", "anthropic:claude-sonnet-5-5"),
    ],
)
async def test_bare_openai_model_name_removes_only_the_leading_prefix(
    raw: str, expected: str
) -> None:
    assert bare_openai_model_name(raw) == expected


async def test_openai_compat_builder_sends_bare_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """pydantic-ai sends ``OpenAIChatModel.model_name`` unchanged, so MEHO strips it."""
    from pydantic_ai import Agent

    sent = capture_httpx2_requests(
        monkeypatch, lambda _request: httpx2.Response(200, json=OPENAI_TEXT_REPLY)
    )
    model = openai_compat_backend_builder(
        vendor=OpenAICompatVendor.OLLAMA,
        model_id="openai:llama3.1:8b",
        base_url=_OPENAI_BASE_URL,
        api_key="fake-endpoint-key",
    )()

    await Agent(model).run("hi")

    assert json.loads(sent[0].content)["model"] == "llama3.1:8b"


async def test_vcf_paif_builder_sends_bare_name(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic_ai import Agent

    async def _token() -> str:
        return "fake-bearer"

    sent = capture_httpx2_requests(
        monkeypatch, lambda _request: httpx2.Response(200, json=OPENAI_TEXT_REPLY)
    )
    model = vcf_paif_backend_builder(
        model_id=f"openai:{_OPENAI_MODEL}",
        base_url="http://paif.invalid/api/v1/compatibility/openai/v1/",
        bearer_token_provider=_token,
    )()

    await Agent(model).run("hi")

    assert json.loads(sent[0].content)["model"] == _OPENAI_MODEL
