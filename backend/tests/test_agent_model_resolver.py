# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the G11.5-T1 tier→Model resolver (#1075).

These exercise :mod:`meho_backplane.agent.models` against stub backend
builders so no real LLM is hit (python_best_practices §14 — no network in
unit tests). The acceptance criteria from #1075 map onto the tests as:

* ``test_resolver_picks_backend_per_tenant_policy`` — a tenant policy
  mapping tier → backend returns the configured Model.
* ``test_no_egress_tenant_refuses_saas_backend`` — a tenant flagged
  ``allow_egress=False`` cannot resolve to a SaaS-flagged backend; the
  resolver raises :class:`EgressViolationError`.
* ``test_capability_mismatch_refused`` — a backend with
  ``supports_tools=False`` mapped to a tier the agent runs raises
  :class:`CapabilityMismatchError` (the agent runtime always needs tools).
* ``test_default_anthropic_policy_routes_to_anthropic`` — the recovery
  shape: every tier under the default tenant policy routes to the
  Anthropic backend (the existing single-tenant path, preserved).
* ``test_falls_back_to_default_tenant_policy`` — a tenant without an
  explicit policy uses the ``__default__`` policy.
* ``test_unknown_backend_raises_backend_not_configured`` — a typo in the
  policy yields :class:`BackendNotConfiguredError`, not a KeyError.
* ``test_seam_uses_resolver_when_tier_set`` — :class:`PydanticAgentRun`
  with a resolver routes a tiered definition through the resolver and
  ignores ``model_factory``.
* ``test_seam_falls_back_to_factory_when_tier_unset`` — a definition
  with ``tier=None`` keeps the legacy ``model_factory`` path so existing
  tests are unaffected.
* ``test_seam_wraps_resolver_error_in_agent_run_error`` — a resolver
  failure surfaces as :class:`AgentRunError` (the seam's uniform
  failure type), not a raw :class:`ResolverError`.
"""

from __future__ import annotations

from collections.abc import Iterator
from uuid import UUID

import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
)
from pydantic_ai.models import Model
from pydantic_ai.models.function import AgentInfo, FunctionModel

from meho_backplane.agent import (
    DEFAULT_TENANT_KEY,
    AgentDefinition,
    AgentRunError,
    AgentRunStatus,
    AgentTier,
    BackendCapabilities,
    BackendNotConfiguredError,
    CapabilityMismatchError,
    EgressViolationError,
    PydanticAgentRun,
    TenantModelPolicy,
    TierMapping,
    anthropic_capabilities,
    build_resolver,
    default_anthropic_backends,
    default_anthropic_policy,
)
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.settings import get_settings

_TENANT_A = UUID("11111111-1111-1111-1111-111111111111")
_TENANT_B = UUID("22222222-2222-2222-2222-222222222222")
_TENANT_AIRGAP = UUID("33333333-3333-3333-3333-333333333333")


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
    tenant_id: UUID = _TENANT_A,
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


def _stub_model() -> Model:
    """Return a deterministic :class:`Model` so resolver tests don't hit network.

    Wraps :class:`FunctionModel` because every place we'd inspect this is
    only the *type* — the resolver returns ``Model``, the caller checks
    identity to confirm the right backend was picked.
    """

    def _final(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart("ok")])

    return FunctionModel(_final)


def test_resolver_picks_backend_per_tenant_policy() -> None:
    """A tenant policy mapping tier→backend returns the configured Model."""
    backend_a_model = _stub_model()
    backend_b_model = _stub_model()

    def build_a() -> Model:
        return backend_a_model

    def build_b() -> Model:
        return backend_b_model

    caps = BackendCapabilities(
        supports_tools=True,
        supports_streaming=True,
        supports_prompt_cache=False,
        tool_format="openai",
    )
    resolver = build_resolver(
        policies={
            _TENANT_A: TenantModelPolicy(
                tiers={
                    AgentTier.TRIAGE: TierMapping(backend_id="backend-a"),
                    AgentTier.INVESTIGATE: TierMapping(backend_id="backend-b"),
                    AgentTier.SUMMARIZE: TierMapping(backend_id="backend-a"),
                },
            ),
        },
        backends={
            "backend-a": (build_a, caps, False),
            "backend-b": (build_b, caps, False),
        },
    )

    operator = _make_operator(tenant_id=_TENANT_A)

    assert resolver.resolve(operator, AgentTier.TRIAGE) is backend_a_model
    assert resolver.resolve(operator, AgentTier.INVESTIGATE) is backend_b_model
    assert resolver.resolve(operator, AgentTier.SUMMARIZE) is backend_a_model


def test_no_egress_tenant_refuses_saas_backend() -> None:
    """A ``allow_egress=False`` tenant cannot resolve to a SaaS backend.

    AC #1: "a no-egress tenant never resolves to a SaaS backend."
    """
    saas_model = _stub_model()

    def build_saas() -> Model:
        return saas_model

    resolver = build_resolver(
        policies={
            _TENANT_AIRGAP: TenantModelPolicy(
                tiers={
                    AgentTier.TRIAGE: TierMapping(backend_id="saas-backend"),
                },
                allow_egress=False,
            ),
        },
        backends={
            "saas-backend": (build_saas, anthropic_capabilities, True),
        },
    )

    operator = _make_operator(tenant_id=_TENANT_AIRGAP)

    with pytest.raises(EgressViolationError, match="allow_egress=False"):
        resolver.resolve(operator, AgentTier.TRIAGE)


def test_no_egress_tenant_allows_on_prem_backend() -> None:
    """A ``allow_egress=False`` tenant *does* resolve to a non-SaaS backend.

    Companion to ``test_no_egress_tenant_refuses_saas_backend``: the
    enforcement is on the ``is_saas_egress`` flag, not on the tenant
    posture wholesale. An on-prem backend (vLLM, PAIF) remains reachable.
    """
    on_prem_model = _stub_model()

    def build_on_prem() -> Model:
        return on_prem_model

    resolver = build_resolver(
        policies={
            _TENANT_AIRGAP: TenantModelPolicy(
                tiers={
                    AgentTier.TRIAGE: TierMapping(backend_id="vllm-on-prem"),
                },
                allow_egress=False,
            ),
        },
        backends={
            "vllm-on-prem": (build_on_prem, anthropic_capabilities, False),
        },
    )

    operator = _make_operator(tenant_id=_TENANT_AIRGAP)

    assert resolver.resolve(operator, AgentTier.TRIAGE) is on_prem_model


def test_capability_mismatch_refused() -> None:
    """A backend without tool support cannot serve any agent tier.

    AC #2: "capability flags enforced: resolving a tools-requiring tier
    to a no-tools backend raises."
    """
    no_tools_caps = BackendCapabilities(
        supports_tools=False,
        supports_streaming=False,
        supports_prompt_cache=False,
        tool_format="openai",
    )

    def build_no_tools() -> Model:
        return _stub_model()

    resolver = build_resolver(
        policies={
            _TENANT_A: TenantModelPolicy(
                tiers={
                    AgentTier.TRIAGE: TierMapping(backend_id="text-only"),
                },
            ),
        },
        backends={
            "text-only": (build_no_tools, no_tools_caps, False),
        },
    )

    operator = _make_operator(tenant_id=_TENANT_A)

    with pytest.raises(CapabilityMismatchError, match="supports_tools=False"):
        resolver.resolve(operator, AgentTier.TRIAGE)


def test_default_anthropic_policy_routes_to_anthropic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default-tenant Anthropic policy preserves the pre-G11.5 path.

    AC #3: "the existing Anthropic path still works through the resolver
    (default tenant → Anthropic)."

    Patches the Anthropic SDK constructor so the resolver builds a Model
    without hitting the network. The point is to prove the policy
    *routes* to the Anthropic builder and the builder fires; the real
    Anthropic init is exercised by the opt-in integration test.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-router-test")
    get_settings.cache_clear()

    resolver = build_resolver(
        policies={DEFAULT_TENANT_KEY: default_anthropic_policy()},
        backends=default_anthropic_backends(),
    )
    operator = _make_operator(tenant_id=_TENANT_B)

    model = resolver.resolve(operator, AgentTier.TRIAGE)
    # The Anthropic Model class is the one the builder constructs.
    from pydantic_ai.models.anthropic import AnthropicModel

    assert isinstance(model, AnthropicModel)


def test_default_anthropic_builder_fails_closed_without_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Anthropic builder fails closed when no key is configured.

    Mirrors the pre-resolver
    ``test_default_model_factory_fail_closed_without_key`` test; the
    fail-closed posture moves with the builder.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    get_settings.cache_clear()

    resolver = build_resolver(
        policies={DEFAULT_TENANT_KEY: default_anthropic_policy()},
        backends=default_anthropic_backends(),
    )
    operator = _make_operator(tenant_id=_TENANT_B)

    with pytest.raises(AgentRunError, match="ANTHROPIC_API_KEY"):
        resolver.resolve(operator, AgentTier.TRIAGE)


def test_falls_back_to_default_tenant_policy() -> None:
    """A tenant without an explicit policy uses the ``__default__`` policy."""
    fallback_model = _stub_model()

    def build_fallback() -> Model:
        return fallback_model

    resolver = build_resolver(
        policies={
            DEFAULT_TENANT_KEY: TenantModelPolicy(
                tiers={
                    AgentTier.TRIAGE: TierMapping(backend_id="fallback"),
                },
            ),
        },
        backends={
            "fallback": (build_fallback, anthropic_capabilities, False),
        },
    )

    # _TENANT_B is not explicitly in the policy table.
    operator = _make_operator(tenant_id=_TENANT_B)

    assert resolver.resolve(operator, AgentTier.TRIAGE) is fallback_model


def test_unknown_backend_raises_backend_not_configured() -> None:
    """A policy referencing an unregistered backend raises a typed error."""
    resolver = build_resolver(
        policies={
            _TENANT_A: TenantModelPolicy(
                tiers={
                    AgentTier.TRIAGE: TierMapping(backend_id="never-registered"),
                },
            ),
        },
        backends={},
    )

    operator = _make_operator(tenant_id=_TENANT_A)

    with pytest.raises(BackendNotConfiguredError, match="never-registered"):
        resolver.resolve(operator, AgentTier.TRIAGE)


def test_tier_without_mapping_raises_backend_not_configured() -> None:
    """A tier the tenant's policy doesn't map to any backend raises a typed error."""
    fallback_model = _stub_model()

    def build_fallback() -> Model:
        return fallback_model

    resolver = build_resolver(
        policies={
            _TENANT_A: TenantModelPolicy(
                tiers={
                    AgentTier.TRIAGE: TierMapping(backend_id="ok-backend"),
                    # INVESTIGATE + SUMMARIZE deliberately omitted.
                },
            ),
        },
        backends={
            "ok-backend": (build_fallback, anthropic_capabilities, False),
        },
    )

    operator = _make_operator(tenant_id=_TENANT_A)

    with pytest.raises(BackendNotConfiguredError, match="investigate"):
        resolver.resolve(operator, AgentTier.INVESTIGATE)


def test_no_policy_and_no_default_raises() -> None:
    """A resolver built without any policy raises clearly on resolve."""
    resolver = build_resolver(policies={}, backends={})
    operator = _make_operator(tenant_id=_TENANT_A)

    with pytest.raises(BackendNotConfiguredError, match="no model policy"):
        resolver.resolve(operator, AgentTier.TRIAGE)


async def test_seam_uses_resolver_when_tier_set() -> None:
    """A definition with ``tier`` routes through the resolver, not the factory.

    Proves the G11.5-T1 integration into :class:`PydanticAgentRun`: when
    both a resolver is wired and the definition names a tier, the
    resolver-built model is what the loop runs against, and the
    zero-arg ``model_factory`` is never called.
    """
    factory_calls = 0

    def factory() -> Model:
        nonlocal factory_calls
        factory_calls += 1

        # The factory's model would echo a wrong answer; the assertion is
        # that the resolver-built model is used instead, so the loop
        # produces the resolver's text.
        def _wrong(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            return ModelResponse(parts=[TextPart("from-factory")])

        return FunctionModel(_wrong)

    def from_resolver(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart("from-resolver")])

    resolver_model = FunctionModel(from_resolver)

    def build_resolver_backend() -> Model:
        return resolver_model

    resolver = build_resolver(
        policies={
            _TENANT_A: TenantModelPolicy(
                tiers={
                    AgentTier.TRIAGE: TierMapping(backend_id="resolver-backend"),
                },
            ),
        },
        backends={
            "resolver-backend": (build_resolver_backend, anthropic_capabilities, False),
        },
    )

    runtime = PydanticAgentRun(model_factory=factory, model_resolver=resolver)
    definition = AgentDefinition(
        name="tiered-agent",
        system_prompt="ignored",
        request_limit=2,
        tier=AgentTier.TRIAGE,
    )
    operator = _make_operator(tenant_id=_TENANT_A)

    handle = runtime.start(definition, operator, "hello")
    result = await runtime.result(handle)

    assert runtime.poll(handle) is AgentRunStatus.SUCCEEDED
    assert result.output == "from-resolver"
    assert factory_calls == 0  # resolver path bypasses the factory entirely.


async def test_seam_falls_back_to_factory_when_tier_unset() -> None:
    """A definition with ``tier=None`` keeps the legacy factory path.

    The backwards-compatibility shape: a definition that pre-dates G11.5
    (or a test that injects ``model_factory=lambda: FunctionModel(...)``
    without bothering with a resolver) gets exactly the same model the
    factory builds. The resolver is wired here but never consulted —
    proves the "tier is the trigger" semantics.
    """
    resolver_called = 0

    class _CountingResolver:
        def resolve(self, operator: Operator, tier: AgentTier) -> Model:
            nonlocal resolver_called
            resolver_called += 1
            raise AssertionError("resolver should not be called when tier is None")

    def factory_model() -> Model:
        def _ok(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            return ModelResponse(parts=[TextPart("via-factory")])

        return FunctionModel(_ok)

    runtime = PydanticAgentRun(
        model_factory=factory_model,
        model_resolver=_CountingResolver(),
    )
    definition = AgentDefinition(
        name="legacy-agent",
        system_prompt="ignored",
        request_limit=2,
        # tier deliberately unset.
    )
    operator = _make_operator(tenant_id=_TENANT_A)

    handle = runtime.start(definition, operator, "hello")
    result = await runtime.result(handle)

    assert runtime.poll(handle) is AgentRunStatus.SUCCEEDED
    assert result.output == "via-factory"
    assert resolver_called == 0


async def test_seam_wraps_resolver_error_in_agent_run_error() -> None:
    """A resolver failure surfaces as :class:`AgentRunError`, not the raw type.

    The seam's contract is "callers catch one exception type"; a
    capability mismatch / egress violation / missing backend at resolve
    time should reach the loop's caller as :class:`AgentRunError` with
    the resolver's detail wrapped in the message.
    """
    no_tools_caps = BackendCapabilities(
        supports_tools=False,
        supports_streaming=False,
        supports_prompt_cache=False,
        tool_format="openai",
    )

    def build_no_tools() -> Model:
        return _stub_model()

    resolver = build_resolver(
        policies={
            _TENANT_A: TenantModelPolicy(
                tiers={
                    AgentTier.TRIAGE: TierMapping(backend_id="no-tools"),
                },
            ),
        },
        backends={
            "no-tools": (build_no_tools, no_tools_caps, False),
        },
    )

    def fallback_factory() -> Model:
        # Should never be called when tier is set + resolver wired.
        raise AssertionError("factory should not be reached")

    runtime = PydanticAgentRun(
        model_factory=fallback_factory,
        model_resolver=resolver,
    )
    definition = AgentDefinition(
        name="broken",
        system_prompt="ignored",
        request_limit=2,
        tier=AgentTier.TRIAGE,
    )
    operator = _make_operator(tenant_id=_TENANT_A)

    with pytest.raises(AgentRunError, match="resolve a model"):
        runtime.start(definition, operator, "hello")
