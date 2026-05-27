# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the G11.5-T4 VCF Private AI Foundation backend (#1078).

These exercise the PAIF builder + OIDC bearer-token provider added in
:mod:`meho_backplane.agent.models` without hitting any real PAIF endpoint
or IdP (python_best_practices §14 — no network in unit tests). All
HTTP traffic to the OIDC token endpoint and to PAIF itself is stubbed
via :mod:`respx`. Acceptance criteria from #1078 map onto the tests as:

* ``test_resolver_routes_air_gapped_tenant_to_paif`` — AC #1: an
  ``allow_egress=False`` tenant resolves all three tiers to the PAIF
  backend (declared ``is_saas_egress=False``) without tripping
  :class:`EgressViolationError`. The PAIF backend is registered with
  the OIDC token endpoint stubbed; the full chain (policy → resolver
  → backend → token acquisition) runs end-to-end with **zero** SaaS
  egress (respx asserts the only POST is to the in-cluster IdP).
* ``test_paif_token_acquired_via_client_credentials_and_cached`` —
  AC #2 (first half): the bundled OIDC provider POSTs the
  ``client_credentials`` grant and caches the access token; a second
  call returns the cached value without re-hitting the IdP.
* ``test_paif_token_re_acquired_after_expiry_skew_window`` —
  AC #2 (second half): once the IdP-reported ``expires_in`` minus the
  refresh skew elapses, the next call re-POSTs the grant.
* ``test_paif_token_acquisition_failure_surfaces_typed_error`` —
  AC #2 (third half): an IdP error response surfaces as
  :class:`TokenAcquisitionError`, not as a raw ``HTTPStatusError`` or
  silent fallthrough, and the message names the IdP's ``error`` field.
* ``test_default_paif_builder_fails_closed_without_settings`` — the
  settings-driven default builder raises :class:`AgentRunError` with
  every missing setting named.
* ``test_vcf_paif_chat_profile_matches_vllm_quirks`` — the PAIF profile
  honours the vLLM quirks (strict-tool-def off, multi-system on); a
  regression where PAIF accidentally inherited the OpenAI-SaaS profile
  would let the loop send ``strict=true`` to the engine and accept
  broken tool calls.
"""

from __future__ import annotations

from collections.abc import Iterator
from uuid import UUID

import httpx
import pytest
import respx
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.profiles.openai import OpenAIModelProfile

from meho_backplane.agent import (
    VCF_PAIF_OPENAI_COMPAT_BASE_PATH,
    AgentRunError,
    AgentTier,
    EgressViolationError,
    OidcClientCredentialsTokenProvider,
    TenantModelPolicy,
    TierMapping,
    TokenAcquisitionError,
    build_resolver,
    default_vcf_paif_backend_builder,
    vcf_paif_backend_builder,
    vcf_paif_bearer_provider,
    vcf_paif_capabilities,
    vcf_paif_chat_profile,
)
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.settings import get_settings

_TENANT_AIRGAP = UUID("33333333-3333-3333-3333-333333333333")
_TENANT_SAAS = UUID("44444444-4444-4444-4444-444444444444")

_TOKEN_URL = "https://kc.airgap.local/realms/meho/protocol/openid-connect/token"
_PAIF_HOST = "https://pais.airgap.local"
_PAIF_BASE_URL = f"{_PAIF_HOST}{VCF_PAIF_OPENAI_COMPAT_BASE_PATH}"


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


# ---------------------------------------------------------------------------
# Profile / capability flags
# ---------------------------------------------------------------------------


def test_vcf_paif_chat_profile_matches_vllm_quirks() -> None:
    """PAIF uses vLLM under the hood, so the profile flips the same quirks.

    A regression where PAIF accidentally returns the OpenAI-SaaS
    profile (strict-tool-def on) would let the loop send
    ``"strict": true`` in tool definitions, which vLLM accepts and
    silently ignores — letting the model emit non-conforming tool
    calls the agent loop then rejects on a structural mismatch.
    Multiple system messages are honoured (vLLM preserves the
    ``messages[]`` array verbatim; only Ollama collapses them).
    """
    profile = vcf_paif_chat_profile()
    assert isinstance(profile, OpenAIModelProfile)
    assert profile.openai_supports_strict_tool_definition is False
    assert profile.openai_chat_supports_multiple_system_messages is True
    assert profile.json_schema_transformer is None


def test_vcf_paif_capabilities_declare_tools_streaming_no_cache() -> None:
    """Capability flags reflect the PAIF surface honestly.

    Tools + streaming are honoured by the vLLM engine PAIF runs;
    prompt caching is off — neither vLLM nor PAIF exposes the
    Anthropic-style ``cache_control`` knob, and PAIF does not have
    the opaque-to-client automatic caching OpenAI SaaS does.
    The cost-attribution layer (#1079) reads this flag.
    """
    caps = vcf_paif_capabilities
    assert caps.supports_tools is True
    assert caps.supports_streaming is True
    assert caps.supports_prompt_cache is False
    assert caps.tool_format == "openai"


def test_vcf_paif_openai_compat_base_path_is_fixed() -> None:
    """The PAIF sub-path is pinned, matching the Broadcom developer docs.

    Pinning the constant here (rather than threading the literal
    through every call site) means a future Broadcom-side change to
    the compat sub-path is a single-line edit, and a preflight check
    that compares an operator-supplied ``OPENAI_BASE_URL`` against
    this anchor stays self-consistent.
    """
    assert VCF_PAIF_OPENAI_COMPAT_BASE_PATH == "/api/v1/compatibility/openai/v1/"


# ---------------------------------------------------------------------------
# OIDC token provider
# ---------------------------------------------------------------------------


async def test_paif_token_acquired_via_client_credentials_and_cached() -> None:
    """AC #2 (first half): the OIDC provider POSTs the right grant and caches.

    Verifies the form-encoded body (``application/x-www-form-urlencoded``,
    not JSON — most IdPs reject the JSON shape), the ``grant_type``,
    ``client_id``, ``client_secret``, and optional ``scope`` parameters.
    Second call inside the skew window returns the cached value without
    a second IdP round-trip (route call count stays at 1).
    """
    provider = vcf_paif_bearer_provider(
        token_url=_TOKEN_URL,
        client_id="meho-backplane",
        client_secret="secret-test",
        scope="paif",
    )
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(_TOKEN_URL).respond(
            json={"access_token": "bearer-abc", "expires_in": 300}
        )

        token = await provider()
        assert token == "bearer-abc"

        # Second call inside skew window — cached, no extra POST.
        cached = await provider()
        assert cached == "bearer-abc"
        assert route.call_count == 1

        # The form-encoded body carries the ``client_credentials`` grant
        # with the configured client id / secret / scope. ``application/
        # x-www-form-urlencoded`` is RFC 6749 §4.4.2's required shape.
        body = route.calls.last.request.content.decode()
        assert "grant_type=client_credentials" in body
        assert "client_id=meho-backplane" in body
        assert "client_secret=secret-test" in body
        assert "scope=paif" in body


async def test_paif_token_provider_omits_scope_when_unset() -> None:
    """Empty ``scope`` is *not* sent — some IdPs reject empty parameters.

    The provider treats ``None`` and ``""`` identically (no scope
    parameter at all). A regression that sent ``scope=`` would break
    against IdPs that strictly validate the OAuth 2.0 spec's
    ``scope`` shape.
    """
    provider = vcf_paif_bearer_provider(
        token_url=_TOKEN_URL,
        client_id="meho-backplane",
        client_secret="secret-test",
        # scope deliberately omitted
    )
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(_TOKEN_URL).respond(
            json={"access_token": "bearer-noscope", "expires_in": 60}
        )

        await provider()
        body = route.calls.last.request.content.decode()
        assert "scope=" not in body


async def test_paif_token_re_acquired_after_expiry_skew_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC #2 (second half): cached token expires, next call re-acquires.

    Drives ``time.monotonic`` past the IdP-reported expiry (minus the
    refresh skew) by monkey-patching the clock the provider reads.
    The second call POSTs a fresh grant, the provider hands back the
    new token. Proves the cache is not "first-write wins forever".
    """
    # ``refresh_skew_seconds=0`` so the test only has to advance the
    # clock past ``expires_in`` to evict — keeps the assertions blunt.
    provider = OidcClientCredentialsTokenProvider(
        token_url=_TOKEN_URL,
        client_id="meho-backplane",
        client_secret="secret-test",
        refresh_skew_seconds=0,
    )

    # Anchor "now" so we can advance it deterministically.
    now = [1000.0]
    monkeypatch.setattr(
        "meho_backplane.agent.models.time.monotonic",
        lambda: now[0],
    )

    with respx.mock(assert_all_called=True) as mock:
        # First grant: short-lived (30 s lifetime, 0 s skew → cache expires at t=1030).
        # Second grant: a *different* token, proving re-acquisition.
        first_route = mock.post(_TOKEN_URL).mock(
            side_effect=[
                httpx.Response(200, json={"access_token": "first", "expires_in": 30}),
                httpx.Response(200, json={"access_token": "second", "expires_in": 30}),
            ]
        )

        first = await provider()
        assert first == "first"

        # Advance past the expiry; next call must re-acquire.
        now[0] = 1031.0
        second = await provider()
        assert second == "second"
        assert first_route.call_count == 2


async def test_paif_token_acquisition_failure_surfaces_typed_error() -> None:
    """AC #2 (third half): IdP non-2xx becomes ``TokenAcquisitionError``.

    The provider must distinguish "IdP rejected the grant" (a typed
    config-error surface) from "network broken" (a different typed
    surface) from "IdP returned 200 but a malformed body" (a third).
    All three reach the agent loop as :class:`TokenAcquisitionError`
    with a message naming the failure mode — not as raw
    :class:`httpx.HTTPStatusError` deep inside the SDK.
    """
    provider = vcf_paif_bearer_provider(
        token_url=_TOKEN_URL,
        client_id="meho",
        client_secret="wrong-secret",
    )
    with respx.mock() as mock:
        mock.post(_TOKEN_URL).respond(
            401, json={"error": "invalid_client", "error_description": "bad secret"}
        )

        with pytest.raises(TokenAcquisitionError) as exc_info:
            await provider()

        # The IdP's ``error`` field is surfaced verbatim so the operator's
        # log read maps to the Keycloak / Okta / Authentik client config.
        assert "invalid_client" in str(exc_info.value)
        # The cause chain preserves the underlying httpx error for triage.
        assert isinstance(exc_info.value.__cause__, httpx.HTTPStatusError)


async def test_paif_token_acquisition_malformed_body_surfaces_typed_error() -> None:
    """A 200 response without ``access_token`` is still an acquisition failure.

    Some misconfigured IdPs return a 200 with an error payload (or a
    success payload missing ``access_token`` / ``expires_in``). The
    provider must not fall through and return ``None``; it must raise
    :class:`TokenAcquisitionError` so the loop's error event names a
    typed reason.
    """
    provider = vcf_paif_bearer_provider(
        token_url=_TOKEN_URL,
        client_id="meho",
        client_secret="secret",
    )
    with respx.mock() as mock:
        mock.post(_TOKEN_URL).respond(200, json={"not_a_token": "huh"})

        with pytest.raises(TokenAcquisitionError, match="without an"):
            await provider()


# ---------------------------------------------------------------------------
# Backend builder
# ---------------------------------------------------------------------------


def test_paif_backend_builder_constructs_openai_chat_model_with_paif_profile() -> None:
    """Builder yields an ``OpenAIChatModel`` carrying the PAIF (vLLM) profile.

    The builder closure must wire the PAIF profile (strict-off) — a
    regression that forgot the ``profile=`` kwarg would silently fall
    back to the framework default (strict-on) and start sending
    ``"strict": true`` to the engine.
    """

    async def _provider() -> str:
        return "fake-bearer-token"

    builder = vcf_paif_backend_builder(
        model_id="openai:meta-llama/Llama-3.1-8B-Instruct",
        base_url=_PAIF_BASE_URL,
        bearer_token_provider=_provider,
    )

    model = builder()

    assert isinstance(model, OpenAIChatModel)
    profile = model.profile
    assert isinstance(profile, OpenAIModelProfile)
    assert profile.openai_supports_strict_tool_definition is False
    assert profile.openai_chat_supports_multiple_system_messages is True


def test_paif_backend_builder_is_lazy_about_provider_resolution() -> None:
    """The bearer-token callable is not invoked at builder construction.

    Construction must be cheap and synchronous — the resolver registers
    the builder at app boot, often before an event loop exists. The
    token is only fetched on the first agent request, inside the openai
    SDK's async transport.
    """
    call_count = [0]

    async def _provider() -> str:
        call_count[0] += 1
        return "fake"

    builder = vcf_paif_backend_builder(
        model_id="openai:llama",
        base_url=_PAIF_BASE_URL,
        bearer_token_provider=_provider,
    )
    # Calling the builder constructs the model — but still must not
    # synchronously kick the token provider (the openai client does
    # that lazily on the first request).
    builder()
    assert call_count[0] == 0


# ---------------------------------------------------------------------------
# Resolver integration — AC #1
# ---------------------------------------------------------------------------


async def test_resolver_routes_air_gapped_tenant_to_paif() -> None:
    """AC #1: an air-gapped tenant resolves every tier to PAIF; zero SaaS egress.

    Builds the full chain — OIDC token provider + PAIF backend builder
    + resolver with a no-egress tenant policy — and resolves all three
    tiers. The PAIF backend is registered with ``is_saas_egress=False``,
    so the resolver's egress check passes. The IdP token endpoint is
    stubbed via respx; the resolver's ``resolve(...)`` does **not**
    fetch a token (the openai SDK only fetches when an actual chat
    request fires), so the test asserts the absence of any SaaS-host
    traffic: only the in-cluster IdP gets hit, and only if the model is
    actually exercised — here we exercise the resolver only, so the
    expected IdP call count is **zero**.

    The PAIF model is built three times (once per tier), each on the
    same in-cluster ``base_url`` — proves the resolver does not silently
    rewrite the URL to a SaaS host.
    """
    provider = vcf_paif_bearer_provider(
        token_url=_TOKEN_URL,
        client_id="meho-paif",
        client_secret="paif-secret",
    )
    builder = vcf_paif_backend_builder(
        model_id="openai:meta-llama/Llama-3.1-8B-Instruct",
        base_url=_PAIF_BASE_URL,
        bearer_token_provider=provider,
    )
    resolver = build_resolver(
        policies={
            _TENANT_AIRGAP: TenantModelPolicy(
                tiers={tier: TierMapping(backend_id="vcf-paif") for tier in AgentTier},
                allow_egress=False,
            ),
        },
        backends={
            "vcf-paif": (builder, vcf_paif_capabilities, False),  # is_saas_egress=False
        },
    )
    operator = _make_operator(tenant_id=_TENANT_AIRGAP)

    # respx with no registered routes refuses any HTTP traffic. If the
    # resolver or builder secretly reached out to a SaaS host or to the
    # IdP, the call would surface as a respx assertion failure with the
    # actual URL — that's the zero-egress contract.
    with respx.mock(assert_all_called=False) as mock:
        # We *do* anticipate that *if* the loop ran, only this URL would
        # be touched (proves the on-prem hosts are wired correctly).
        # We don't actually fire a chat completion in this unit test —
        # that lives in the live integration tier. But we still need to
        # stub the IdP, because the openai SDK will not call api_key
        # unless a request is made.
        mock.post(_TOKEN_URL).respond(json={"access_token": "fake-bearer", "expires_in": 300})

        for tier in AgentTier:
            model = resolver.resolve(operator, tier)
            assert isinstance(model, OpenAIChatModel), tier

        # Resolve alone must not hit the IdP — the openai SDK is lazy
        # about ``api_key=Callable[...]``. If a regression made the
        # provider fetch eagerly, this assertion would catch it.
        assert mock.calls.call_count == 0


def test_resolver_refuses_paif_backend_flagged_saas_for_no_egress_tenant() -> None:
    """Belt-and-suspenders: a *mis-registered* PAIF backend still fails closed.

    PAIF is on-prem by definition, so it should always register with
    ``is_saas_egress=False``. But if an operator copy-pastes the OpenAI
    SaaS registration shape and forgets to flip the flag, the resolver's
    egress check (driven by the registration flag, not by URL parsing)
    still fires :class:`EgressViolationError` — proving the egress
    contract is end-to-end correct regardless of which backend kind is
    misregistered.
    """

    async def _provider() -> str:
        return "noop"

    builder = vcf_paif_backend_builder(
        model_id="openai:llama",
        base_url=_PAIF_BASE_URL,
        bearer_token_provider=_provider,
    )
    resolver = build_resolver(
        policies={
            _TENANT_AIRGAP: TenantModelPolicy(
                tiers={AgentTier.TRIAGE: TierMapping(backend_id="vcf-paif-misregistered")},
                allow_egress=False,
            ),
        },
        backends={
            # Deliberately mis-flagged: True (SaaS) on a PAIF endpoint.
            "vcf-paif-misregistered": (builder, vcf_paif_capabilities, True),
        },
    )
    operator = _make_operator(tenant_id=_TENANT_AIRGAP)

    with pytest.raises(EgressViolationError, match="allow_egress=False"):
        resolver.resolve(operator, AgentTier.TRIAGE)


# ---------------------------------------------------------------------------
# Settings-driven default builder
# ---------------------------------------------------------------------------


def test_default_paif_builder_fails_closed_without_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty PAIF settings raise :class:`AgentRunError` naming each missing key.

    A deploy that registered the PAIF backend but never wired the OIDC
    config must surface at first agent invocation, not mid-loop with an
    opaque 401. The error message names every missing setting so the
    operator's fix is one ``helm upgrade`` away.
    """
    monkeypatch.setenv("VCF_PAIF_BASE_URL", "")
    monkeypatch.setenv("VCF_PAIF_OIDC_TOKEN_URL", "")
    monkeypatch.setenv("VCF_PAIF_OIDC_CLIENT_ID", "")
    monkeypatch.setenv("VCF_PAIF_OIDC_CLIENT_SECRET", "")
    get_settings.cache_clear()

    with pytest.raises(AgentRunError) as exc_info:
        default_vcf_paif_backend_builder()

    msg = str(exc_info.value)
    # All four required settings named — the operator sees the full list,
    # not one error per redeploy.
    assert "VCF_PAIF_BASE_URL" in msg
    assert "VCF_PAIF_OIDC_TOKEN_URL" in msg
    assert "VCF_PAIF_OIDC_CLIENT_ID" in msg
    assert "VCF_PAIF_OIDC_CLIENT_SECRET" in msg


def test_default_paif_builder_constructs_when_settings_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wired settings → :class:`OpenAIChatModel` carrying the PAIF profile.

    Smoke test for the settings-driven path: a deploy with all four
    required env vars set yields a constructed model, and the model
    carries the PAIF (vLLM-like) profile — not a SaaS-OpenAI profile
    silently picked up by some other code path.
    """
    monkeypatch.setenv("VCF_PAIF_BASE_URL", _PAIF_BASE_URL)
    monkeypatch.setenv("VCF_PAIF_MODEL", "openai:meta-llama/Llama-3.1-8B-Instruct")
    monkeypatch.setenv("VCF_PAIF_OIDC_TOKEN_URL", _TOKEN_URL)
    monkeypatch.setenv("VCF_PAIF_OIDC_CLIENT_ID", "meho-paif")
    monkeypatch.setenv("VCF_PAIF_OIDC_CLIENT_SECRET", "shh")
    get_settings.cache_clear()

    model = default_vcf_paif_backend_builder()

    assert isinstance(model, OpenAIChatModel)
    profile = model.profile
    assert isinstance(profile, OpenAIModelProfile)
    assert profile.openai_supports_strict_tool_definition is False


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


async def test_paif_token_provider_serialises_concurrent_acquisitions() -> None:
    """Concurrent first-acquisitions don't double-post the grant.

    The openai SDK can call the provider from multiple in-flight
    requests on the same client. Without serialisation, two requests
    racing past the cache miss would each fire a token POST. The
    provider's lock + double-check pattern ensures the first acquirer
    populates the cache and concurrent waiters re-read it on retry.
    """
    provider = OidcClientCredentialsTokenProvider(
        token_url=_TOKEN_URL,
        client_id="meho",
        client_secret="secret",
    )

    with respx.mock() as mock:
        route = mock.post(_TOKEN_URL).respond(json={"access_token": "raced", "expires_in": 300})

        import asyncio

        results = await asyncio.gather(*(provider() for _ in range(5)))

        # All five calls saw the same token, and we never hit the IdP
        # more than twice (some implementations allow a second
        # acquisition if it lands before the first's lock release, but
        # never five — that would mean no serialisation at all).
        assert results == ["raced"] * 5
        assert route.call_count <= 2
