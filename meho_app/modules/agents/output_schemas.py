# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Output schemas for LLM agents.

Pydantic models used as output types for PydanticAI agents.
These models define the structured output format that agents must produce.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field


class ConnectorDetermination(BaseModel):
    """
    Output model for connector determination.

    Used by the classifier agent to identify which connector/system
    a user query is referring to.
    """

    connector_id: str = Field(description="UUID of the connector or 'unknown'")
    connector_name: str | None = Field(description="Name of the connector or null", default=None)
    confidence: Literal["high", "medium", "low"] = Field(
        description="Confidence level of the match"
    )
    reason: str = Field(description="Brief explanation of why this connector was selected")


class DataSummary(BaseModel):
    """
    Output model for large data summarization.

    Used by the data extractor agent to summarize large API responses
    into a concise format that fits within LLM context limits.
    """

    summary: str = Field(description="Text description of the data")
    critical_items: list[dict[str, Any]] | None = Field(
        description="Array of items with issues (max 50)", default=None
    )
    statistics: dict[str, Any] | None = Field(description="Counts and percentages", default=None)
    note: str | None = Field(description="Additional notes about the summarization", default=None)
