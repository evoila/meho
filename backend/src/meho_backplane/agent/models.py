# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Per-tenant tier→Model resolver for the agent runtime (G11.5-T1).

This module replaces the hard-coded single-provider model factory in
:mod:`meho_backplane.agent.run` with a **per-tenant resolver** that maps a
logical tier (``triage`` / ``investigate`` / ``summarize``) to a concrete
:class:`pydantic_ai.models.Model`, gated on:

* **Tenant policy** — each tenant declares which backend handles each tier
  it cares about (Anthropic SaaS, AWS Bedrock, on-prem OpenAI-compatible,
  VCF Private AI Foundation, …). A tenant with no entry for a tier falls
  back to the *default* tenant's policy.
* **Egress constraint** — a tenant flagged ``allow_egress=False`` (the
  air-gapped posture) cannot resolve to a SaaS backend. The resolver
  enforces this independently of which backends are registered, so a
  misconfigured policy that lists a SaaS backend for a no-egress tenant
  fail-closes at resolve time rather than at request time.
* **Capability flags** — each backend declares :class:`BackendCapabilities`
  (``supports_tools`` / ``supports_streaming`` / ``supports_prompt_cache``
  / ``tool_format``). A tier needing tools (the agent runtime always
  needs tools — the loop is tool-use) cannot route to a no-tools backend;
  the resolver raises :class:`CapabilityMismatchError`.

Why a resolver, not "another factory"
=====================================

The G11.1 seam (:class:`~meho_backplane.agent.run.PydanticAgentRun`) takes
a zero-arg :data:`~meho_backplane.agent.run.ModelFactory`. That shape was
right for T1 where one process talked to one provider; it loses the two
pieces this task needs to honour: *which tenant* is running the agent, and
*which tier* the definition asks for. The resolver is the architectural
sibling of the connectors' fingerprint resolver
(:func:`meho_backplane.connectors.resolver.resolve_connector`): keyed on
identity-derived context (here ``tenant_id`` + ``egress`` posture), returns
a domain object (here :class:`pydantic_ai.models.Model`).

The legacy :data:`~meho_backplane.agent.run.ModelFactory` is **not removed**.
Tests inject ``model_factory=lambda: FunctionModel(...)`` to make the loop
deterministic, and the test surface is large enough that flipping every
call site to a resolver is unnecessary churn for this task. When a runtime
carries both a ``model_factory`` and a ``model_resolver``, the resolver
wins for definitions that name a tier; the factory remains the path for
definitions with ``tier is None`` (tests, the legacy default-tenant run).

What ships here vs. C4-c/d
==========================

G11.5-T1 (#1075) shipped the **resolver shape + capability flags + the
Anthropic backend builder**. G11.5-T2 (#1076) added the **AWS Bedrock
Converse backend builder** alongside it (``[bedrock]`` extra: boto3 +
:class:`pydantic_ai.models.bedrock.BedrockConverseModel`). Concrete
builders for OpenAI-compatible (vLLM / Ollama) and VCF Private AI
Foundation are filed under #1077 and #1078; they slot in as additional
:class:`BackendBuilder` registrations following the same pattern.

The Bedrock backend deliberately speaks the **Converse API** (boto3) —
not the ``anthropic[bedrock]`` adapter. The two paths look similar
("Claude over AWS") but route through different tool schemas: Anthropic
direct API uses Anthropic-native tool-call XML; Bedrock uses the
Converse API's ``toolSpec`` shape. The capability flag
``tool_format="converse"`` records the difference so a future format-
adapter seam can branch on it.

Imports for each backend are **function-local** so a deployment whose
policy never references that backend never loads the provider extra
(e.g. an Anthropic-only deploy never imports boto3; an air-gapped
Bedrock-only deploy never imports the Anthropic SDK).
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from threading import Lock
from typing import TYPE_CHECKING, Final, Protocol
from uuid import UUID

import httpx
import structlog

if TYPE_CHECKING:
    from pydantic_ai.models import Model

    from meho_backplane.auth.operator import Operator

__all__ = [
    "DEFAULT_TENANT_KEY",
    "VCF_PAIF_OPENAI_COMPAT_BASE_PATH",
    "AgentTier",
    "BackendBuilder",
    "BackendCapabilities",
    "BackendNotConfiguredError",
    "BearerTokenProvider",
    "CapabilityMismatchError",
    "EgressViolationError",
    "ModelResolver",
    "OidcClientCredentialsTokenProvider",
    "OpenAICompatVendor",
    "ResolverError",
    "TenantModelPolicy",
    "TierMapping",
    "TokenAcquisitionError",
    "anthropic_backend_builder",
    "anthropic_capabilities",
    "bedrock_backend_builder",
    "bedrock_capabilities",
    "build_resolver",
    "default_anthropic_backends",
    "default_anthropic_policy",
    "default_bedrock_backends",
    "default_openai_backend_builder",
    "default_vcf_paif_backend_builder",
    "ollama_chat_profile",
    "openai_chat_profile",
    "openai_compat_backend_builder",
    "openai_compat_capabilities",
    "vcf_paif_backend_builder",
    "vcf_paif_bearer_provider",
    "vcf_paif_capabilities",
    "vcf_paif_chat_profile",
    "vllm_chat_profile",
]

_log = structlog.get_logger(__name__)


class AgentTier(StrEnum):
    """Logical model tiers a tenant policy maps to a concrete backend.

    Three tiers, deliberately small — the consumer-facing tiering doc
    (``agent-runtime-for-ops-spec.md`` §C4) settled on this triplet:

    * :attr:`TRIAGE` — a cheap fast classifier; the always-on watcher tier.
    * :attr:`INVESTIGATE` — a deep reasoning model invoked on escalation.
    * :attr:`SUMMARIZE` — a mid-cost finisher; renders a final write-up.

    Closed enum so a tenant policy listing an unknown tier name fails at
    config-load time (Pydantic validates against the enum members) rather
    than at agent-run time. Adding a tier later is a deliberate code
    change; "tier" in the consumer harness is *not* the same concept as
    a MEHO ``AgentTier`` — the harness composes two agents one of which
    invokes the other (see Goal #800 out-of-scope), and each agent's
    definition picks a tier independently.
    """

    TRIAGE = "triage"
    INVESTIGATE = "investigate"
    SUMMARIZE = "summarize"


@dataclass(frozen=True, slots=True)
class BackendCapabilities:
    """Static capability flags a backend declares to the resolver.

    Reused across every :class:`BackendBuilder` registration so the resolver
    can refuse a tier→backend mapping that would route a tool-using agent
    to a backend without tool support. Frozen + slotted because these flags
    are looked up on the hot path of every agent run (one read per resolve).

    Fields mirror the four capabilities the Initiative body (#806) calls
    out as the cross-backend decision surface:

    * :attr:`supports_tools` — whether the backend supports the multi-turn
      tool-use loop the agent runtime drives. Anthropic + Bedrock + OpenAI
      Chat Completions + vLLM (with tool-calling turned on, see vLLM docs)
      do; some on-prem inference servers do not. Required ``True`` for
      every backend the agent runtime resolves to in v1 — the loop calls
      tools every turn — so the resolver raises
      :class:`CapabilityMismatchError` if a backend with ``False`` is
      mapped to a tier the agent will run.
    * :attr:`supports_streaming` — whether the backend can emit per-token
      / per-part deltas during a generation. The T1 seam ships a
      node-level stream (``stream_events``) and does not require this;
      future intra-turn streaming would. Captured now so resolver
      callers can branch.
    * :attr:`supports_prompt_cache` — whether the backend honours
      Anthropic-style ``cache_control`` (or an equivalent), which cuts
      input cost on stable system prompts. Anthropic + Bedrock-via-
      Anthropic-SDK do; OpenAI compat at the time of writing does not
      (vendor-specific knob); recorded so cost-attribution (#1079) can
      adjust pricing math per backend.
    * :attr:`tool_format` — the wire format the backend speaks for tool
      calls. Anthropic = ``"anthropic"`` (XML-shaped under the hood,
      surfaced as structured ``tool_use`` blocks by the SDK); OpenAI-
      compatible / Bedrock-non-Anthropic / vLLM-OpenAI-compat =
      ``"openai"``. The resolver does not branch on this directly today,
      but persisting it on the backend descriptor means a tool-format
      adapter (the seam Initiative #806 alludes to) can flip behaviour
      without re-walking every backend registration. The field is a
      free string (not a closed enum) deliberately — the format set
      grows as adapters land (Gemini-flavoured tool-calling, etc.) and
      the resolver does not need to know the value's domain.
    """

    supports_tools: bool
    supports_streaming: bool
    supports_prompt_cache: bool
    tool_format: str


#: Capability flags for ``pydantic_ai.models.anthropic.AnthropicModel``.
#: Anthropic supports the full tool-use loop, intra-turn streaming, and
#: ``cache_control`` prompt caching; tool format is ``anthropic``.
anthropic_capabilities: Final[BackendCapabilities] = BackendCapabilities(
    supports_tools=True,
    supports_streaming=True,
    supports_prompt_cache=True,
    tool_format="anthropic",
)


#: Capability flags for ``pydantic_ai.models.bedrock.BedrockConverseModel``
#: registered against an **Anthropic-family** model id (Claude 3.5+,
#: Claude 4.x — the consumer-facing tiers consumer-doc §C4 names).
#: Tools and intra-turn streaming work for every model the Converse API
#: serves; prompt caching is **per-model** on Bedrock (the
#: :class:`~pydantic_ai.providers.bedrock.BedrockModelProfile`
#: ``bedrock_supports_prompt_caching`` flag tracks the per-id allow-list
#: AWS publishes), and the Anthropic-on-Bedrock family is in the
#: caching-supported set. A deploy that registers a *non*-Anthropic
#: Bedrock model (Nova, Mistral, Cohere) should register it under a
#: separate backend id with a copy of these capabilities that flips
#: ``supports_prompt_cache=False`` to match the per-model profile.
#:
#: Tool format is ``"converse"`` — Bedrock's Converse API ``toolSpec``
#: shape, **not** the Anthropic-native XML tool-call format. The two
#: look similar from a tenant-facing distance (both surface "Claude
#: with tools") but route through different wire shapes, so a future
#: tool-format adapter (initiative #806 §C4) must branch on this
#: string rather than infer from the underlying model family.
bedrock_capabilities: Final[BackendCapabilities] = BackendCapabilities(
    supports_tools=True,
    supports_streaming=True,
    supports_prompt_cache=True,
    tool_format="converse",
)


#: A :class:`BackendBuilder` is a zero-arg callable that materialises one
#: backend's :class:`~pydantic_ai.models.Model`. Builders are registered
#: per backend id and called lazily — the resolver only builds the Model
#: it returns, so a deploy whose policy never references the
#: ``bedrock-anthropic`` backend never imports ``boto3`` (the
#: ``pydantic-ai-slim[bedrock]`` extra). The builder owns its own
#: configuration lookup (settings, secrets, provider construction) and
#: is responsible for failing closed if a credential is missing — the
#: same posture :func:`~meho_backplane.agent.run.default_model_factory`
#: had pre-resolver.
BackendBuilder = Callable[[], "Model"]


@dataclass(frozen=True, slots=True)
class TierMapping:
    """One tier's resolution: which backend builds the Model.

    The resolver looks up
    ``policy.tiers[tier]`` → :class:`TierMapping` → ``backend_id`` →
    :class:`BackendBuilder` registration → :class:`pydantic_ai.models.Model`.
    The mapping is the leaf the tenant policy points at — keeping it a
    distinct dataclass (rather than a bare string) lets a future enhancement
    add per-tier knobs (model id override, per-tier cache TTL, etc.) without
    re-shaping every tenant config in flight.
    """

    backend_id: str


@dataclass(frozen=True, slots=True)
class TenantModelPolicy:
    """One tenant's per-tier resolution policy.

    Frozen because a policy is loaded at resolver-build time and never
    mutated mid-flight. A run uses the policy snapshot the resolver was
    built with; reloading the resolver on settings change is the
    caller's responsibility (mirroring how settings are read elsewhere).

    The ``allow_egress`` field carries the egress posture the Goal #800
    body names as "the #1 enterprise blocker": ``False`` means
    air-gapped — the resolver refuses to materialise a SaaS-backed
    backend for this tenant. Whether a backend is "SaaS" is declared
    on the backend registration via :attr:`is_saas_egress`; this
    keeps the egress check a simple boolean lookup on the resolved
    backend rather than name-string matching.
    """

    tiers: Mapping[AgentTier, TierMapping]
    allow_egress: bool = True


#: Sentinel key for the **default tenant policy** used when a runtime
#: resolves a tier for a tenant that has no explicit entry. A separate
#: sentinel rather than an arbitrary "default tenant" UUID, so a tenant
#: that legitimately has no policy entry is unambiguously distinguished
#: from a tenant whose UUID happens to match one's misconfigured sentinel.
DEFAULT_TENANT_KEY: Final[str] = "__default__"


class ResolverError(RuntimeError):
    """Base type for every resolver-level failure.

    The seam in :mod:`meho_backplane.agent.run` catches this and surfaces
    it as :class:`~meho_backplane.agent.run.AgentRunError`, so callers see
    one error type regardless of which precise resolver mismatch fired.
    The subclasses below carry the diagnosable reason — useful in tests
    and in operator-facing logs without leaking provider-specific
    vocabulary back to the loop.
    """


class BackendNotConfiguredError(ResolverError):
    """A tier is mapped to a backend id that has no registered builder.

    Either (a) the policy names a backend that hasn't been registered (a
    typo, or a backend whose extra isn't installed in this deploy), or
    (b) the policy doesn't map this tier at all and the *default* tenant
    policy doesn't fill the gap. Distinct from
    :class:`CapabilityMismatchError` (the backend exists but can't honour
    the tier's needs) so log readers can tell config drift from a
    capability ask.
    """


class CapabilityMismatchError(ResolverError):
    """A tier needs a capability the resolved backend doesn't declare.

    Today the agent runtime always needs ``supports_tools=True`` (the loop
    is tool-use); a backend with ``supports_tools=False`` mapped to any
    tier the agent will run is a configuration error. Future
    capability-aware tiering (a ``stream`` tier requiring
    ``supports_streaming=True``, etc.) reuses this exception with the
    failing flag named in the message.
    """


class EgressViolationError(ResolverError):
    """A no-egress tenant resolved to a SaaS backend.

    Fail-closed because the entire premise of egress=False is "no log
    content leaves the tenant's deploy boundary"; one accidental SaaS
    call defeats the whole posture. Distinct error type so an air-gapped
    deployment's observability picks the egress break out of the noise
    of generic config errors and pages.
    """


@dataclass(frozen=True, slots=True)
class _BackendRegistration:
    """Internal: one row of the backend registry the resolver consults.

    Bundles the builder, the capability flags, and the SaaS-egress flag
    so the resolver makes one dict lookup per resolve rather than three
    parallel ones. Private — callers register through
    :meth:`build_resolver`'s ``backends=`` argument.
    """

    builder: BackendBuilder
    capabilities: BackendCapabilities
    is_saas_egress: bool


class ModelResolver(Protocol):
    """The narrow surface the agent runtime depends on.

    A structural :class:`~typing.Protocol` so the seam can hold the
    interface while a test or an alternate implementation (e.g. one that
    reads policy from the database when #1075's follow-ups land that
    persistence) supplies the implementation. The default
    implementation is :func:`build_resolver`'s return value.

    A single :meth:`resolve` call: returns the
    :class:`~pydantic_ai.models.Model` the loop should run against,
    given the run's operator and the definition's tier. The runtime
    passes the :class:`~meho_backplane.auth.operator.Operator` rather
    than the bare ``tenant_id`` so a future per-principal policy
    extension (per-agent-identity routing within a tenant) reuses the
    same signature.
    """

    def resolve(self, operator: Operator, tier: AgentTier) -> Model:
        """Return the Model for *tier* under *operator*'s tenant.

        Raises:
            BackendNotConfiguredError: the tier has no backend mapping.
            CapabilityMismatchError: the backend can't honour the tier.
            EgressViolationError: a no-egress tenant resolved to a SaaS backend.
        """
        ...


@dataclass(frozen=True, slots=True)
class _DefaultResolver:
    """Concrete :class:`ModelResolver` built by :func:`build_resolver`.

    Holds the per-tenant policies + the backend registry; resolves a
    tier in three steps: (1) look up the tenant's policy (falling back
    to the default-tenant policy when absent), (2) look up the backend
    id the tier maps to, (3) build the Model after enforcing egress +
    capability checks.
    """

    policies: Mapping[UUID | str, TenantModelPolicy]
    backends: Mapping[str, _BackendRegistration]

    def resolve(self, operator: Operator, tier: AgentTier) -> Model:
        """Return the Model for *tier* under *operator*'s tenant.

        See :class:`ModelResolver` for the exception contract.
        """
        policy = self._policy_for(operator.tenant_id)
        mapping = policy.tiers.get(tier)
        if mapping is None:
            raise BackendNotConfiguredError(
                f"no backend configured for tier '{tier.value}' "
                f"under tenant '{operator.tenant_id}' (and the default "
                f"tenant policy does not fill the gap)",
            )
        backend = self.backends.get(mapping.backend_id)
        if backend is None:
            raise BackendNotConfiguredError(
                f"tier '{tier.value}' maps to backend "
                f"'{mapping.backend_id}' but no builder is registered "
                f"for that id; check the deploy's pydantic-ai extras "
                f"and the backends= argument passed to build_resolver()",
            )
        if not policy.allow_egress and backend.is_saas_egress:
            raise EgressViolationError(
                f"tenant '{operator.tenant_id}' has allow_egress=False "
                f"but tier '{tier.value}' resolved to backend "
                f"'{mapping.backend_id}' which is flagged as SaaS egress; "
                f"refusing to materialise the model (data egress is the "
                f"#1 enterprise blocker — fail closed)",
            )
        if not backend.capabilities.supports_tools:
            # The agent runtime always needs tools — the loop is tool-use.
            # A future non-tool tier could relax this; today, every
            # tier-resolution targets the tool-use loop, so no-tools
            # backends are a configuration error.
            raise CapabilityMismatchError(
                f"tier '{tier.value}' resolved to backend "
                f"'{mapping.backend_id}' which declares "
                f"supports_tools=False; the agent runtime requires "
                f"tool support",
            )
        _log.info(
            "agent_model_resolved",
            tenant_id=str(operator.tenant_id),
            tier=tier.value,
            backend_id=mapping.backend_id,
            tool_format=backend.capabilities.tool_format,
        )
        return backend.builder()

    def _policy_for(self, tenant_id: UUID) -> TenantModelPolicy:
        """Look up *tenant_id*'s policy, falling back to the default key.

        The fallback is the architectural commitment that "a single-
        tenant deploy doesn't need to enumerate its tenant id" — the
        ``__default__`` policy is the legacy single-provider path
        recovered. Returning :class:`BackendNotConfiguredError` here
        would be premature: per-tier resolution is the level where
        the failure mode is meaningful (the tenant may have a partial
        policy that covers some tiers and not others).
        """
        explicit = self.policies.get(tenant_id)
        if explicit is not None:
            return explicit
        default = self.policies.get(DEFAULT_TENANT_KEY)
        if default is not None:
            return default
        # The resolver was built with no default policy and the tenant has
        # no explicit one. Surface a clear error rather than a KeyError so
        # the operator's log read picks it out of dispatch-stack noise.
        raise BackendNotConfiguredError(
            f"no model policy configured for tenant '{tenant_id}' "
            f"and no '{DEFAULT_TENANT_KEY}' fallback registered",
        )


def build_resolver(
    *,
    policies: Mapping[UUID | str, TenantModelPolicy],
    backends: Mapping[str, tuple[BackendBuilder, BackendCapabilities, bool]],
) -> ModelResolver:
    """Build a :class:`ModelResolver` from policies + backend registrations.

    *policies* maps a tenant UUID (or :data:`DEFAULT_TENANT_KEY`) to its
    :class:`TenantModelPolicy`. *backends* maps a backend id to a triple
    ``(builder, capabilities, is_saas_egress)`` — the builder constructs
    the :class:`~pydantic_ai.models.Model` lazily, the capabilities are
    consulted by the resolver, and ``is_saas_egress`` is what the
    egress check reads. Tests build per-test resolvers with a stub
    backend; production callers build one resolver at app boot from
    settings.

    The triple shape (rather than a dataclass per registration) keeps
    the call-site terse without losing field naming at the storage
    layer; the internal :class:`_BackendRegistration` does the naming.
    """
    registrations: dict[str, _BackendRegistration] = {
        backend_id: _BackendRegistration(
            builder=builder,
            capabilities=capabilities,
            is_saas_egress=is_saas_egress,
        )
        for backend_id, (builder, capabilities, is_saas_egress) in backends.items()
    }
    return _DefaultResolver(policies=dict(policies), backends=registrations)


def anthropic_backend_builder() -> Model:
    """Build an Anthropic :class:`~pydantic_ai.models.Model` from settings.

    Lifted from the original
    :func:`~meho_backplane.agent.run.default_model_factory` so the existing
    Anthropic path keeps working *through* the resolver: a deploy whose
    policy maps every tier to backend id ``"anthropic"`` and registers
    this builder behaves identically to the pre-resolver code (default
    tenant → Anthropic, fail-closed on missing key).

    Fail-closed: a deploy with no ``ANTHROPIC_API_KEY`` configured raises
    :class:`~meho_backplane.agent.run.AgentRunError` here rather than
    surfacing an opaque framework error mid-loop. The import is
    function-local so a deployment whose policy never references this
    builder (e.g. an air-gapped tenant routing every tier to vLLM)
    doesn't load the ``anthropic`` package at all.
    """
    from anthropic import AsyncAnthropic
    from pydantic_ai.models.anthropic import AnthropicModel
    from pydantic_ai.providers.anthropic import AnthropicProvider

    # Imported lazily so this module doesn't form an import cycle with
    # the run module (``run.py`` imports this module's symbols).
    from meho_backplane.agent.run import AgentRunError
    from meho_backplane.settings import get_settings

    settings = get_settings()
    api_key = settings.anthropic_api_key
    if not api_key:
        raise AgentRunError(
            "no ANTHROPIC_API_KEY configured for the agent runtime; "
            "set it to run against Anthropic, or route this tenant to "
            "an on-prem backend (Bedrock/vLLM/PAIF) — see G11.5.",
        )
    provider = AnthropicProvider(anthropic_client=AsyncAnthropic(api_key=api_key))
    return AnthropicModel(settings.agent_default_model, provider=provider)


def default_anthropic_backends() -> dict[str, tuple[BackendBuilder, BackendCapabilities, bool]]:
    """Return the Anthropic-direct slice of the built-in backend registry.

    The Anthropic-only entry, kept as its own helper so a deploy that
    routes every tier to Anthropic (the pre-G11.5 recovery shape) does
    not need to know Bedrock exists. The companion
    :func:`default_bedrock_backends` adds the Bedrock entry; a deploy
    that wants both calls ``{**default_anthropic_backends(),
    **default_bedrock_backends()}`` at the resolver-build site. OpenAI-
    compat (#1077) and VCF PAIF (#1078) builders land their own helpers
    on the same pattern.

    The Anthropic backend is flagged :attr:`is_saas_egress` ``=True``:
    routes content to ``api.anthropic.com``, the SaaS endpoint, so a
    no-egress tenant cannot resolve to it. A future on-prem-Claude
    backend would register a *different* backend id with
    ``is_saas_egress=False``.
    """
    return {
        "anthropic": (
            anthropic_backend_builder,
            anthropic_capabilities,
            True,  # is_saas_egress: api.anthropic.com is SaaS.
        ),
    }


def bedrock_backend_builder() -> Model:
    """Build an AWS Bedrock :class:`~pydantic_ai.models.Model` from settings.

    Uses pydantic_ai's :class:`~pydantic_ai.models.bedrock.BedrockConverseModel`
    + :class:`~pydantic_ai.providers.bedrock.BedrockProvider` — the
    boto3-backed Converse API path. The Bedrock provider resolves AWS
    credentials through boto3's standard chain (environment variables,
    IAM-role / EC2 instance metadata, shared profile, …), so a deploy
    typically provides only the region; the credentials come from the
    pod's IRSA role on EKS, the EC2 instance profile elsewhere, or the
    ``AWS_*`` env vars in dev.

    Fail-closed: if :attr:`~meho_backplane.settings.Settings.bedrock_region`
    is unset *and* boto3's own region resolution returns nothing (no
    ``AWS_DEFAULT_REGION`` / ``AWS_REGION`` / shared-profile region), the
    underlying provider raises ``NoRegionError`` mid-construction. The
    builder wraps that in :class:`~meho_backplane.agent.run.AgentRunError`
    so callers see one error type. Imports are function-local: a
    deployment whose policy never routes to ``bedrock-anthropic`` never
    loads boto3 (the ``[bedrock]`` extra is *installed* in every wheel
    but *unused* on Anthropic-only deploys).

    Why a single shared registration (rather than one per model id):
    Bedrock model ids name the underlying foundation model, but the
    *capability surface* (tools + streaming + Converse) is the same
    across the Anthropic-on-Bedrock family. The pinned default
    (:attr:`~meho_backplane.settings.Settings.bedrock_default_model`) is
    the Claude id the tenant policy resolves to. A deploy that needs to
    swap *between* Claude families per tier registers additional backend
    ids alongside (``bedrock-anthropic-opus``, ``bedrock-amazon-nova``,
    …) each with their own per-model capability flags (Nova does not
    advertise prompt caching, for example) — the call-site dict layered
    on top of :func:`default_bedrock_backends`.
    """
    from pydantic_ai.models.bedrock import BedrockConverseModel
    from pydantic_ai.providers.bedrock import BedrockProvider

    # Imported lazily so this module doesn't form an import cycle with
    # the run module (``run.py`` imports this module's symbols).
    from meho_backplane.agent.run import AgentRunError
    from meho_backplane.settings import get_settings

    settings = get_settings()
    # ``bedrock_region`` empty (the default) defers to boto3's own
    # region-resolution chain. The provider raises ``NoRegionError``
    # if every source comes up empty — re-raised here as the seam's
    # uniform error type so callers don't need to import botocore.
    region = settings.bedrock_region or None
    try:
        provider = BedrockProvider(region_name=region)
    except Exception as exc:  # botocore.exceptions.NoRegionError + auth errors
        raise AgentRunError(
            "could not construct AWS Bedrock provider for the agent runtime; "
            "set BEDROCK_REGION (or one of AWS_DEFAULT_REGION / AWS_REGION) "
            f"and ensure boto3 credentials resolve: {exc}",
        ) from exc
    return BedrockConverseModel(settings.bedrock_default_model, provider=provider)


def default_bedrock_backends() -> dict[str, tuple[BackendBuilder, BackendCapabilities, bool]]:
    """Return the Bedrock slice of the built-in backend registry.

    Registers one Bedrock entry under the id ``"bedrock-anthropic"``:
    pydantic_ai's :class:`~pydantic_ai.models.bedrock.BedrockConverseModel`
    pointed at the pinned Anthropic-on-Bedrock model
    (:attr:`~meho_backplane.settings.Settings.bedrock_default_model`).
    The id is family-tagged rather than just ``"bedrock"`` so a deploy
    that needs to route different tiers to different Bedrock families
    (Anthropic / Nova / Mistral) registers additional ids
    (``"bedrock-amazon-nova"``, …) alongside without re-keying the
    existing one.

    The default registration flags :attr:`is_saas_egress` ``=True``:
    public Bedrock endpoints route data to ``bedrock-runtime.<region>.
    amazonaws.com``, which crosses the tenant's deploy boundary on the
    public internet. A tenant that brokers Bedrock over AWS PrivateLink
    or VPC endpoints (so traffic stays on AWS-private networking and
    never traverses the public internet) registers the *same* builder
    under a different backend id with ``is_saas_egress=False`` — the
    egress check reads the per-registration flag, not the backend's
    name. The companion tests in
    ``backend/tests/test_agent_model_resolver.py`` cover both postures.
    """
    return {
        "bedrock-anthropic": (
            bedrock_backend_builder,
            bedrock_capabilities,
            True,  # is_saas_egress: public Bedrock endpoint is SaaS.
        ),
    }


def default_anthropic_policy() -> TenantModelPolicy:
    """Return the default-tenant policy mapping every tier to Anthropic.

    The recovery shape: a deploy that hasn't onboarded any tenant-specific
    policy yet routes every tier through the existing Anthropic path —
    bit-for-bit equivalent to the pre-resolver default behaviour. Callers
    pass ``{DEFAULT_TENANT_KEY: default_anthropic_policy()}`` to
    :func:`build_resolver` for this posture.
    """
    return TenantModelPolicy(
        tiers={tier: TierMapping(backend_id="anthropic") for tier in AgentTier},
        allow_egress=True,
    )


# Re-export the (private) registration row so a downstream task that
# builds a resolver from a richer config (a database row, a richer JSON)
# can construct the registry directly without going through the
# call-site triple of :func:`build_resolver`. Keeping it underscore-
# prefixed signals "stable but advanced".
_ = _BackendRegistration  # silence "unused" linters; documented surface.


# ---------------------------------------------------------------------------
# OpenAI-compatible backend (G11.5-T3 #1077)
# ---------------------------------------------------------------------------
#
# The OpenAI-compatible surface covers three deployment shapes the
# Initiative #806 §C4 calls out: **OpenAI SaaS** (``api.openai.com``),
# **vLLM** on-prem (a Python inference server exposing the OpenAI
# Chat Completions wire format under ``/v1``), and **Ollama** local
# (the same wire format with quirks documented at
# https://docs.vllm.ai/en/latest/features/tool_calling/ and
# https://github.com/ollama/ollama/blob/main/docs/openai.md).
# All three share the *transport* (OpenAI Chat Completions) but
# differ on which sub-features the underlying engine actually
# implements; pydantic_ai exposes those quirks as
# :class:`~pydantic_ai.profiles.openai.OpenAIModelProfile` fields.
#
# The shape mirrors :func:`anthropic_backend_builder` — a zero-arg
# closure the resolver registers — but with an explicit constructor
# (:func:`openai_compat_backend_builder`) since a multi-tenant deploy
# typically registers *several* OpenAI-compat backends (one per
# on-prem endpoint, one for OpenAI SaaS), each with its own
# ``base_url`` / ``api_key`` / ``model_id``. The settings-driven
# default (:func:`default_openai_backend_builder`) reproduces the
# single-tenant convenience the Anthropic builder offers.


class OpenAICompatVendor(StrEnum):
    """The OpenAI-compatible deployment shapes the builder distinguishes.

    Each enum member picks a pre-baked :class:`OpenAIModelProfile` whose
    flags reflect the vendor's documented quirks (see module docstring
    above). Adding a vendor is two lines: a new enum member and a
    matching ``_vendor_profile_for(...)`` branch. The set is deliberately
    closed (StrEnum) so a tenant policy listing an unknown vendor fails
    at config-load time rather than mid-loop.

    * :attr:`OPENAI` — OpenAI SaaS (``api.openai.com``). Full feature
      surface, including strict tool definitions and multiple system
      messages.
    * :attr:`VLLM` — vLLM on-prem. Tool calling supported (see vLLM
      docs §"Tool calling"), but the engine does *not* honour OpenAI's
      ``strict: true`` tool-definition contract — the inference server
      treats the schema as advisory. Surfaced via
      ``openai_supports_strict_tool_definition=False`` so the loop
      will not send the strict flag (which vLLM would silently ignore,
      letting the model emit a non-conforming call).
    * :attr:`OLLAMA` — Ollama local. Same wire format, plus two extra
      restrictions: no strict tool defs (per Ollama's OpenAI-compat
      docs), and the OpenAI ``messages[]`` shape with *multiple*
      ``role=system`` entries is collapsed to a single one (the
      framework prepends them with newlines instead of sending them
      as distinct turns), which surfaces here as
      ``openai_chat_supports_multiple_system_messages=False``.
    """

    OPENAI = "openai"
    VLLM = "vllm"
    OLLAMA = "ollama"


def openai_chat_profile() -> OpenAIModelProfile:
    """Return the capability profile for OpenAI SaaS Chat Completions.

    Bit-equivalent to pydantic_ai's bundled defaults at the OpenAI
    Chat Completions surface, returned explicitly so the three vendor
    profiles read at the same level of abstraction and a future API
    change at the framework level (a default flip on a flag) surfaces
    as a diff in this file rather than a silent behaviour change at
    runtime. Constructing the profile inside a function (vs. a module
    constant) keeps the lazy-import discipline: the import only fires
    when an OpenAI-compat backend is actually registered.
    """
    from pydantic_ai.profiles.openai import OpenAIModelProfile

    return OpenAIModelProfile(
        openai_supports_strict_tool_definition=True,
        openai_chat_supports_multiple_system_messages=True,
        json_schema_transformer=None,
    )


def vllm_chat_profile() -> OpenAIModelProfile:
    """Return the capability profile for vLLM behind the OpenAI shim.

    Flips ``openai_supports_strict_tool_definition`` to ``False``: vLLM
    accepts the ``strict: true`` flag on its REST surface but the engine
    does not enforce the schema (see vLLM tool-calling docs); leaving
    the framework to send the flag would let the model emit a non-
    conforming tool call that the loop then rejects on a structural
    mismatch. Multiple system messages and the default JSON-schema
    pipeline are honoured.
    """
    from pydantic_ai.profiles.openai import OpenAIModelProfile

    return OpenAIModelProfile(
        openai_supports_strict_tool_definition=False,
        openai_chat_supports_multiple_system_messages=True,
        json_schema_transformer=None,
    )


def ollama_chat_profile() -> OpenAIModelProfile:
    """Return the capability profile for Ollama behind the OpenAI shim.

    Two restrictions versus OpenAI SaaS:

    * ``openai_supports_strict_tool_definition=False`` — Ollama's
      ``openai`` compat layer ignores the strict flag (same posture as
      vLLM).
    * ``openai_chat_supports_multiple_system_messages=False`` —
      Ollama's chat template collapses multiple ``role=system`` turns
      into one, so the framework merges them before sending. Without
      this flag the loop would forward each system part as a separate
      message and Ollama would render the conversation history in an
      order the prompt template did not expect.
    """
    from pydantic_ai.profiles.openai import OpenAIModelProfile

    return OpenAIModelProfile(
        openai_supports_strict_tool_definition=False,
        openai_chat_supports_multiple_system_messages=False,
        json_schema_transformer=None,
    )


def _vendor_profile_for(vendor: OpenAICompatVendor) -> OpenAIModelProfile:
    """Map a :class:`OpenAICompatVendor` to its bundled profile.

    Internal — the public surface is the three named profile factories
    above (``openai_chat_profile`` / ``vllm_chat_profile`` /
    ``ollama_chat_profile``), so a caller wanting one of the three
    constructs it directly. This helper exists so
    :func:`openai_compat_backend_builder` can dispatch from a vendor
    enum without forcing every call site to remember which profile
    corresponds to which vendor.
    """
    if vendor is OpenAICompatVendor.OPENAI:
        return openai_chat_profile()
    if vendor is OpenAICompatVendor.VLLM:
        return vllm_chat_profile()
    if vendor is OpenAICompatVendor.OLLAMA:
        return ollama_chat_profile()
    # Closed enum — a new member added without a branch above would
    # land in mypy's exhaustiveness check; this raise is the runtime
    # belt for the same misuse.
    raise ValueError(f"unsupported OpenAI-compat vendor: {vendor!r}")


# ----- Capability flags --------------------------------------------------
#
# The resolver consults :class:`BackendCapabilities` to decide whether a
# backend can honour a tier; OpenAI-compat backends share the same broad
# shape (tools yes, streaming yes, prompt-cache no) regardless of vendor.
# Prompt caching is OFF because none of the three OpenAI-compat surfaces
# implement the Anthropic-style ``cache_control`` knob today (OpenAI's
# automatic input caching is opaque to the client and does not require
# explicit declaration; vLLM/Ollama don't expose any equivalent). The
# cost-attribution layer (#1079) reads this flag to decide whether to
# model a per-message cache discount, so flipping this to ``True`` later
# is a real billing change — keep it ``False`` until a vendor exposes
# the knob explicitly.


#: Capability flags for an OpenAI-compatible backend
#: (``pydantic_ai.models.openai.OpenAIChatModel`` over OpenAI / vLLM /
#: Ollama). Tools and streaming are honoured by every supported vendor;
#: prompt caching is off because none of the three exposes the
#: Anthropic-style ``cache_control`` knob. ``tool_format`` is
#: ``"openai"`` — the wire format every OpenAI-compat surface speaks.
openai_compat_capabilities: Final[BackendCapabilities] = BackendCapabilities(
    supports_tools=True,
    supports_streaming=True,
    supports_prompt_cache=False,
    tool_format="openai",
)


# ----- Backend builders --------------------------------------------------


def openai_compat_backend_builder(
    *,
    vendor: OpenAICompatVendor,
    model_id: str,
    base_url: str | None = None,
    api_key: str | None = None,
) -> BackendBuilder:
    """Build a zero-arg :class:`BackendBuilder` for one OpenAI-compat backend.

    Returns a closure the resolver registers; the closure constructs a
    fresh :class:`~pydantic_ai.models.openai.OpenAIChatModel` on each
    call, the same lazy-build posture the Anthropic builder uses (so
    settings reloads pick up at the next resolve without a resolver
    rebuild). The vendor-specific quirks ride on the
    :class:`OpenAIModelProfile` picked from ``vendor``.

    Args:
        vendor: Which OpenAI-compat deployment shape to target — picks
            the bundled :class:`OpenAIModelProfile`.
        model_id: The framework's model id. Accepts the raw model name
            (``gpt-4o-mini``, ``meta-llama/Llama-3.1-8B-Instruct``,
            ``llama3.1:8b``) or the ``openai:<name>`` prefix; the
            framework strips the prefix internally so both shapes work.
        base_url: The OpenAI Chat Completions base URL. ``None`` (the
            default) routes to OpenAI SaaS. An on-prem deploy passes
            the engine's URL (e.g. ``http://vllm.internal:8000/v1`` /
            ``http://ollama.internal:11434/v1`` / the VCF PAIF
            ``…/api/v1/compatibility/openai/v1/``).
        api_key: The bearer token sent on every request. ``None``
            tells the provider to fall back to ``OPENAI_API_KEY``;
            most on-prem endpoints accept any non-empty string but
            still require *some* value (the OpenAI SDK refuses to
            send a request without an API key argument). The value
            never leaks into structured logs — :class:`OpenAIProvider`
            stores it as a private attribute on the client.

    Returns:
        A :class:`BackendBuilder` suitable for the ``backends=`` argument
        of :func:`build_resolver`. The builder is **lazy**: pydantic_ai
        and the OpenAI SDK only import when the resolver calls it, so
        a deploy that registers an OpenAI-compat backend but never
        resolves to it never loads the ``openai`` wheel into memory.
    """
    profile = _vendor_profile_for(vendor)

    def _build() -> Model:
        # Lazy import — the ``pydantic-ai-slim[openai]`` extra pulls in
        # the ``openai`` SDK and ``tiktoken``; deploys that route every
        # tier to Anthropic should not pay those imports. The closure
        # captures only Python primitives so it stays cheap to register.
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider

        provider = OpenAIProvider(base_url=base_url, api_key=api_key)
        return OpenAIChatModel(model_id, provider=provider, profile=profile)

    return _build


def default_openai_backend_builder() -> Model:
    """Build the settings-driven default OpenAI-compatible Model.

    Reads ``openai_api_key`` / ``openai_base_url`` / ``openai_default_model``
    from :class:`~meho_backplane.settings.Settings` and picks a vendor
    profile from the base URL host hint: a URL containing ``ollama``
    picks the Ollama profile, ``vllm`` picks vLLM, everything else
    (including the empty string for OpenAI SaaS) picks the OpenAI
    profile. The host-hint heuristic is deliberately weak — operators
    routing to an endpoint the hint misses register their own backend
    via :func:`openai_compat_backend_builder` with the vendor passed
    explicitly.

    Fail-closed: empty ``openai_api_key`` raises
    :class:`~meho_backplane.agent.run.AgentRunError`, mirroring
    :func:`anthropic_backend_builder`'s posture so a deploy that
    registered an OpenAI-compat backend but never wired credentials
    surfaces at first agent invocation rather than mid-loop.
    """
    # Imported lazily so this function only pays the cost when the
    # resolver actually resolves to OpenAI-compat (the
    # :class:`AgentRunError` import would otherwise tie this module's
    # import time to ``agent.run`` and its dependencies).
    from meho_backplane.agent.run import AgentRunError
    from meho_backplane.settings import get_settings

    settings = get_settings()
    api_key = settings.openai_api_key
    if not api_key:
        raise AgentRunError(
            "no OPENAI_API_KEY configured for the agent runtime; "
            "set it to route a tier to OpenAI / vLLM / Ollama via the "
            "default OpenAI-compat backend, or register a per-backend "
            "builder via openai_compat_backend_builder(...) — see G11.5.",
        )
    base_url = settings.openai_base_url or None
    vendor = _vendor_from_base_url_hint(base_url)
    builder = openai_compat_backend_builder(
        vendor=vendor,
        model_id=settings.openai_default_model,
        base_url=base_url,
        api_key=api_key,
    )
    return builder()


def _vendor_from_base_url_hint(base_url: str | None) -> OpenAICompatVendor:
    """Pick an :class:`OpenAICompatVendor` from a base URL host hint.

    Cheap heuristic for the settings-driven default builder. A real
    multi-endpoint deploy registers each backend explicitly via
    :func:`openai_compat_backend_builder` rather than relying on this
    function; it exists only so the single-knob single-tenant default
    picks the right profile out of the three common shapes.
    """
    if base_url is None:
        return OpenAICompatVendor.OPENAI
    lowered = base_url.lower()
    if "ollama" in lowered:
        return OpenAICompatVendor.OLLAMA
    if "vllm" in lowered:
        return OpenAICompatVendor.VLLM
    return OpenAICompatVendor.OPENAI


if TYPE_CHECKING:
    # The vendor profile factories return concrete
    # :class:`OpenAIModelProfile` instances. Importing the class
    # under :data:`TYPE_CHECKING` keeps the strict-mypy contract
    # while preserving the runtime lazy-import posture (the actual
    # class is imported inside each factory at call time).
    from pydantic_ai.profiles.openai import OpenAIModelProfile


# ---------------------------------------------------------------------------
# VCF Private AI Foundation backend (G11.5-T4 #1078)
# ---------------------------------------------------------------------------
#
# VCF Private AI Foundation (PAIF) is VMware's air-gapped on-prem inference
# platform. The Initiative #806 §C4 calls out PAIF as the **zero-egress**
# target: a tenant with ``allow_egress=False`` routes every tier through a
# PAIF endpoint and **no** request leaves the cluster boundary. The
# Broadcom developer docs (https://developer.broadcom.com/xapis/
# vmware-private-ai-service-api/latest/) document that PAIF exposes an
# **OpenAI-compatible surface** under a fixed sub-path; the
# Broadcom techdocs (https://techdocs.broadcom.com/.../deploying-model-endpoints.html)
# confirm the underlying engine set: **vLLM** for chat completions on
# GPU, **Infinity** for embeddings, **llama.cpp** for CPU fallback.
#
# What that means for this slice:
#
# * The wire format is OpenAI Chat Completions verbatim — same
#   :class:`OpenAIChatModel` + :class:`OpenAIProvider` as the C4-c slice
#   (#1077). No bespoke client.
# * The base URL takes a **non-standard sub-path** —
#   ``<host>/api/v1/compatibility/openai/v1/`` — distinct from OpenAI
#   SaaS's ``api.openai.com/v1`` or vLLM's bare ``/v1``. The
#   :data:`VCF_PAIF_OPENAI_COMPAT_BASE_PATH` constant pins the path so
#   a deploy that mis-types the sub-path fails at config-load time
#   rather than at first agent invocation. Operators pass the
#   appliance host; the builder appends the path.
# * The engine is vLLM, so the same profile quirks apply: strict tool
#   definitions are advisory (the engine does not enforce them), and
#   multiple system messages are honoured. Reuses
#   :func:`vllm_chat_profile` rather than declaring a new profile —
#   surfacing PAIF as "vLLM behind a different sub-path with a
#   bearer header instead of an API key" is the honest abstraction.
# * The auth is **OpenID bearer** (Authorization Code with PKCE for
#   interactive clients, Resource Owner Password Flow / client
#   credentials for non-interactive). Bearer tokens are short-lived
#   and rotated by the IdP; the builder takes a **token provider**
#   callable (async, ``str | Callable[[], Awaitable[str]]``) so the
#   :class:`AsyncOpenAI` client can re-resolve the token on every
#   request — letting the OIDC layer transparently refresh expiring
#   tokens without rebuilding the resolver. This is the auth-pattern
#   choice the framework supports natively (``openai>=2.0`` accepts a
#   ``Callable[[], Awaitable[str]]`` as ``api_key=``).
# * The :class:`BackendCapabilities` flags match the vLLM ones from
#   #1077: tools yes, streaming yes, prompt-cache no, tool format
#   ``"openai"`` — none of the underlying engines exposes the
#   Anthropic-style ``cache_control`` knob.


#: The fixed sub-path PAIF mounts the OpenAI-compatible API under
#: (Broadcom developer docs). An operator configures the appliance
#: host (e.g. ``https://pais.airgap.local``) and the builder appends
#: this path; declaring the constant here lets a misconfigured
#: ``OPENAI_BASE_URL`` (operator pointed the legacy single-knob
#: setting at a PAIF host but forgot the sub-path) be diagnosed
#: against this anchor in a future preflight check, and stops the
#: literal path string from drifting across the code/docs surfaces.
VCF_PAIF_OPENAI_COMPAT_BASE_PATH: Final[str] = "/api/v1/compatibility/openai/v1/"


#: An async callable that, when invoked, returns a current bearer
#: token for the PAIF endpoint. The shape matches what
#: :class:`openai.AsyncOpenAI` accepts as ``api_key=`` — a
#: ``Callable[[], Awaitable[str]]`` — so the openai SDK calls it
#: on every request and gets a fresh value, no provider rebuild
#: needed when the IdP rotates the token. Callers can pass either
#: the bundled :class:`OidcClientCredentialsTokenProvider` (the
#: settings-driven default) or a custom callable backed by their
#: own secret manager (Vault dynamic credentials, a sidecar that
#: rotates a file on disk, etc.).
BearerTokenProvider = Callable[[], Awaitable[str]]


class TokenAcquisitionError(RuntimeError):
    """Raised when the OIDC token endpoint refuses the bearer request.

    Distinct error type so an operator's log read picks the failure
    out of the noise of generic transport errors and pages — a PAIF
    deploy with a misconfigured client secret or a revoked client
    surfaces here, not as an opaque ``httpx.HTTPStatusError`` deep
    inside the agent loop. The error message names the token
    endpoint and the IdP's error response (when present) so the
    operator can map back to a Keycloak / Okta / Authentik client
    config without spelunking through structured logs.

    Not wrapped in :class:`ResolverError` because token failure is a
    *runtime* condition (the IdP is down, the secret was rotated
    out-of-band, etc.) rather than a configuration mismatch
    discovered at resolve time. The seam at
    :meth:`~meho_backplane.agent.run.PydanticAgentRun._resolve_model`
    treats unwrapped runtime exceptions as the framework's domain;
    bubbling :class:`TokenAcquisitionError` up surfaces the failure
    as the loop's :attr:`AgentRunEventKind.ERROR` event with the
    typed reason preserved.
    """


@dataclass(slots=True)
class OidcClientCredentialsTokenProvider:
    """Acquire + cache bearer tokens via the OIDC ``client_credentials`` grant.

    A reusable, thread-safe token provider that implements the
    OAuth 2.0 ``client_credentials`` grant against a configured OIDC
    token endpoint, caches the access token until shortly before its
    expiry, and re-acquires on demand. The instance is callable
    (``await provider()`` returns a fresh string), matching the
    :data:`BearerTokenProvider` shape :class:`openai.AsyncOpenAI`
    accepts as ``api_key=``.

    Why ``client_credentials`` is the default grant for PAIF:

    * The agent runtime is a **service-to-service** caller (the
      backplane talks to PAIF on behalf of a tenant, not on behalf of
      a human user). Authorization-Code-with-PKCE — the PAIF docs'
      "preferred" grant — fits an interactive client; the
      client-credentials grant fits a daemon. Keycloak / Okta /
      Authentik / Auth0 all support both grants on the same realm,
      so swapping is an IdP-side config change, not a code change.
    * The token belongs to the **deploy**, not to the agent's
      operator: every tenant routed to this PAIF endpoint shares the
      same machine identity. Per-tenant authorization is enforced
      one layer up — by the tenant policy that maps the tier to this
      backend in the first place — not by per-call token issuance.
      (A future per-tenant token-issuance mode would supply a
      different :data:`BearerTokenProvider` callable; the builder
      surface stays unchanged.)

    Caching contract:

    * The provider stores the latest ``access_token`` + an absolute
      expiry timestamp (``time.monotonic() + expires_in -
      refresh_skew_seconds``). Subsequent calls within the skew
      window return the cached token without re-hitting the IdP.
    * **Refresh skew** defaults to 30 s — long enough to mask a slow
      network or a slow IdP, short enough not to throw away most of
      a typical 5 to 15 min access-token lifetime. Operators with very
      short-lived tokens (sub-minute) tune this down at construction.
    * The cache is guarded by a :class:`threading.Lock`: the openai
      SDK can call the provider concurrently from multiple in-flight
      requests on the same client, and without the lock two
      requests racing past the expiry would each fire a token POST.
      The lock is held only across the in-memory check + the HTTP
      call's *kick-off*; concurrent callers that arrive during a
      refresh-in-flight find a refreshed value on retry.

    Why no refresh-token plumbing:

    * The ``client_credentials`` grant in OAuth 2.0 / OIDC does **not**
      return a refresh token (`RFC 6749 §4.4.3
      <https://datatracker.ietf.org/doc/html/rfc6749#section-4.4.3>`_).
      The recovery path is "re-issue an access token by re-running
      the grant" — exactly what this class does on cache miss.
      Adding a refresh-token branch would be dead code for the
      grant we actually use; if a deploy switches to ROPC or
      Authorization-Code grants (both of which *do* issue refresh
      tokens), that's a different provider class.

    Args:
        token_url: The IdP's token endpoint (Keycloak:
            ``https://kc.airgap.local/realms/<realm>/protocol/openid-connect/token``).
        client_id: The OIDC client id registered with the IdP for
            the backplane → PAIF integration.
        client_secret: The OIDC client secret. Read from Vault /
            external-secret / sealed-secret upstream; the provider
            does not log the value (stored as a private attribute,
            never included in ``__repr__``).
        scope: Optional ``scope`` parameter to send with the token
            request. Most IdPs default to a sensible OIDC scope; PAIF
            deployments using fine-grained scopes pass the relevant
            value here.
        http_client: Optional :class:`httpx.AsyncClient`. When ``None``
            the provider constructs a short-lived per-call client —
            simpler in tests, but in production a long-lived client
            (with connection pooling) sized to the agent runtime's
            concurrency should be passed in.
        refresh_skew_seconds: How many seconds before the IdP-reported
            expiry to treat the cached token as expired. Defaults to
            30 s; tune down for sub-minute access tokens.
    """

    token_url: str
    client_id: str
    client_secret: str
    scope: str | None = None
    http_client: httpx.AsyncClient | None = None
    refresh_skew_seconds: float = 30.0
    # Cache state. The two paired fields are mutated together under
    # the lock; an absolute monotonic expiry (rather than wall-clock)
    # is robust to clock jumps. ``init=False`` keeps them out of the
    # generated ``__init__`` — callers only configure the IdP-facing
    # surface above, never seed the cache.
    _cached_token: str | None = field(default=None, init=False)
    _cached_expiry_monotonic: float = field(default=0.0, init=False)
    _lock: Lock = field(init=False, repr=False, default_factory=Lock)

    async def __call__(self) -> str:
        """Return a non-expired access token, acquiring one if needed.

        The hot path is the cache hit: check the expiry under the lock,
        return the cached value. On miss, drop the lock for the HTTP
        round-trip (so concurrent callers aren't blocked on the IdP),
        then re-acquire to commit the new cache state. The double-check
        on re-acquire avoids two concurrent misses both firing a POST.

        Raises:
            TokenAcquisitionError: the IdP returned a non-2xx response
                or a body without ``access_token`` / ``expires_in``.
                The cause chain preserves the underlying
                ``httpx.HTTPStatusError`` or ``KeyError`` for debug
                triage; the message names the token endpoint and the
                IdP's ``error`` field when present.
        """
        with self._lock:
            if self._cached_token is not None and time.monotonic() < self._cached_expiry_monotonic:
                return self._cached_token

        # Drop the lock for the network round-trip — concurrent callers
        # waiting here would be blocked on the IdP, not on a hot path.
        token, expires_in = await self._fetch_token()

        with self._lock:
            # Re-check under the lock in case a concurrent caller beat
            # us to the refresh; the freshest value wins. Using ``time.
            # monotonic()`` (not ``time.time()``) so a wall-clock jump
            # mid-acquire doesn't immediately invalidate the new token.
            self._cached_token = token
            self._cached_expiry_monotonic = (
                time.monotonic() + expires_in - self.refresh_skew_seconds
            )
            return token

    async def _fetch_token(self) -> tuple[str, float]:
        """POST the ``client_credentials`` grant; return ``(token, expires_in)``.

        Constructs an HTTP client if the caller didn't supply one — a
        short-lived per-call client is simpler to reason about in tests,
        but the per-call cost (TCP+TLS handshake to the IdP) is fine for
        the low-cadence token-refresh path. Production deploys with very
        short-lived tokens supply a long-lived ``http_client`` with
        connection pooling.

        The grant body uses ``application/x-www-form-urlencoded`` per
        RFC 6749 §4.4.2; the SDK-style JSON body is **not** what most
        IdPs accept. ``scope`` is included only when non-empty (some
        IdPs reject an empty ``scope`` parameter as malformed).
        """
        data: dict[str, str] = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        if self.scope:
            data["scope"] = self.scope
        try:
            if self.http_client is not None:
                response = await self.http_client.post(self.token_url, data=data)
            else:
                async with httpx.AsyncClient() as client:
                    response = await client.post(self.token_url, data=data)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            # Surface the IdP's ``error`` field when present so the
            # operator's log read maps to an IdP-side misconfiguration
            # (invalid_client, invalid_grant, ...) without spelunking
            # through structured logs.
            idp_error = ""
            with contextlib.suppress(Exception):
                # ``response.json()`` raises on non-JSON / malformed
                # bodies; ``.get`` raises if the JSON root isn't a
                # dict. Either way, falling back to an empty error
                # name is fine — we still surface the status code.
                idp_error = exc.response.json().get("error", "")
            raise TokenAcquisitionError(
                f"OIDC token endpoint {self.token_url!r} returned "
                f"HTTP {exc.response.status_code}"
                + (f" ({idp_error!r})" if idp_error else "")
                + "; check the PAIF deploy's OIDC client id/secret and "
                "verify the client has the client_credentials grant enabled.",
            ) from exc
        except httpx.HTTPError as exc:
            raise TokenAcquisitionError(
                f"could not reach OIDC token endpoint {self.token_url!r} "
                f"to acquire a PAIF bearer token: {exc}",
            ) from exc
        try:
            token = payload["access_token"]
            expires_in = float(payload["expires_in"])
        except (KeyError, TypeError, ValueError) as exc:
            payload_hint = sorted(payload) if isinstance(payload, dict) else type(payload).__name__
            raise TokenAcquisitionError(
                f"OIDC token endpoint {self.token_url!r} returned a "
                f"2xx response without an ``access_token`` / ``expires_in`` "
                f"pair; payload keys: {payload_hint}.",
            ) from exc
        return token, expires_in


def vcf_paif_chat_profile() -> OpenAIModelProfile:
    """Return the capability profile for VCF Private AI Foundation.

    PAIF's chat-completion engine is vLLM (per Broadcom techdocs at
    https://techdocs.broadcom.com/.../deploying-model-endpoints.html),
    so the profile is bit-equivalent to :func:`vllm_chat_profile`:

    * ``openai_supports_strict_tool_definition=False`` — vLLM accepts
      the OpenAI ``strict: true`` tool-definition flag but the engine
      does not enforce the schema; sending it would let the model
      emit a non-conforming tool call the loop then rejects on a
      structural mismatch.
    * ``openai_chat_supports_multiple_system_messages=True`` — vLLM
      preserves the ``messages[]`` array verbatim (no chat-template
      collapse of multiple ``role=system`` turns).
    * ``json_schema_transformer=None`` — no per-call schema rewrite
      needed.

    Surfaced as a distinct factory (rather than aliased to
    :func:`vllm_chat_profile`) so a future PAIF release that diverges
    from vanilla vLLM (a Broadcom-side patch, a non-vLLM engine swap)
    only requires editing this function, not every PAIF call site.
    """
    from pydantic_ai.profiles.openai import OpenAIModelProfile

    return OpenAIModelProfile(
        openai_supports_strict_tool_definition=False,
        openai_chat_supports_multiple_system_messages=True,
        json_schema_transformer=None,
    )


#: Capability flags for a VCF Private AI Foundation backend
#: (``pydantic_ai.models.openai.OpenAIChatModel`` over the PAIF
#: OpenAI-compat surface). Tools and streaming are honoured by the
#: underlying vLLM engine; prompt caching is off — neither vLLM nor
#: PAIF exposes the Anthropic-style ``cache_control`` knob, and PAIF
#: does not have an opaque-to-the-client automatic cache the way
#: OpenAI SaaS does. ``tool_format`` is ``"openai"`` — same wire
#: shape as the rest of the OpenAI-compat family.
vcf_paif_capabilities: Final[BackendCapabilities] = BackendCapabilities(
    supports_tools=True,
    supports_streaming=True,
    supports_prompt_cache=False,
    tool_format="openai",
)


def vcf_paif_bearer_provider(
    *,
    token_url: str,
    client_id: str,
    client_secret: str,
    scope: str | None = None,
    http_client: httpx.AsyncClient | None = None,
    refresh_skew_seconds: float = 30.0,
) -> BearerTokenProvider:
    """Build a :data:`BearerTokenProvider` driven by OIDC client-credentials.

    Convenience constructor returning a configured
    :class:`OidcClientCredentialsTokenProvider` instance — most callers
    only need this thin wrapper, not the class directly. A deploy with
    a non-OIDC token source (Vault dynamic credentials, a sidecar that
    refreshes a file, a static long-lived token) constructs its own
    ``Callable[[], Awaitable[str]]`` and hands it to
    :func:`vcf_paif_backend_builder` directly.

    Args:
        token_url: The IdP's token endpoint.
        client_id: The OIDC client id for the backplane → PAIF integration.
        client_secret: The OIDC client secret. Sourced upstream from
            Vault / external-secret / sealed-secret; never logged.
        scope: Optional ``scope`` to include in the token request.
        http_client: Optional shared :class:`httpx.AsyncClient` — pass a
            long-lived client for connection pooling in production.
        refresh_skew_seconds: How many seconds before the IdP-reported
            expiry to treat the cached token as expired (default 30 s).

    Returns:
        A callable awaitable of ``str`` matching :data:`BearerTokenProvider`.
    """
    return OidcClientCredentialsTokenProvider(
        token_url=token_url,
        client_id=client_id,
        client_secret=client_secret,
        scope=scope,
        http_client=http_client,
        refresh_skew_seconds=refresh_skew_seconds,
    )


def vcf_paif_backend_builder(
    *,
    model_id: str,
    base_url: str,
    bearer_token_provider: BearerTokenProvider,
) -> BackendBuilder:
    """Build a zero-arg :class:`BackendBuilder` for one PAIF endpoint.

    Returns a closure the resolver registers; the closure constructs a
    fresh :class:`OpenAIChatModel` whose underlying :class:`AsyncOpenAI`
    client is wired with the **lazy** token provider — every request
    re-resolves the bearer, so the IdP's token rotation is transparent
    (no resolver rebuild on rotation).

    The lazy-callable shape (``api_key=Callable[[], Awaitable[str]]``)
    is what :class:`openai.AsyncOpenAI` accepts natively from openai
    ``>=2.0``; verified by installed-library introspection on
    ``openai==2.38.0`` in this slice. The alternative pattern —
    rebuilding the provider per resolve — would re-instantiate the
    underlying httpx client on every agent run and lose connection
    pooling, so the framework-native lazy-callable wins on both code
    economy and runtime efficiency.

    Args:
        model_id: The pydantic_ai model id (e.g.
            ``openai:meta-llama/Llama-3.1-8B-Instruct``). PAIF accepts
            the bare model name; pydantic_ai's :class:`OpenAIChatModel`
            strips the ``openai:`` prefix when present.
        base_url: The PAIF endpoint base URL **including** the
            :data:`VCF_PAIF_OPENAI_COMPAT_BASE_PATH` sub-path —
            e.g. ``https://pais.airgap.local/api/v1/compatibility/openai/v1/``.
            The builder does **not** auto-append the path: operators
            who point a generic OpenAI-compat backend at a PAIF host
            should switch to this builder rather than relying on the
            generic one to silently fix the path.
        bearer_token_provider: An async callable that returns a current
            bearer token. The bundled :func:`vcf_paif_bearer_provider`
            covers the OIDC ``client_credentials`` case; a custom
            callable covers Vault-dynamic / sidecar / static-token
            shapes.

    Returns:
        A :class:`BackendBuilder` suitable for the ``backends=`` argument
        of :func:`build_resolver`. The builder is **lazy**: the openai
        SDK only imports when the resolver calls the closure, so a
        deploy that registers a PAIF backend but never resolves to it
        never loads the ``openai`` wheel.
    """
    profile = vcf_paif_chat_profile()

    def _build() -> Model:
        # Lazy import — the ``[openai]`` extra is pinned (#1077) but the
        # SDK still costs ~20 MB of resident memory and a non-trivial
        # import-time. Deploys whose policy never resolves to PAIF skip
        # the cost entirely. The AsyncOpenAI client must be constructed
        # here (not at module-import time) because it spins up an
        # event-loop-aware httpx client; constructing at import would
        # bind to whichever loop happened to be current then.
        from openai import AsyncOpenAI
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider

        openai_client = AsyncOpenAI(
            base_url=base_url,
            api_key=bearer_token_provider,
        )
        provider = OpenAIProvider(openai_client=openai_client)
        return OpenAIChatModel(model_id, provider=provider, profile=profile)

    return _build


def default_vcf_paif_backend_builder() -> Model:
    """Build the settings-driven default VCF PAIF Model.

    Reads ``vcf_paif_base_url`` / ``vcf_paif_model`` and the OIDC
    config (``vcf_paif_oidc_token_url`` / ``vcf_paif_oidc_client_id`` /
    ``vcf_paif_oidc_client_secret`` / ``vcf_paif_oidc_scope``) from
    :class:`~meho_backplane.settings.Settings`; constructs the bundled
    :class:`OidcClientCredentialsTokenProvider`; returns the built
    :class:`OpenAIChatModel`.

    Fail-closed: any of ``vcf_paif_base_url`` / ``vcf_paif_oidc_token_url``
    / ``vcf_paif_oidc_client_id`` / ``vcf_paif_oidc_client_secret``
    empty raises :class:`~meho_backplane.agent.run.AgentRunError`, so
    a deploy that registered a PAIF backend but never wired the OIDC
    config surfaces at first agent invocation rather than mid-loop
    with an opaque 401. The error message names the specific missing
    setting so the operator's fix is one ``helm upgrade`` away.

    Operators with **multiple** PAIF endpoints (per-tenant routing)
    construct each backend explicitly via
    :func:`vcf_paif_backend_builder` + :func:`vcf_paif_bearer_provider`
    instead — the settings-driven default is the single-PAIF-endpoint
    convenience path, symmetric to :func:`anthropic_backend_builder`
    and :func:`default_openai_backend_builder`.
    """
    # Imported lazily so this function only pays the cost when the
    # resolver actually resolves to PAIF (the :class:`AgentRunError`
    # import would otherwise tie this module's import time to
    # ``agent.run`` and its dependencies).
    from meho_backplane.agent.run import AgentRunError
    from meho_backplane.settings import get_settings

    settings = get_settings()
    missing: list[str] = []
    if not settings.vcf_paif_base_url:
        missing.append("VCF_PAIF_BASE_URL")
    if not settings.vcf_paif_oidc_token_url:
        missing.append("VCF_PAIF_OIDC_TOKEN_URL")
    if not settings.vcf_paif_oidc_client_id:
        missing.append("VCF_PAIF_OIDC_CLIENT_ID")
    if not settings.vcf_paif_oidc_client_secret:
        missing.append("VCF_PAIF_OIDC_CLIENT_SECRET")
    if missing:
        raise AgentRunError(
            "VCF Private AI Foundation backend is registered but the "
            "following required settings are unset: "
            + ", ".join(missing)
            + ". Wire them via Helm / external-secret / sealed-secret "
            "(see docs/cross-repo/vcf-paif-deployment.md) before routing "
            "a tier to the PAIF backend, or register a per-backend "
            "builder via vcf_paif_backend_builder(...) explicitly.",
        )

    provider = vcf_paif_bearer_provider(
        token_url=settings.vcf_paif_oidc_token_url,
        client_id=settings.vcf_paif_oidc_client_id,
        client_secret=settings.vcf_paif_oidc_client_secret,
        scope=settings.vcf_paif_oidc_scope or None,
    )
    builder = vcf_paif_backend_builder(
        model_id=settings.vcf_paif_model,
        base_url=settings.vcf_paif_base_url,
        bearer_token_provider=provider,
    )
    return builder()
