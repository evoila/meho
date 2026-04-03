# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Pydantic models for MCP server tool inputs and outputs."""

from pydantic import BaseModel, Field


class InvestigateInput(BaseModel):
    """Input for the meho_investigate tool."""

    query: str = Field(description="The diagnostic question to investigate")
    connector_scope: list[str] | None = Field(
        default=None,
        description="Optional list of connector IDs to limit investigation scope",
    )


class InvestigateOutput(BaseModel):
    """Output from the meho_investigate tool."""

    session_id: str
    status: str  # "completed" | "failed"
    summary: str
    findings: list[str]
    connectors_used: list[str]


class SearchKnowledgeInput(BaseModel):
    """Input for the meho_search_knowledge tool."""

    query: str = Field(description="Search query for the knowledge base")
    limit: int = Field(default=10, ge=1, le=50, description="Max results")
    tags: list[str] | None = Field(default=None, description="Optional tag filter")


class SearchKnowledgeOutput(BaseModel):
    """Output from the meho_search_knowledge tool."""

    results: list[dict]
    total: int


class QueryTopologyInput(BaseModel):
    """Input for the meho_query_topology tool."""

    entity_name: str = Field(description="Name of the entity to look up")
    entity_type: str | None = Field(default=None, description="Filter by type")


class QueryTopologyOutput(BaseModel):
    """Output from the meho_query_topology tool."""

    entity: dict | None
    relationships: list[dict]


class ListConnectorsOutput(BaseModel):
    """Output from the meho_list_connectors tool."""

    connectors: list[dict]
