# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
LLM Instruction Generator for OpenAPI Endpoints.

This module auto-generates LLMInstructions when OpenAPI specs are ingested.
It analyzes endpoint schemas and creates appropriate guidance for the LLM
to help users through complex parameter collection.

The generator:
1. Analyzes body schema for complexity indicators
2. Identifies patterns (ID references, nested objects, arrays, enums)
3. Generates appropriate conversation strategy
4. Creates default parameter handling rules
5. Optionally uses GPT-4 for enhanced instruction generation

Usage:
    generator = InstructionGenerator()
    instructions = await generator.generate_for_endpoint(endpoint)
"""

from typing import Any

from pydantic_ai import Agent

from meho_app.core.config import get_config
from meho_app.core.otel import get_logger
from meho_app.modules.connectors.rest.llm_instructions import (
    EndpointComplexityAnalysis,
    LLMInstructions,
    analyze_endpoint_complexity,
    generate_default_instructions,
)

logger = get_logger(__name__)


class InstructionGenerator:
    """
    Generates LLM instructions for API endpoints.

    Can generate instructions using:
    1. Rule-based analysis (fast, deterministic)
    2. LLM-enhanced generation (slower, higher quality)
    """

    def __init__(self, use_llm: bool = False) -> None:
        """
        Initialize the instruction generator.

        Args:
            use_llm: Whether to use LLM for enhanced instruction generation
        """
        self.use_llm = use_llm
        self._llm_agent: Agent[None, LLMInstructions] | None = None

    async def generate_for_endpoint(
        self,
        endpoint_id: str,
        method: str,
        path: str,
        body_schema: dict[str, Any] | None = None,
        description: str | None = None,
        summary: str | None = None,
        operation_id: str | None = None,
    ) -> LLMInstructions:
        """
        Generate LLM instructions for an endpoint.

        Args:
            endpoint_id: Unique identifier for the endpoint
            method: HTTP method (GET, POST, PUT, DELETE, etc.)
            path: Endpoint path (e.g., /api/vms)
            body_schema: Request body JSON schema
            description: Endpoint description from OpenAPI spec
            summary: Endpoint summary from OpenAPI spec
            operation_id: Operation ID from OpenAPI spec

        Returns:
            Generated LLMInstructions
        """
        # Analyze complexity
        analysis = analyze_endpoint_complexity(
            endpoint_id=endpoint_id,
            method=method,
            path=path,
            body_schema=body_schema,
        )

        logger.info(
            f"Analyzed endpoint {method} {path}: "
            f"complexity={analysis.computed_complexity_score}, "
            f"id_refs={len(analysis.id_reference_fields)}, "
            f"nested={len(analysis.nested_objects)}, "
            f"arrays={len(analysis.array_fields)}"
        )

        # For simple endpoints, use rule-based generation
        if analysis.computed_complexity_score <= 5 or not self.use_llm:
            return self._generate_rule_based(analysis, description, summary)

        # For complex endpoints with LLM enabled, enhance with LLM
        try:
            return await self._generate_llm_enhanced(analysis, description, summary, body_schema)
        except Exception as e:
            logger.warning(f"LLM generation failed, falling back to rules: {e}")
            return self._generate_rule_based(analysis, description, summary)

    def _generate_rule_based(
        self,
        analysis: EndpointComplexityAnalysis,
        description: str | None = None,
        summary: str | None = None,
    ) -> LLMInstructions:
        """Generate instructions using rule-based analysis."""
        instructions = generate_default_instructions(analysis)

        # Add endpoint-specific reasoning hints
        hints_parts = []

        if description:
            hints_parts.append(f"Endpoint description: {description[:200]}")

        if analysis.has_id_references:
            id_fields = ", ".join(analysis.id_reference_fields[:5])
            hints_parts.append(
                f"ID reference fields detected: {id_fields}. "
                "For each, offer to list available resources before asking for ID."
            )

        if analysis.has_arrays:
            array_fields = ", ".join(analysis.array_fields[:5])
            hints_parts.append(
                f"Array fields detected: {array_fields}. "
                "Ask how many items, then guide through each one."
            )

        if analysis.nested_objects:
            nested = ", ".join(analysis.nested_objects[:5])
            hints_parts.append(f"Nested objects: {nested}. Break down into sub-conversations.")

        if hints_parts:
            instructions.reasoning_hints = "\n".join(hints_parts)

        return instructions

    async def _generate_llm_enhanced(
        self,
        analysis: EndpointComplexityAnalysis,
        description: str | None,
        summary: str | None,
        body_schema: dict[str, Any] | None,
    ) -> LLMInstructions:
        """Generate enhanced instructions using LLM."""
        if not self._llm_agent:
            self._llm_agent = self._create_llm_agent()

        # Build context for LLM
        context = f"""
Analyze this API endpoint and generate LLM instructions for helping users
call it through a conversational interface.

Endpoint: {analysis.method} {analysis.path}
Summary: {summary or "Not provided"}
Description: {description or "Not provided"}

Complexity Analysis:
- Total parameters: {analysis.total_parameters}
- Required parameters: {analysis.required_parameters}
- Nesting depth: {analysis.nested_depth}
- Has ID references: {analysis.has_id_references} ({", ".join(analysis.id_reference_fields[:5])})
- Has arrays: {analysis.has_arrays} ({", ".join(analysis.array_fields[:5])})
- Has enums: {analysis.has_enums} ({", ".join(analysis.enum_fields[:5])})

Body Schema (truncated):
{str(body_schema)[:2000] if body_schema else "No body schema"}

Generate instructions that help the LLM guide users through parameter collection
in a natural, conversational way. Include:
1. Appropriate conversation strategy
2. Specific parameter handling rules
3. Example conversation showing good UX
4. User guidance (warnings, tips)
"""

        result = await self._llm_agent.run(context)
        return result.output

    def _create_llm_agent(self) -> Agent[None, LLMInstructions]:
        """Create the LLM agent for instruction generation."""
        config = get_config()

        return Agent(
            model=config.data_extractor_model,
            output_type=LLMInstructions,
            instructions="""
You are an API documentation expert. Your job is to generate LLM instructions
that help another AI assistant guide users through calling complex API endpoints.

Generate instructions that:
1. Break down complex operations into conversational steps
2. Identify when to offer "list resources" for ID fields
3. Guide through nested objects one at a time
4. Handle arrays by asking count, then configuring each item
5. Present enums as clear options

The instructions should enable natural conversation, not rigid scripts.
""",
        )


async def generate_instructions_for_spec(
    endpoints: list[dict[str, Any]],
    use_llm: bool = False,
) -> dict[str, LLMInstructions]:
    """
    Generate LLM instructions for all endpoints in a spec.

    This is called during OpenAPI spec ingestion to auto-generate
    guidance for complex endpoints.

    Args:
        endpoints: List of endpoint data from spec parsing
        use_llm: Whether to use LLM for enhanced generation

    Returns:
        Dict mapping endpoint_id to LLMInstructions
    """
    generator = InstructionGenerator(use_llm=use_llm)
    results: dict[str, LLMInstructions] = {}

    for endpoint in endpoints:
        try:
            # Only generate for POST/PUT/PATCH (write operations)
            method = endpoint.get("method", "").upper()
            if method not in ("POST", "PUT", "PATCH"):
                continue

            # Only generate for endpoints with body schemas
            body_schema = endpoint.get("body_schema")
            if not body_schema:
                continue

            instructions = await generator.generate_for_endpoint(
                endpoint_id=endpoint.get("id", ""),
                method=method,
                path=endpoint.get("path", ""),
                body_schema=body_schema,
                description=endpoint.get("description"),
                summary=endpoint.get("summary"),
                operation_id=endpoint.get("operation_id"),
            )

            results[endpoint.get("id", "")] = instructions

        except Exception as e:
            logger.error(f"Failed to generate instructions for {endpoint.get('path')}: {e}")

    return results


def should_generate_instructions(
    method: str,
    body_schema: dict[str, Any] | None,
) -> bool:
    """
    Determine if an endpoint should have LLM instructions generated.

    Instructions are generated for:
    - Write operations (POST, PUT, PATCH)
    - Endpoints with complex body schemas
    """
    # Only write operations
    if method.upper() not in ("POST", "PUT", "PATCH"):
        return False

    # Need body schema
    if not body_schema:
        return False

    # Check complexity
    properties = body_schema.get("properties", {})

    # Simple schemas don't need instructions
    if len(properties) <= 2:
        return False

    # Complex schemas benefit from instructions
    return (
        len(properties) > 5
        or any(p.endswith("_id") for p in properties)
        or any(v.get("type") == "array" for v in properties.values())
        or any(v.get("type") == "object" for v in properties.values())
    )
