# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the G11.5-T3 OpenAI-compatible backend (#1077).

These exercise the OpenAI-compat builder added in
:mod:`meho_backplane.agent.models` without hitting any real LLM endpoint
(python_best_practices §14 — no network in unit tests). Acceptance
criteria from #1077 map onto the tests as:

* ``test_openai_compat_builder_constructs_openai_chat_model`` — a
  tenant routing a tier to an OpenAI-compat ``base_url`` builds an
  :class:`OpenAIChatModel` whose underlying provider points at that
  base URL.
* ``test_vllm_profile_disables_strict_tool_definition`` — a vLLM-style
  profile (``openai_supports_strict_tool_definition=False``) is
  honoured on the model; the resolver wires the right profile.
* ``test_ollama_profile_collapses_system_messages`` — Ollama profile
  flips both quirky flags.
* ``test_resolver_routes_air_gapped_tenant_to_on_prem_openai_compat``
  — an ``allow_egress=False`` tenant resolves a tier to an on-prem
  OpenAI-compat backend without tripping
  :class:`EgressViolationError` (the egress posture allows non-SaaS
  registrations).
* ``test_default_openai_builder_fails_closed_without_key`` — the
  settings-driven default builder raises :class:`AgentRunError` when
  no ``OPENAI_API_KEY`` is configured.
* ``test_default_openai_builder_picks_vendor_from_base_url_hint`` —
  the settings-driven default picks the Ollama profile when the
  configured base URL contains ``ollama`` (host hint heuristic).
"""

from __future__ import annotations

from collections.abc import Iterator
from uuid import UUID

import pytest
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.profiles.openai import OpenAIModelProfile

from meho_backplane.agent import (
    AgentRunError,
    AgentTier,
    EgressViolationError,
    OpenAICompatVendor,
    TenantModelPolicy,
    TierMapping,
    build_resolver,
    default_openai_backend_builder,
    ollama_chat_profile,
    openai_chat_profile,
    openai_compat_backend_builder,
    openai_compat_capabilities,
    vllm_chat_profile,
)
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.settings import get_settings

_TENANT_AIRGAP = UUID("33333333-3333-3333-3333-333333333333")
_TENANT_SAAS = UUID("44444444-4444-4444-4444-444444444444")


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the env vars :class:`Settings` requires for this module."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _make_operator(
    *,
    tenant_id: UUID = _TENANT_AIRGAP,
    role: TenantRole = TenantRole.OPERATOR,
    sub: str = "op-agent",
) -> Operator:
    """Construct a minimal valid :class:`Operator` for a tenant."""
    return Operator(
        sub=sub,
        name="Agent Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=tenant_id,
        tenant_role=role,
    )


def _extract_profile(model: Model) -> OpenAIModelProfile:
    """Return the :class:`OpenAIModelProfile` that the model carries.

    pydantic_ai exposes the profile through the ``Model.profile``
    property — the resolver wires it via the builder's ``profile=``
    kwarg, so this reads back what was wired. ``isinstance`` rather
    than equality so a future framework-side widening of the profile
    return type (a subclass) still passes the test.
    """
    profile = model.profile
    assert isinstance(profile, OpenAIModelProfile), profile
    return profile


def test_openai_chat_profile_matches_openai_saas_quirks() -> None:
    """The OpenAI SaaS profile keeps strict tool defs + multi-system on."""
    profile = openai_chat_profile()
    assert profile.openai_supports_strict_tool_definition is True
    assert profile.openai_chat_supports_multiple_system_messages is True
    assert profile.json_schema_transformer is None


def test_vllm_profile_disables_strict_tool_definition() -> None:
    """vLLM accepts the OpenAI shape but does not enforce ``strict=True``.

    Recording the quirk on the profile (``openai_supports_strict_tool_definition=False``)
    is what stops the framework from sending ``"strict": true`` in tool
    definitions, which the vLLM engine would silently ignore — leaving
    the model free to emit a non-conforming tool call that the loop
    then rejects on a structural mismatch.
    """
    profile = vllm_chat_profile()
    assert profile.openai_supports_strict_tool_definition is False
    # vLLM still honours multiple system messages.
    assert profile.openai_chat_supports_multiple_system_messages is True


def test_ollama_profile_collapses_system_messages() -> None:
    """Ollama needs strict-off AND multi-system-off.

    Two restrictions versus OpenAI SaaS (see ``ollama_chat_profile``
    docstring): the ``openai`` compat layer ignores strict tool defs,
    and Ollama's chat template collapses multiple ``role=system`` turns
    into one. Both flags must surface to the framework so the loop's
    message-assembly path does the right thing.
    """
    profile = ollama_chat_profile()
    assert profile.openai_supports_strict_tool_definition is False
    assert profile.openai_chat_supports_multiple_system_messages is False


def test_openai_compat_capabilities_declare_tools_streaming_no_cache() -> None:
    """Capability flags reflect the OpenAI-compat surface honestly.

    Tools and streaming are universal across the three vendors; prompt
    caching is off because none of them exposes the Anthropic-style
    ``cache_control`` knob today (OpenAI's automatic input caching is
    opaque to the client). The cost-attribution layer (#1079) reads
    this flag to decide whether to model a per-message cache discount.
    """
    caps = openai_compat_capabilities
    assert caps.supports_tools is True
    assert caps.supports_streaming is True
    assert caps.supports_prompt_cache is False
    assert caps.tool_format == "openai"


def test_openai_compat_builder_constructs_openai_chat_model() -> None:
    """AC #1: a tier routed to an OpenAI-compat base_url builds an OpenAIChatModel.

    Constructs the builder closure, calls it once, and confirms the
    returned :class:`Model` is an :class:`OpenAIChatModel` whose
    profile is the OpenAI SaaS profile. The builder is lazy — the
    OpenAI SDK / provider construction happens here, not at registration,
    and any failure (missing extra, bad model id) would surface as an
    exception on this call rather than silently mid-loop.
    """
    builder = openai_compat_backend_builder(
        vendor=OpenAICompatVendor.OPENAI,
        model_id="openai:gpt-4o-mini",
        base_url=None,
        api_key="sk-test-not-real",
    )

    model = builder()

    assert isinstance(model, OpenAIChatModel)
    profile = _extract_profile(model)
    assert profile.openai_supports_strict_tool_definition is True


def test_openai_compat_builder_honours_vendor_profile_for_vllm() -> None:
    """AC #2: the resolver wires the right per-vendor profile.

    A tools tier routed to a vLLM-style backend gets the vLLM profile
    (strict-off), not the OpenAI default. Proves the
    ``_vendor_profile_for`` dispatch is hooked up — a regression where
    the builder forgot the profile kwarg would silently fall back to
    the framework's default (strict-on), which would let the loop send
    ``strict=True`` to vLLM and accept the broken tool calls.
    """
    builder = openai_compat_backend_builder(
        vendor=OpenAICompatVendor.VLLM,
        model_id="meta-llama/Llama-3.1-8B-Instruct",
        base_url="http://vllm.internal:8000/v1",
        api_key="not-checked-by-vllm",
    )

    model = builder()

    assert isinstance(model, OpenAIChatModel)
    profile = _extract_profile(model)
    assert profile.openai_supports_strict_tool_definition is False
    assert profile.openai_chat_supports_multiple_system_messages is True


def test_openai_compat_builder_honours_vendor_profile_for_ollama() -> None:
    """Ollama vendor pick yields the Ollama profile (both flags flipped)."""
    builder = openai_compat_backend_builder(
        vendor=OpenAICompatVendor.OLLAMA,
        model_id="llama3.1:8b",
        base_url="http://ollama.internal:11434/v1",
        api_key="ollama",
    )

    model = builder()

    assert isinstance(model, OpenAIChatModel)
    profile = _extract_profile(model)
    assert profile.openai_supports_strict_tool_definition is False
    assert profile.openai_chat_supports_multiple_system_messages is False


def test_resolver_routes_air_gapped_tenant_to_on_prem_openai_compat() -> None:
    """An ``allow_egress=False`` tenant resolves to an on-prem OpenAI-compat.

    Companion to ``test_no_egress_tenant_refuses_saas_backend`` in
    :mod:`tests.test_agent_model_resolver`: registering an OpenAI-compat
    backend with ``is_saas_egress=False`` (the on-prem vLLM / Ollama /
    PAIF case) means the egress check passes, and the tier resolves
    to the OpenAI-compat Model. The tenant policy mentions a single
    backend; the resolver should not need any Anthropic registration
    to satisfy it.

    Demonstrates the "tier→OpenAI-compat routing for on-prem tenants"
    AC from the task brief: the air-gapped posture and the OpenAI-compat
    backend are independent dimensions — the resolver enforces egress
    on a per-backend ``is_saas_egress`` flag, not on the backend kind.
    """
    builder = openai_compat_backend_builder(
        vendor=OpenAICompatVendor.VLLM,
        model_id="meta-llama/Llama-3.1-8B-Instruct",
        base_url="http://vllm.airgap.local:8000/v1",
        api_key="any-non-empty-string",
    )
    resolver = build_resolver(
        policies={
            _TENANT_AIRGAP: TenantModelPolicy(
                tiers={
                    AgentTier.TRIAGE: TierMapping(backend_id="vllm-on-prem"),
                    AgentTier.INVESTIGATE: TierMapping(backend_id="vllm-on-prem"),
                    AgentTier.SUMMARIZE: TierMapping(backend_id="vllm-on-prem"),
                },
                allow_egress=False,
            ),
        },
        backends={
            "vllm-on-prem": (builder, openai_compat_capabilities, False),
        },
    )
    operator = _make_operator(tenant_id=_TENANT_AIRGAP)

    for tier in AgentTier:
        model = resolver.resolve(operator, tier)
        assert isinstance(model, OpenAIChatModel), tier


def test_resolver_refuses_saas_openai_for_air_gapped_tenant() -> None:
    """The flag-driven egress check still bites when OpenAI SaaS is registered.

    A tenant flagged ``allow_egress=False`` whose policy resolves a
    tier to an OpenAI-compat backend registered with
    ``is_saas_egress=True`` (the OpenAI SaaS case — ``api.openai.com``
    counts as egress) must surface as :class:`EgressViolationError`,
    not silently fall through. Proves the OpenAI-compat shape uses
    the same egress contract as Anthropic.
    """
    builder = openai_compat_backend_builder(
        vendor=OpenAICompatVendor.OPENAI,
        model_id="openai:gpt-4o-mini",
        base_url=None,
        api_key="sk-test-not-real",
    )
    resolver = build_resolver(
        policies={
            _TENANT_AIRGAP: TenantModelPolicy(
                tiers={AgentTier.TRIAGE: TierMapping(backend_id="openai-saas")},
                allow_egress=False,
            ),
        },
        backends={
            "openai-saas": (builder, openai_compat_capabilities, True),
        },
    )
    operator = _make_operator(tenant_id=_TENANT_AIRGAP)

    with pytest.raises(EgressViolationError, match="allow_egress=False"):
        resolver.resolve(operator, AgentTier.TRIAGE)


def test_default_openai_builder_fails_closed_without_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mirrors the Anthropic fail-closed contract for the OpenAI default.

    Empty ``OPENAI_API_KEY`` raises :class:`AgentRunError` — a deploy
    that registered an OpenAI-compat backend but never wired credentials
    surfaces at first agent invocation, not mid-loop. The settings-
    driven default is the convenience path; a multi-endpoint deploy
    uses ``openai_compat_backend_builder(api_key=...)`` directly.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "")
    get_settings.cache_clear()

    with pytest.raises(AgentRunError, match="OPENAI_API_KEY"):
        default_openai_backend_builder()


def test_default_openai_builder_picks_ollama_profile_from_base_url_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The host-hint heuristic flips the profile when base URL mentions ollama.

    A bare ``OPENAI_BASE_URL=http://ollama.internal:11434/v1`` should
    yield the Ollama profile (strict-off, multi-system-off) without
    the operator having to wire a custom builder. Proves the
    settings-driven default is more than a stub — it picks the right
    quirks for the common single-knob single-tenant on-prem case.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "ollama")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://ollama.internal:11434/v1")
    monkeypatch.setenv("OPENAI_DEFAULT_MODEL", "openai:llama3.1:8b")
    get_settings.cache_clear()

    model = default_openai_backend_builder()

    assert isinstance(model, OpenAIChatModel)
    profile = _extract_profile(model)
    assert profile.openai_supports_strict_tool_definition is False
    assert profile.openai_chat_supports_multiple_system_messages is False


def test_default_openai_builder_routes_to_openai_saas_when_no_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty ``OPENAI_BASE_URL`` keeps the OpenAI SaaS profile.

    Confirms the host-hint heuristic's default branch — no base URL
    means OpenAI SaaS, not a vendor-misdirected build.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("OPENAI_BASE_URL", "")
    monkeypatch.setenv("OPENAI_DEFAULT_MODEL", "openai:gpt-4o-mini")
    get_settings.cache_clear()

    model = default_openai_backend_builder()

    assert isinstance(model, OpenAIChatModel)
    profile = _extract_profile(model)
    assert profile.openai_supports_strict_tool_definition is True
    assert profile.openai_chat_supports_multiple_system_messages is True


def test_resolver_routes_default_tenant_to_openai_for_saas_only_deploy() -> None:
    """A deploy with no Anthropic key uses an OpenAI-compat default tenant.

    Demonstrates the recovery shape symmetric to
    :func:`default_anthropic_policy`: a SaaS-OK tenant whose policy
    routes every tier to the OpenAI-compat backend resolves all three
    tiers to OpenAI without ever building or registering an Anthropic
    backend (so a deploy without ``ANTHROPIC_API_KEY`` is fine, as
    long as it has ``OPENAI_API_KEY``).
    """
    builder = openai_compat_backend_builder(
        vendor=OpenAICompatVendor.OPENAI,
        model_id="openai:gpt-4o-mini",
        base_url=None,
        api_key="sk-test-not-real",
    )
    resolver = build_resolver(
        policies={
            _TENANT_SAAS: TenantModelPolicy(
                tiers={tier: TierMapping(backend_id="openai-saas") for tier in AgentTier},
            ),
        },
        backends={
            "openai-saas": (builder, openai_compat_capabilities, True),
        },
    )
    operator = _make_operator(tenant_id=_TENANT_SAAS)

    for tier in AgentTier:
        model = resolver.resolve(operator, tier)
        assert isinstance(model, OpenAIChatModel), tier
