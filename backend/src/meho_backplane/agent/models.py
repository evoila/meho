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

What ships here vs. C4-b/c/d
============================

This task ships the **resolver shape + capability flags + the Anthropic
backend builder** (so the existing Anthropic path keeps working through
the resolver). Concrete builders for AWS Bedrock, OpenAI-compatible
(vLLM / Ollama), and VCF Private AI Foundation are filed under #1076,
#1077, and #1078 respectively; they slot in as additional
:class:`BackendBuilder` registrations.

The Anthropic builder is **deliberately the only built-in** here: the
``pydantic-ai-slim[bedrock]`` / ``[openai]`` extras are not installed
yet (they land with their respective tasks), and an eager import would
break the agent module on a deployment that doesn't ship them.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final, Protocol
from uuid import UUID

import structlog

if TYPE_CHECKING:
    from pydantic_ai.models import Model

    from meho_backplane.auth.operator import Operator

__all__ = [
    "DEFAULT_TENANT_KEY",
    "AgentTier",
    "BackendBuilder",
    "BackendCapabilities",
    "BackendNotConfiguredError",
    "CapabilityMismatchError",
    "EgressViolationError",
    "ModelResolver",
    "ResolverError",
    "TenantModelPolicy",
    "TierMapping",
    "anthropic_backend_builder",
    "anthropic_capabilities",
    "build_resolver",
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
    """Return the built-in backends registry — just Anthropic in T1.

    Bedrock / OpenAI-compat / VCF PAIF builders land with their
    respective tasks (#1076, #1077, #1078); registering them eagerly
    here would fail-import on a deploy without those extras. The
    registry is a plain dict, so a downstream task adds entries by
    ``backends = {**default_anthropic_backends(), "bedrock-anthropic":
    (build_bedrock, bedrock_caps, True), ...}`` at the call-site.

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
