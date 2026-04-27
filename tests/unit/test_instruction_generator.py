# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for LLM Instruction Generator.
"""

import pytest

from meho_app.modules.connectors.rest.instruction_generator import (
    InstructionGenerator,
    generate_instructions_for_spec,
    should_generate_instructions,
)
from meho_app.modules.connectors.rest.llm_instructions import (
    ConversationStrategy,
    LLMInstructions,
)


class TestInstructionGenerator:
    """Tests for InstructionGenerator class."""

    @pytest.fixture
    def generator(self) -> InstructionGenerator:
        """Create a generator instance."""
        return InstructionGenerator(use_llm=False)

    @pytest.mark.asyncio
    async def test_simple_endpoint(self, generator: InstructionGenerator) -> None:
        """Test generating instructions for a simple endpoint."""
        instructions = await generator.generate_for_endpoint(
            endpoint_id="test-1",
            method="POST",
            path="/api/items",
            body_schema={"type": "object", "properties": {"name": {"type": "string"}}},
        )

        assert isinstance(instructions, LLMInstructions)
        assert instructions.complexity_score <= 5

    @pytest.mark.asyncio
    async def test_complex_with_id_refs(self, generator: InstructionGenerator) -> None:
        """Test complex endpoint with ID reference fields."""
        instructions = await generator.generate_for_endpoint(
            endpoint_id="create-vm",
            method="POST",
            path="/api/vms",
            body_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "datastore_id": {"type": "string"},
                    "network_id": {"type": "string"},
                    "cluster_id": {"type": "string"},
                },
                "required": ["name", "datastore_id"],
            },
            description="Create a new virtual machine",
        )

        assert instructions.complexity_score >= 3
        assert instructions.conversation_strategy == ConversationStrategy.PREREQUISITE_FIRST
        assert "datastore_id" in instructions.reasoning_hints

    @pytest.mark.asyncio
    async def test_with_arrays(self, generator: InstructionGenerator) -> None:
        """Test endpoint with array fields."""
        instructions = await generator.generate_for_endpoint(
            endpoint_id="test",
            method="POST",
            path="/api/resources",
            body_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "items": {"type": "array", "items": {"type": "object"}},
                },
            },
        )

        assert "items" in instructions.reasoning_hints
        assert "array" in instructions.reasoning_hints.lower()

    @pytest.mark.asyncio
    async def test_with_nested(self, generator: InstructionGenerator) -> None:
        """Test endpoint with nested objects."""
        instructions = await generator.generate_for_endpoint(
            endpoint_id="test",
            method="POST",
            path="/api/deployments",
            body_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "config": {"type": "object", "properties": {"replicas": {"type": "integer"}}},
                },
            },
        )

        assert "config" in instructions.reasoning_hints
        assert "nested" in instructions.reasoning_hints.lower()

    @pytest.mark.asyncio
    async def test_no_body_schema(self, generator: InstructionGenerator) -> None:
        """Test endpoint without body schema."""
        instructions = await generator.generate_for_endpoint(
            endpoint_id="test", method="GET", path="/api/items", body_schema=None
        )

        assert instructions.complexity_score == 1
        assert instructions.conversation_strategy == ConversationStrategy.ALL_AT_ONCE


class TestGenerateInstructionsForSpec:
    """Tests for batch instruction generation."""

    @pytest.mark.asyncio
    async def test_write_operations(self) -> None:
        """Test instructions are generated for POST/PUT/PATCH."""
        endpoints = [
            {
                "id": "create",
                "method": "POST",
                "path": "/api/items",
                "body_schema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "type_id": {"type": "string"},
                        "config": {"type": "object"},
                    },
                },
            },
        ]

        results = await generate_instructions_for_spec(endpoints)
        assert "create" in results

    @pytest.mark.asyncio
    async def test_skips_get(self) -> None:
        """Test that GET is skipped."""
        endpoints = [{"id": "list", "method": "GET", "path": "/api/items"}]
        results = await generate_instructions_for_spec(endpoints)
        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_skips_no_body(self) -> None:
        """Test endpoints without body are skipped."""
        endpoints = [{"id": "post", "method": "POST", "path": "/api/trigger", "body_schema": None}]
        results = await generate_instructions_for_spec(endpoints)
        assert len(results) == 0


class TestShouldGenerateInstructions:
    """Tests for instruction generation predicate."""

    def test_complex_post(self) -> None:
        """Test POST with complex body returns True."""
        result = should_generate_instructions(
            method="POST",
            body_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "datastore_id": {"type": "string"},
                    "config": {"type": "object"},
                    "items": {"type": "array"},
                    "value": {"type": "integer"},
                    "active": {"type": "boolean"},
                },
            },
        )
        assert result is True

    def test_get_false(self) -> None:
        """Test GET returns False."""
        result = should_generate_instructions(method="GET", body_schema={})
        assert result is False

    def test_no_body_false(self) -> None:
        """Test POST without body returns False."""
        result = should_generate_instructions(method="POST", body_schema=None)
        assert result is False

    def test_simple_body_false(self) -> None:
        """Test POST with simple body returns False."""
        result = should_generate_instructions(
            method="POST",
            body_schema={
                "type": "object",
                "properties": {"name": {"type": "string"}, "value": {"type": "integer"}},
            },
        )
        assert result is False

    def test_with_id_refs(self) -> None:
        """Test body with ID fields returns True."""
        result = should_generate_instructions(
            method="POST",
            body_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "datastore_id": {"type": "string"},
                    "network_id": {"type": "string"},
                },
            },
        )
        assert result is True
