# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the G11.5 tier→Model resolver (#1075 + #1076).

These exercise :mod:`meho_backplane.agent.models` against stub backend
builders so no real LLM is hit (python_best_practices §14 — no network in
unit tests). The Bedrock tests *construct* the
:class:`~pydantic_ai.models.bedrock.BedrockConverseModel` but never call
its ``request`` / ``request_stream`` methods, so no Bedrock traffic
leaves the process (boto3 builds the client lazily — the first AWS
call would be the loop step the tests don't run).

The acceptance criteria from #1075 + #1076 map onto the tests as:

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
    AgentRunEventKind,
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
    bedrock_capabilities,
    build_resolver,
    default_anthropic_backends,
    default_anthropic_policy,
    default_bedrock_backends,
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

    # The Anthropic Model class is the one the builder constructs. Assert
    # every AgentTier value routes to it, not just TRIAGE — the
    # default-tenant policy is the recovery shape, so a tier that quietly
    # missed registration would regress every existing default-tenant
    # deploy.
    from pydantic_ai.models.anthropic import AnthropicModel

    for tier in AgentTier:
        model = resolver.resolve(operator, tier)
        assert isinstance(model, AnthropicModel), tier


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


def test_bedrock_capabilities_declare_converse_tool_format() -> None:
    """The Bedrock backend declares the Converse-API tool format and caps.

    AC #2 (the capability-flags slice for #1076): the Bedrock registration
    declares ``tool_format="converse"`` — *not* ``"anthropic"`` — because
    pydantic_ai's Bedrock path is the Converse API via boto3, which
    routes tool calls through Bedrock's ``toolSpec`` shape rather than
    the Anthropic-native XML tool-call format. Tools + streaming +
    prompt-cache are all advertised because the default Bedrock
    registration targets the Anthropic-on-Bedrock family, which the
    :class:`~pydantic_ai.providers.bedrock.BedrockModelProfile`
    ``bedrock_supports_prompt_caching=True`` allow-list covers.
    """
    assert bedrock_capabilities.supports_tools is True
    assert bedrock_capabilities.supports_streaming is True
    assert bedrock_capabilities.supports_prompt_cache is True
    assert bedrock_capabilities.tool_format == "converse"


def test_default_bedrock_backends_registers_bedrock_anthropic_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``default_bedrock_backends()`` registers under ``bedrock-anthropic``.

    The id is family-tagged (``bedrock-anthropic``, not bare ``bedrock``)
    so a deploy that later routes a non-Anthropic Bedrock family to a
    different tier (Nova, Mistral) registers an additional id without
    re-keying the existing one. Pins the contract a downstream policy
    builder reads.
    """
    monkeypatch.setenv("BEDROCK_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIATESTKEY")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-secret-key")
    get_settings.cache_clear()

    backends = default_bedrock_backends()
    assert set(backends) == {"bedrock-anthropic"}
    builder, capabilities, is_saas_egress = backends["bedrock-anthropic"]
    assert capabilities is bedrock_capabilities
    # Public Bedrock endpoint counts as SaaS egress by default; the
    # PrivateLink posture flag is the policy's choice (covered by
    # ``test_air_gapped_tenant_routes_to_bedrock_via_privatelink``).
    assert is_saas_egress is True
    # The builder constructs a BedrockConverseModel; importing here
    # rather than at module level keeps the test independent of how
    # the production code stages its lazy import.
    from pydantic_ai.models.bedrock import BedrockConverseModel

    model = builder()
    assert isinstance(model, BedrockConverseModel)


def test_bedrock_routes_to_bedrock_converse_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tenant policy mapping a tier to ``bedrock-anthropic`` builds the Converse model.

    AC #1 (the routing slice for #1076): the resolver picks up the
    Bedrock registration off ``default_bedrock_backends()`` and
    materialises a :class:`~pydantic_ai.models.bedrock.BedrockConverseModel`
    when the tenant policy maps a tier to that backend id. Capability
    flags reflect Converse (asserted in
    ``test_bedrock_capabilities_declare_converse_tool_format``); the
    routing assertion here is structural — the model type proves the
    builder ran.
    """
    monkeypatch.setenv("BEDROCK_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIATESTKEY")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-secret-key")
    get_settings.cache_clear()

    resolver = build_resolver(
        policies={
            _TENANT_A: TenantModelPolicy(
                tiers={
                    AgentTier.INVESTIGATE: TierMapping(backend_id="bedrock-anthropic"),
                },
            ),
        },
        backends=default_bedrock_backends(),
    )
    operator = _make_operator(tenant_id=_TENANT_A)

    from pydantic_ai.models.bedrock import BedrockConverseModel

    model = resolver.resolve(operator, AgentTier.INVESTIGATE)
    assert isinstance(model, BedrockConverseModel)


def test_bedrock_builder_fails_closed_without_region(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Bedrock builder fails closed when boto3 can't resolve a region.

    Companion to ``test_default_anthropic_builder_fails_closed_without_key``:
    the fail-closed posture for Bedrock is *region*-shaped (the
    Converse endpoint is region-bound), not key-shaped (boto3's
    credential chain owns the auth). With ``BEDROCK_REGION`` empty *and*
    no ``AWS_DEFAULT_REGION`` / ``AWS_REGION``, boto3 raises
    ``NoRegionError`` mid-construction; the builder wraps that in
    :class:`AgentRunError` so callers catch one error type.
    """
    monkeypatch.delenv("BEDROCK_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    # Point AWS_CONFIG_FILE at a path that does not exist so boto3
    # cannot read a region from a shared profile on the CI runner.
    monkeypatch.setenv("AWS_CONFIG_FILE", "/nonexistent/aws-config-for-test")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/nonexistent/aws-creds-for-test")
    get_settings.cache_clear()

    resolver = build_resolver(
        policies={
            _TENANT_A: TenantModelPolicy(
                tiers={
                    AgentTier.TRIAGE: TierMapping(backend_id="bedrock-anthropic"),
                },
            ),
        },
        backends=default_bedrock_backends(),
    )
    operator = _make_operator(tenant_id=_TENANT_A)

    with pytest.raises(AgentRunError, match="BEDROCK_REGION"):
        resolver.resolve(operator, AgentTier.TRIAGE)


def test_air_gapped_tenant_refuses_default_saas_bedrock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A no-egress tenant cannot resolve to the default (SaaS-flagged) Bedrock.

    The public Bedrock endpoint
    (``bedrock-runtime.<region>.amazonaws.com``) traverses the public
    internet, so the default ``default_bedrock_backends()`` registration
    flags it ``is_saas_egress=True``. An ``allow_egress=False`` tenant
    that points its policy at this default registration fail-closes —
    the AWS PrivateLink / VPC-endpoint posture is a *different*
    registration, covered next.
    """
    monkeypatch.setenv("BEDROCK_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIATESTKEY")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-secret-key")
    get_settings.cache_clear()

    resolver = build_resolver(
        policies={
            _TENANT_AIRGAP: TenantModelPolicy(
                tiers={
                    AgentTier.INVESTIGATE: TierMapping(backend_id="bedrock-anthropic"),
                },
                allow_egress=False,
            ),
        },
        backends=default_bedrock_backends(),
    )
    operator = _make_operator(tenant_id=_TENANT_AIRGAP)

    with pytest.raises(EgressViolationError, match="bedrock-anthropic"):
        resolver.resolve(operator, AgentTier.INVESTIGATE)


def test_air_gapped_tenant_routes_to_bedrock_via_privatelink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Bedrock-preferred air-gapped tenant routes through a PrivateLink registration.

    AC #1 / #3 (the air-gapped policy example for #1076): the brief
    asks for "tier→Bedrock routing for an air-gapped/Bedrock-preferred
    tenant". A tenant that brokers Bedrock over AWS PrivateLink or
    VPC endpoints — so traffic never traverses the public internet —
    re-registers the bedrock builder under a separate id with
    ``is_saas_egress=False``. The resolver picks it up and the
    ``allow_egress=False`` posture is preserved. The test demonstrates
    the registration *layering*: ``default_bedrock_backends()`` is the
    safe-by-default SaaS registration, and a deploy that needs the
    PrivateLink posture adds (not replaces) a second registration under
    a separate backend id.
    """
    monkeypatch.setenv("BEDROCK_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIATESTKEY")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-secret-key")
    get_settings.cache_clear()

    # Layer a PrivateLink registration on top of the SaaS default. Same
    # builder + capabilities; only the SaaS-egress flag flips.
    saas_default = default_bedrock_backends()
    saas_builder, saas_caps, _ = saas_default["bedrock-anthropic"]
    backends = {
        **saas_default,
        "bedrock-anthropic-privatelink": (saas_builder, saas_caps, False),
    }

    resolver = build_resolver(
        policies={
            _TENANT_AIRGAP: TenantModelPolicy(
                tiers={
                    AgentTier.TRIAGE: TierMapping(
                        backend_id="bedrock-anthropic-privatelink",
                    ),
                    AgentTier.INVESTIGATE: TierMapping(
                        backend_id="bedrock-anthropic-privatelink",
                    ),
                    AgentTier.SUMMARIZE: TierMapping(
                        backend_id="bedrock-anthropic-privatelink",
                    ),
                },
                allow_egress=False,
            ),
        },
        backends=backends,
    )
    operator = _make_operator(tenant_id=_TENANT_AIRGAP)

    # Every tier resolves cleanly — the PrivateLink flag clears the
    # egress check, the Converse capability flags clear the tool-use
    # check, and the model materialises as a BedrockConverseModel.
    from pydantic_ai.models.bedrock import BedrockConverseModel

    for tier in AgentTier:
        model = resolver.resolve(operator, tier)
        assert isinstance(model, BedrockConverseModel), tier


def test_mixed_tenant_routes_some_tiers_to_anthropic_some_to_bedrock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tenant policy can split tiers across Anthropic + Bedrock backends.

    Demonstrates the layering ``{**default_anthropic_backends(),
    **default_bedrock_backends()}`` ``default_bedrock_backends()``'s
    docstring documents: a deploy registers both, and the tenant policy
    picks which tier routes where. Concrete example: send the cheap
    triage tier to Anthropic direct (lower latency, native tools) but
    route the deep investigate tier to Bedrock (enterprise procurement,
    region pinning, IAM audit).
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-router-test")
    monkeypatch.setenv("BEDROCK_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIATESTKEY")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-secret-key")
    get_settings.cache_clear()

    resolver = build_resolver(
        policies={
            _TENANT_A: TenantModelPolicy(
                tiers={
                    AgentTier.TRIAGE: TierMapping(backend_id="anthropic"),
                    AgentTier.INVESTIGATE: TierMapping(backend_id="bedrock-anthropic"),
                    AgentTier.SUMMARIZE: TierMapping(backend_id="anthropic"),
                },
            ),
        },
        backends={
            **default_anthropic_backends(),
            **default_bedrock_backends(),
        },
    )
    operator = _make_operator(tenant_id=_TENANT_A)

    from pydantic_ai.models.anthropic import AnthropicModel
    from pydantic_ai.models.bedrock import BedrockConverseModel

    assert isinstance(resolver.resolve(operator, AgentTier.TRIAGE), AnthropicModel)
    assert isinstance(
        resolver.resolve(operator, AgentTier.INVESTIGATE),
        BedrockConverseModel,
    )
    assert isinstance(resolver.resolve(operator, AgentTier.SUMMARIZE), AnthropicModel)


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

    with pytest.raises(AgentRunError, match="resolve a model") as exc_info:
        runtime.start(definition, operator, "hello")

    # The resolver's typed error is preserved as the wrapped __cause__ so
    # debuggers / loggers can recover the precise mismatch (a future
    # regression that drops the ``from exc`` chain would silently lose
    # the diagnostic).
    assert isinstance(exc_info.value.__cause__, CapabilityMismatchError)


async def test_stream_events_yields_error_on_resolver_failure() -> None:
    """A resolver failure inside ``stream_events`` emits one terminal ERROR event.

    The :meth:`PydanticAgentRun.stream_events` docstring promises every
    failure mode (turn-budget exhausted, model error, tool error)
    surfaces as a terminal :attr:`AgentRunEventKind.ERROR` event, so the
    SSE consumer always sees a closing frame regardless of which level
    failed. A resolver failure (capability mismatch, no-egress
    violation, missing backend) lives in the same envelope: it must
    not propagate as a raw exception out of the generator, which would
    tear the ``text/event-stream`` connection without a terminal frame
    (and the EventSource client would auto-reconnect into a hot loop).
    """
    from uuid import uuid4

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
        raise AssertionError("factory should not be reached")

    runtime = PydanticAgentRun(
        model_factory=fallback_factory,
        model_resolver=resolver,
    )
    definition = AgentDefinition(
        name="broken-stream",
        system_prompt="ignored",
        request_limit=2,
        tier=AgentTier.TRIAGE,
    )
    operator = _make_operator(tenant_id=_TENANT_A)

    events = [
        event
        async for event in runtime.stream_events(definition, operator, "hello", run_id=uuid4())
    ]

    # One terminal ERROR event, generator completes cleanly (no escape).
    assert len(events) == 1
    assert events[0].kind is AgentRunEventKind.ERROR
    assert "supports_tools=False" in events[0].data["error"]
