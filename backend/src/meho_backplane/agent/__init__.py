# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``meho_backplane.agent`` — the in-process agent runtime (G11.1).

This package hosts the ``AgentRun`` seam: a thin, swappable wrapper around
the third-party agent loop (Pydantic AI) that runs one bounded tool-use
loop inside MEHO's own process, with every tool call routed through the
existing ``call_operation`` dispatch path.

The framework is deliberately confined behind :mod:`meho_backplane.agent.run`.
Nothing outside this package imports ``pydantic_ai``; callers depend on the
:class:`~meho_backplane.agent.run.AgentRun` Protocol and the
:class:`~meho_backplane.agent.run.AgentDefinition` /
:class:`~meho_backplane.agent.run.AgentRunHandle` value objects only. That
keeps the loop library replaceable (G11 Goal #800 architecture decision) and
keeps the run-handle store, audit wiring, and invocation surface ours.

The wider G11.1 initiative (#802) builds on this seam: definition persistence
(T2 #809), full toolset resolution (T3 #810), the public sync/async surface
(T4 #811), composition (T5 #812), and run records (T6 #813) all import the
types this package exports.
"""

from meho_backplane.agent.approval_wait import (
    AWAITING_APPROVAL_TIMEOUT_ERROR_CODE,
    ApprovalDecision,
    resume_or_surface_awaiting_approval,
    wait_for_approval_decision,
)
from meho_backplane.agent.invoke import (
    AGENT_INVOKE_DEPTH_TOP_LEVEL,
    AgentInvocationDepthExceeded,
    ChildAgentResolver,
    ChildRunFinalizer,
    ChildRunner,
    ChildRunRecorder,
    agent_invoke_depth_var,
    current_agent_run_id_var,
    make_invoke_agent_tool,
)
from meho_backplane.agent.models import (
    DEFAULT_TENANT_KEY,
    VCF_PAIF_OPENAI_COMPAT_BASE_PATH,
    AgentTier,
    BackendBuilder,
    BackendCapabilities,
    BackendNotConfiguredError,
    BearerTokenProvider,
    CapabilityMismatchError,
    EgressViolationError,
    ModelResolver,
    OidcClientCredentialsTokenProvider,
    OpenAICompatVendor,
    ResolverError,
    TenantModelPolicy,
    TierMapping,
    TokenAcquisitionError,
    anthropic_backend_builder,
    anthropic_capabilities,
    bedrock_backend_builder,
    bedrock_capabilities,
    build_resolver,
    default_anthropic_backends,
    default_anthropic_policy,
    default_bedrock_backends,
    default_openai_backend_builder,
    default_vcf_paif_backend_builder,
    ollama_chat_profile,
    openai_chat_profile,
    openai_compat_backend_builder,
    openai_compat_capabilities,
    vcf_paif_backend_builder,
    vcf_paif_bearer_provider,
    vcf_paif_capabilities,
    vcf_paif_chat_profile,
    vllm_chat_profile,
)
from meho_backplane.agent.run import (
    UNEXECUTABLE_RUNBOOK_CLASS,
    AgentDefinition,
    AgentRun,
    AgentRunError,
    AgentRunEvent,
    AgentRunEventKind,
    AgentRunHandle,
    AgentRunResult,
    AgentRunStatus,
    BudgetExceededError,
    ModelFactory,
    PydanticAgentRun,
    UnexecutableRunbookReferenceError,
    default_model_factory,
    find_runbook_instruction,
)
from meho_backplane.agent.toolset import (
    META_TOOL_NAMES,
    RUNBOOK_EXECUTION_META_TOOL_NAMES,
    MetaToolSpec,
    resolve_agent_tools,
    toolset_admits_runbook_execution,
)

__all__ = [
    "AGENT_INVOKE_DEPTH_TOP_LEVEL",
    "AWAITING_APPROVAL_TIMEOUT_ERROR_CODE",
    "DEFAULT_TENANT_KEY",
    "META_TOOL_NAMES",
    "RUNBOOK_EXECUTION_META_TOOL_NAMES",
    "UNEXECUTABLE_RUNBOOK_CLASS",
    "VCF_PAIF_OPENAI_COMPAT_BASE_PATH",
    "AgentDefinition",
    "AgentInvocationDepthExceeded",
    "AgentRun",
    "AgentRunError",
    "AgentRunEvent",
    "AgentRunEventKind",
    "AgentRunHandle",
    "AgentRunResult",
    "AgentRunStatus",
    "AgentTier",
    "ApprovalDecision",
    "BackendBuilder",
    "BackendCapabilities",
    "BackendNotConfiguredError",
    "BearerTokenProvider",
    "BudgetExceededError",
    "CapabilityMismatchError",
    "ChildAgentResolver",
    "ChildRunFinalizer",
    "ChildRunRecorder",
    "ChildRunner",
    "EgressViolationError",
    "MetaToolSpec",
    "ModelFactory",
    "ModelResolver",
    "OidcClientCredentialsTokenProvider",
    "OpenAICompatVendor",
    "PydanticAgentRun",
    "ResolverError",
    "TenantModelPolicy",
    "TierMapping",
    "TokenAcquisitionError",
    "UnexecutableRunbookReferenceError",
    "agent_invoke_depth_var",
    "anthropic_backend_builder",
    "anthropic_capabilities",
    "bedrock_backend_builder",
    "bedrock_capabilities",
    "build_resolver",
    "current_agent_run_id_var",
    "default_anthropic_backends",
    "default_anthropic_policy",
    "default_bedrock_backends",
    "default_model_factory",
    "default_openai_backend_builder",
    "default_vcf_paif_backend_builder",
    "find_runbook_instruction",
    "make_invoke_agent_tool",
    "ollama_chat_profile",
    "openai_chat_profile",
    "openai_compat_backend_builder",
    "openai_compat_capabilities",
    "resolve_agent_tools",
    "resume_or_surface_awaiting_approval",
    "toolset_admits_runbook_execution",
    "vcf_paif_backend_builder",
    "vcf_paif_bearer_provider",
    "vcf_paif_capabilities",
    "vcf_paif_chat_profile",
    "vllm_chat_profile",
    "wait_for_approval_decision",
]
