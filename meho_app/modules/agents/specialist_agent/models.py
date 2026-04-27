# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Pydantic models for SpecialistAgent structured outputs.

These models define the LLM's decision types. The ReActStep model is used
by the ReAct loop for structured LLM output. Legacy models (SearchIntent,
OperationSelection, NoRelevantOperation) are retained for backward
compatibility during migration.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

# ──────────────────────────────────────────────────────────────────────────────
# Typed tool action models (v2.3 — replaces untyped dict[str, Any])
#
# Each meta-tool gets its own schema. The discriminated union ensures the LLM
# sees full JSON schema with required fields, types, and constraints via oneOf.
# connector_id is excluded — always injected server-side.
# ──────────────────────────────────────────────────────────────────────────────


class SearchOperationsAction(BaseModel):
    """Search for available API operations on the connector."""

    tool: Literal["search_operations"] = "search_operations"
    query: str = Field(min_length=1, description="Search terms for operation names")
    limit: int = Field(default=10, ge=1, le=50, description="Max results to return")


class CallOperationAction(BaseModel):
    """Execute a discovered operation by its ID."""

    tool: Literal["call_operation"] = "call_operation"
    operation_id: str = Field(description="Operation ID from search_operations results")
    parameter_sets: list[dict[str, Any]] = Field(
        default_factory=lambda: [dict[str, Any]()],
        description="List of parameter dicts. Use [{}] for no-param operations.",
    )


class ReduceDataAction(BaseModel):
    """Query cached operation results with SQL."""

    tool: Literal["reduce_data"] = "reduce_data"
    sql: str = Field(min_length=1, description="SQL query on cached table data")


class LookupTopologyAction(BaseModel):
    """Look up an entity in the topology graph."""

    tool: Literal["lookup_topology"] = "lookup_topology"
    query: str = Field(min_length=1, description="Entity name or identifier to look up")
    traverse_depth: int = Field(default=10, ge=1, le=20, description="How many hops to traverse")
    cross_connectors: bool = Field(
        default=True, description="Follow SAME_AS edges across connectors"
    )


class SearchKnowledgeAction(BaseModel):
    """Search the knowledge base for documentation."""

    tool: Literal["search_knowledge"] = "search_knowledge"
    query: str = Field(min_length=1, description="Search terms for knowledge documents")
    limit: int = Field(default=5, ge=1, le=20, description="Max results")


class StoreMemoryAction(BaseModel):
    """Store a memory for this connector."""

    tool: Literal["store_memory"] = "store_memory"
    title: str = Field(min_length=1, max_length=500, description="Memory title")
    body: str = Field(min_length=1, description="Memory content")
    memory_type: str = Field(
        default="config", description="Type: entity, pattern, outcome, or config"
    )
    tags: list[str] = Field(default_factory=list, description="Searchable tags")


class ForgetMemoryAction(BaseModel):
    """Search or delete memories for this connector."""

    tool: Literal["forget_memory"] = "forget_memory"
    action: Literal["search", "delete"] = Field(description="search to find, delete to remove")
    query: str | None = Field(default=None, description="Search query (for action=search)")
    memory_id: str | None = Field(default=None, description="Memory ID (for action=delete)")


# ──────────────────────────────────────────────────────────────────────────────
# Network diagnostic tool actions (Phase 96.1 — NOT connector-scoped)
# ──────────────────────────────────────────────────────────────────────────────


class DnsResolveAction(BaseModel):
    """Resolve DNS records for a hostname."""

    tool: Literal["dns_resolve"] = "dns_resolve"
    hostname: str = Field(min_length=1, description="Hostname to resolve (e.g., 'api.example.com')")
    record_types: list[str] = Field(
        default=["A", "AAAA"],
        description="DNS record types to query (A, AAAA, CNAME, MX, SRV, TXT, NS, SOA)",
    )


class TcpProbeAction(BaseModel):
    """Test TCP connectivity to a host:port."""

    tool: Literal["tcp_probe"] = "tcp_probe"
    host: str = Field(min_length=1, description="Hostname or IP address to probe")
    port: int = Field(ge=1, le=65535, description="TCP port number")
    timeout_seconds: float = Field(
        default=5.0, ge=0.5, le=30.0, description="Connection timeout in seconds"
    )


class HttpProbeAction(BaseModel):
    """Send an HTTP request and inspect the response."""

    tool: Literal["http_probe"] = "http_probe"
    url: str = Field(min_length=1, description="URL to probe (must include http:// or https://)")
    method: str = Field(default="GET", description="HTTP method (GET or HEAD)")
    timeout_seconds: float = Field(
        default=10.0, ge=1.0, le=60.0, description="Request timeout in seconds"
    )
    follow_redirects: bool = Field(default=True, description="Follow HTTP redirects")


class TlsCheckAction(BaseModel):
    """Check TLS certificate details and validity."""

    tool: Literal["tls_check"] = "tls_check"
    hostname: str = Field(min_length=1, description="Hostname to check TLS certificate for")
    port: int = Field(default=443, ge=1, le=65535, description="TLS port (usually 443)")
    timeout_seconds: float = Field(
        default=10.0, ge=1.0, le=30.0, description="Connection timeout in seconds"
    )


ToolAction = Annotated[
    SearchOperationsAction
    | CallOperationAction
    | ReduceDataAction
    | LookupTopologyAction
    | SearchKnowledgeAction
    | StoreMemoryAction
    | ForgetMemoryAction
    | DnsResolveAction
    | TcpProbeAction
    | HttpProbeAction
    | TlsCheckAction,
    Field(discriminator="tool"),
]


# ──────────────────────────────────────────────────────────────────────────────
# ReAct loop model (Phase 3, v2.3 typed actions)
# ──────────────────────────────────────────────────────────────────────────────


class ReActStep(BaseModel):
    """Structured LLM response in the ReAct loop.

    The LLM returns EITHER an action (tool call) OR a final answer, never both.
    action_input is a typed discriminated union — the LLM sees full JSON schema
    with required fields for each tool via oneOf.
    """

    thought: str = Field(description="Your step-by-step reasoning about what to do next")
    response_type: Literal["action", "final_answer"] = Field(
        description="Whether you are taking a tool action or providing a final answer"
    )
    action_input: ToolAction | None = Field(
        default=None,
        description="Tool call with typed parameters. The 'tool' field selects the tool.",
    )
    final_answer: str | None = Field(
        default=None,
        description="Your complete response to the operator when you have enough information",
    )
    extend_budget: bool = Field(
        default=False,
        description=(
            "Set to true when your investigation genuinely needs more steps. "
            "Grants +4 additional steps (once only, up to max 12). "
            "Only set alongside an action, not with final_answer."
        ),
    )

    @property
    def action(self) -> str | None:
        """Tool name derived from typed action_input. Backward compatible."""
        if self.action_input is not None:
            return self.action_input.tool
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Legacy models (deprecated -- kept for backward compatibility)
# ──────────────────────────────────────────────────────────────────────────────


class SearchIntent(BaseModel):
    """LLM's decision on what operation to search for.

    .. deprecated::
        Used by the deterministic workflow nodes. Retained for backward
        compatibility during migration to ReAct loop.

    Includes cached data detection for multi-turn context awareness.
    When use_cached_data=True, the workflow should skip operation selection
    and query the cached table directly via reduce_data.
    """

    query: str = Field(
        description="Search query to find relevant operations (e.g., 'list namespaces', 'get pods')"
    )
    reasoning: str = Field(
        description="Brief explanation of why this search is relevant to the user's question"
    )
    use_cached_data: bool = Field(
        default=False,
        description=(
            "True if the user's query should use cached table data instead of "
            "calling a new operation. Set this when the user references previous "
            "results (e.g., 'show more', 'the other X', 'remaining items')."
        ),
    )
    cached_table_name: str | None = Field(
        default=None,
        description=(
            "Name of the cached table to query if use_cached_data=True. "
            "Must match one of the cached table names provided in context."
        ),
    )


class OperationSelection(BaseModel):
    """LLM's selection of which operation to execute.

    .. deprecated::
        Used by the deterministic workflow nodes. Retained for backward
        compatibility during migration to ReAct loop.
    """

    operation_id: str = Field(description="The operation_id to call (from search results)")
    parameters: dict[str, Any] = Field(
        default_factory=dict, description="Parameters to pass to the operation"
    )
    reasoning: str = Field(description="Why this operation answers the user's question")


class NoRelevantOperation(BaseModel):
    """LLM indicates no relevant operation was found.

    .. deprecated::
        Used by the deterministic workflow nodes. Retained for backward
        compatibility during migration to ReAct loop.
    """

    reasoning: str = Field(description="Explanation of why no operation is relevant")
