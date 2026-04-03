# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Integration tests for LLM instruction generation during OpenAPI spec ingestion.

These tests verify that when an OpenAPI spec with complex write endpoints is
uploaded, LLM instructions are automatically generated and stored.
"""

import json

import pytest

from meho_app.modules.connectors.rest.instruction_generator import (
    InstructionGenerator,
    should_generate_instructions,
)
from meho_app.modules.connectors.rest.llm_instructions import ConversationStrategy, LLMInstructions


class TestInstructionGenerationOnIngestion:
    """Tests for instruction generation during spec upload."""

    def test_should_generate_for_complex_post(self) -> None:
        """Test that complex POST endpoints trigger instruction generation."""
        body_schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "datastore_id": {"type": "string"},
                "network_id": {"type": "string"},
                "config": {"type": "object", "properties": {"replicas": {"type": "integer"}}},
                "disks": {"type": "array", "items": {"type": "object"}},
                "cpu": {"type": "integer"},
            },
        }

        assert should_generate_instructions("POST", body_schema) is True

    def test_should_not_generate_for_simple_post(self) -> None:
        """Test that simple POST endpoints don't trigger generation."""
        body_schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}, "value": {"type": "integer"}},
        }

        assert should_generate_instructions("POST", body_schema) is False

    def test_should_not_generate_for_get(self) -> None:
        """Test that GET endpoints don't trigger generation."""
        body_schema = {"type": "object", "properties": {"x": {"type": "string"}}}

        assert should_generate_instructions("GET", body_schema) is False

    @pytest.mark.asyncio
    async def test_instruction_generator_creates_valid_instructions(self) -> None:
        """Test that generator creates valid LLMInstructions."""
        generator = InstructionGenerator(use_llm=False)

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
            description="Create a new VM",
        )

        # Should be valid LLMInstructions
        assert isinstance(instructions, LLMInstructions)

        # Should have prerequisite-first strategy for ID references
        assert instructions.conversation_strategy == ConversationStrategy.PREREQUISITE_FIRST

        # Should mention ID references in hints
        assert "datastore_id" in instructions.reasoning_hints

    @pytest.mark.asyncio
    async def test_instruction_generator_handles_arrays(self) -> None:
        """Test that generator handles array fields."""
        generator = InstructionGenerator(use_llm=False)

        instructions = await generator.generate_for_endpoint(
            endpoint_id="create-deployment",
            method="POST",
            path="/api/deployments",
            body_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "containers": {"type": "array", "items": {"type": "object"}},
                },
            },
        )

        # Should mention arrays in hints
        assert "containers" in instructions.reasoning_hints
        assert "array" in instructions.reasoning_hints.lower()

    @pytest.mark.asyncio
    async def test_instruction_generator_serializable(self) -> None:
        """Test that generated instructions can be serialized to JSON."""
        generator = InstructionGenerator(use_llm=False)

        instructions = await generator.generate_for_endpoint(
            endpoint_id="test",
            method="POST",
            path="/api/test",
            body_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "ref_id": {"type": "string"},
                    "items": {"type": "array", "items": {"type": "object"}},
                },
            },
        )

        # Should serialize without error
        json_str = json.dumps(instructions.model_dump())
        assert len(json_str) > 100

        # Should deserialize back
        data = json.loads(json_str)
        restored = LLMInstructions(**data)
        assert restored.conversation_strategy == instructions.conversation_strategy


class TestInstructionGenerationRules:
    """Tests for the default parameter handling rules."""

    @pytest.mark.asyncio
    async def test_default_rules_included(self) -> None:
        """Test that default rules are included in generated instructions."""
        generator = InstructionGenerator(use_llm=False)

        instructions = await generator.generate_for_endpoint(
            endpoint_id="test",
            method="POST",
            path="/api/complex",
            body_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "resource_id": {"type": "string"},
                    "config": {"type": "object"},
                    "items": {"type": "array"},
                    "type": {"type": "string", "enum": ["A", "B", "C"]},
                    "value": {"type": "integer"},
                },
            },
        )

        # Should have parameter handling rules
        assert len(instructions.parameter_handling) >= 4

        # Should have rules for different types
        rule_types = [r.type.value for r in instructions.parameter_handling]
        assert "resource_reference" in rule_types
        assert "array" in rule_types
        assert "enum" in rule_types
