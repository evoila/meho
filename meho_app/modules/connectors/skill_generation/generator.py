# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Skill generation pipeline service.

Orchestrates the full skill generation flow:
1. Read operations from DB (REST endpoints or typed connector operations)
2. Compute quality score from metadata completeness
3. Sanitize descriptions against prompt injection
4. Call Claude Sonnet to synthesize an SRE-style operational playbook
5. Store the generated skill on the ConnectorModel

The generated skill teaches the SpecialistAgent how to investigate and
diagnose systems using the connector's exact operation_ids.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from meho_app.core.otel.logging import get_logger
from meho_app.modules.connectors.models import ConnectorModel
from meho_app.modules.connectors.skill_generation.quality_scorer import (
    OperationData,
    compute_quality_score,
)
from meho_app.modules.connectors.skill_generation.sanitizer import (
    sanitize_descriptions,
)

logger = get_logger(__name__)


class SkillGenerationResult(BaseModel):
    """Result of skill generation pipeline."""

    skill_content: str
    quality_score: int
    operation_count: int


class SkillGenerator:
    """Generates contextual markdown skills from connector operations.

    Reads operations from the database, scores metadata quality, sanitizes
    descriptions, and calls Claude Sonnet to produce a diagnostic playbook
    that the SpecialistAgent uses as its system prompt skill content.
    """

    async def generate_skill(
        self,
        session: AsyncSession,
        connector_id: str,
        connector_type: str,
        connector_name: str,
    ) -> SkillGenerationResult:
        """Full pipeline: read ops -> score -> sanitize -> generate -> store.

        Args:
            session: SQLAlchemy async session for DB access.
            connector_id: UUID string of the connector.
            connector_type: Connector type string (e.g., "rest", "vmware").
            connector_name: Human-readable connector name.

        Returns:
            SkillGenerationResult with generated content, quality score,
            and operation count.
        """
        from meho_app.core.config import get_config

        config = get_config()

        # 1. Read operations from DB
        operations = await self._read_operations(session, connector_id, connector_type)
        logger.info(
            f"Read {len(operations)} operations for skill generation",
            connector_id=connector_id,
            connector_type=connector_type,
        )

        # 2. Compute quality score BEFORE generation
        quality_score = compute_quality_score(operations)
        logger.info(
            f"Quality score: {quality_score}/5 stars",
            connector_id=connector_id,
        )

        # 3. Sanitize descriptions (SKILL-04)
        sanitized_ops = sanitize_descriptions(operations)

        # 4. Build generation prompt with operations data
        generation_prompt = self._build_generation_prompt(
            sanitized_ops, connector_name, connector_type
        )

        # 5. Load system prompt and call LLM with retry for transient API failures
        import asyncio

        from pydantic_ai import Agent, InstrumentationSettings

        system_prompt = self._load_system_prompt()
        skill_agent: Agent[None, str] = Agent(
            config.skill_generation_model,
            instrument=InstrumentationSettings(),
        )

        max_attempts = 3
        per_attempt_timeout = 120.0
        skill_content: str | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                logger.info(
                    f"LLM call attempt {attempt}/{max_attempts}",
                    connector_id=connector_id,
                )
                async with asyncio.timeout(per_attempt_timeout):
                    async with skill_agent.run_stream(
                        generation_prompt,
                        instructions=system_prompt,
                    ) as stream:
                        skill_content = await stream.get_output()
                break
            except TimeoutError:
                if attempt < max_attempts:
                    backoff = 2 ** (attempt - 1) * 5
                    logger.warning(
                        f"LLM call timed out after {per_attempt_timeout}s "
                        f"(attempt {attempt}/{max_attempts}), retrying in {backoff}s",
                        connector_id=connector_id,
                    )
                    await asyncio.sleep(backoff)
                else:
                    raise TimeoutError(
                        f"Skill generation timed out after {max_attempts} attempts "
                        f"({per_attempt_timeout}s each)"
                    ) from None

        assert skill_content is not None

        # 6. Store on connector (only generated_skill, never custom_skill)
        await self._store_skill(session, connector_id, skill_content, quality_score)

        return SkillGenerationResult(
            skill_content=skill_content,
            quality_score=quality_score,
            operation_count=len(operations),
        )

    async def _read_operations(
        self,
        session: AsyncSession,
        connector_id: str,
        connector_type: str,
    ) -> list[OperationData]:
        """Read operations from the appropriate DB source.

        REST connectors: reads from EndpointDescriptorModel via repository.
        Typed connectors: reads from ConnectorOperationModel via repository.

        Returns:
            List of normalized OperationData for skill generation.
        """
        if connector_type == "rest":
            return await self._read_rest_operations(session, connector_id)
        else:
            return await self._read_typed_operations(session, connector_id)

    async def _read_rest_operations(
        self,
        session: AsyncSession,
        connector_id: str,
    ) -> list[OperationData]:
        """Read REST endpoint operations from EndpointDescriptorRepository."""
        from meho_app.modules.connectors.rest.repository import (
            EndpointDescriptorRepository,
        )
        from meho_app.modules.connectors.rest.schemas import EndpointFilter

        repo = EndpointDescriptorRepository(session)
        endpoints = await repo.list_endpoints(
            EndpointFilter(
                connector_id=connector_id,
                is_enabled=True,
                limit=500,
            )
        )

        return [
            OperationData(
                operation_id=(
                    ep.operation_id or f"{ep.method}_{ep.path.replace('/', '_').strip('_')}"
                ),
                name=ep.summary or ep.operation_id or f"{ep.method} {ep.path}",
                description=ep.description,
                category=ep.tags[0] if ep.tags else None,
                parameters=_extract_params_from_endpoint(ep),
                response_schema=(ep.response_schema if ep.response_schema else None),
                tags=ep.tags if ep.tags else None,
                summary=ep.summary,
            )
            for ep in endpoints
        ]

    async def _read_typed_operations(
        self,
        session: AsyncSession,
        connector_id: str,
    ) -> list[OperationData]:
        """Read typed connector operations from ConnectorOperationRepository."""
        from meho_app.modules.connectors.repositories.operation_repository import (
            ConnectorOperationRepository,
        )

        repo = ConnectorOperationRepository(session)
        ops = await repo.list_operations(connector_id, is_enabled=True, limit=500)

        return [
            OperationData(
                operation_id=op.operation_id,
                name=op.name,
                description=op.description,
                category=op.category,
                parameters=op.parameters,
                response_schema=None,
                tags=[op.category] if op.category else None,
                summary=op.description,
            )
            for op in ops
        ]

    def _build_generation_prompt(  # NOSONAR (cognitive complexity)
        self,
        operations: list[OperationData],
        connector_name: str,
        connector_type: str,
    ) -> str:
        """Format operations data as a structured message for the LLM.

        Includes connector context, quality metadata, and a formatted list
        of all operations with their descriptions and parameters.

        Args:
            operations: Sanitized operation data.
            connector_name: Human-readable connector name.
            connector_type: Connector type string.

        Returns:
            Formatted prompt string for the generation LLM call.
        """
        # Count operations with/without descriptions
        with_desc = sum(1 for op in operations if op.description and len(op.description) > 10)
        without_desc = len(operations) - with_desc

        # Build connector context section
        lines = [
            f"# Connector: {connector_name}",
            f"**Type:** {connector_type}",
            f"**Total operations:** {len(operations)}",
            f"**Operations with descriptions:** {with_desc}",
            f"**Operations without descriptions:** {without_desc}",
            "",
            "## Operations",
            "",
        ]

        # Format each operation
        for op in operations:
            category_label = op.category or "uncategorized"
            desc = op.description or op.name
            lines.append(f"- **{op.operation_id}** ({category_label}): {desc}")

            # Add parameter summary if available
            if op.parameters:
                param_names = []
                for p in op.parameters:
                    if isinstance(p, dict):
                        name = p.get("name", "?")
                        p_type = p.get("type", "")
                        p_in = p.get("in", "")
                        suffix = f" ({p_type})" if p_type else ""
                        prefix = f"[{p_in}] " if p_in else ""
                        param_names.append(f"{prefix}{name}{suffix}")
                if param_names:
                    lines.append(f"  Parameters: {', '.join(param_names)}")

            # Note response schema presence
            if op.response_schema:
                schema_props = op.response_schema.get("properties", {})
                if schema_props:
                    field_names = list(schema_props.keys())[:10]
                    lines.append(f"  Response fields: {', '.join(field_names)}")

        return "\n".join(lines)

    def _load_system_prompt(self) -> str:
        """Load the generation system prompt from the prompts directory.

        Reads prompts/generate_skill.md relative to this file's location.

        Returns:
            System prompt string for the generation LLM call.
        """
        prompt_path = Path(__file__).parent / "prompts" / "generate_skill.md"
        return prompt_path.read_text(encoding="utf-8")

    async def _store_skill(
        self,
        session: AsyncSession,
        connector_id: str,
        skill_content: str,
        quality_score: int,
    ) -> None:
        """Store the generated skill on the connector.

        Updates ONLY generated_skill and skill_quality_score. The custom_skill
        field is NEVER touched by the pipeline -- it is operator-only.

        Args:
            session: SQLAlchemy async session.
            connector_id: UUID string of the connector.
            skill_content: Generated skill markdown content.
            quality_score: Quality score 1-5.
        """
        import uuid as _uuid

        stmt = (
            update(ConnectorModel)
            .where(ConnectorModel.id == _uuid.UUID(connector_id))
            .values(
                generated_skill=skill_content,
                skill_quality_score=quality_score,
            )
        )
        await session.execute(stmt)
        logger.info(
            "Stored generated skill on connector",
            connector_id=connector_id,
            quality_score=quality_score,
            skill_length=len(skill_content),
        )


def _extract_params_from_endpoint(ep: Any) -> list[dict]:
    """Extract parameter names and types from endpoint schemas.

    Combines path_params_schema, query_params_schema, and body_schema
    into a flat list of parameter dicts with name, type, description, and
    location (in: path|query|body).

    Args:
        ep: EndpointDescriptor instance with schema attributes.

    Returns:
        List of parameter dicts.
    """
    params: list[dict] = []

    # Path parameters
    if hasattr(ep, "path_params_schema") and ep.path_params_schema:
        props = ep.path_params_schema.get("properties", {})
        for name, schema in props.items():
            params.append(
                {
                    "name": name,
                    "type": schema.get("type", "string"),
                    "description": schema.get("description", ""),
                    "in": "path",
                }
            )

    # Query parameters
    if hasattr(ep, "query_params_schema") and ep.query_params_schema:
        props = ep.query_params_schema.get("properties", {})
        for name, schema in props.items():
            params.append(
                {
                    "name": name,
                    "type": schema.get("type", "string"),
                    "description": schema.get("description", ""),
                    "in": "query",
                }
            )

    # Body parameters (top-level properties only)
    if hasattr(ep, "body_schema") and ep.body_schema:
        props = ep.body_schema.get("properties", {})
        for name, schema in props.items():
            params.append(
                {
                    "name": name,
                    "type": schema.get("type", "string"),
                    "description": schema.get("description", ""),
                    "in": "body",
                }
            )

    return params
