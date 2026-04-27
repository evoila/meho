# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Pydantic schemas for orchestrator skills API.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class OrchestratorSkillCreate(BaseModel):
    """Request body for creating an orchestrator skill."""

    name: str = Field(..., max_length=255)
    description: str | None = None
    content: str
    is_customized: bool = False
    skill_type: str = "orchestrator"
    connector_type: str | None = None


class OrchestratorSkillUpdate(BaseModel):
    """Request body for updating an orchestrator skill (partial)."""

    name: str | None = Field(default=None, max_length=255)
    description: str | None = None
    content: str | None = None
    is_active: bool | None = None


class OrchestratorSkillResponse(BaseModel):
    """Full orchestrator skill response."""

    id: UUID
    tenant_id: str
    name: str
    description: str | None = None
    content: str
    summary: str
    is_active: bool
    is_customized: bool
    skill_type: str
    connector_type: str | None = None
    created_at: datetime
    updated_at: datetime


class OrchestratorSkillSummary(BaseModel):
    """Lightweight list response for orchestrator skills."""

    id: UUID
    name: str
    description: str | None = None
    is_active: bool


class GenerateSkillRequest(BaseModel):
    """Request for LLM-powered skill generation."""

    user_description: str


class GenerateSkillResponse(BaseModel):
    """Response with generated skill content."""

    content: str
