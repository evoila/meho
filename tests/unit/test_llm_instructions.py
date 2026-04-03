# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for LLM Instructions schema and utilities.
"""

import pytest

from meho_app.modules.connectors.rest.llm_instructions import (
    DEFAULT_PARAMETER_RULES,
    ConversationStrategy,
    ConversationTurn,
    EndpointComplexityAnalysis,
    LLMInstructions,
    ParameterHandlingRule,
    ParameterType,
    PrerequisiteFlow,
    UserGuidance,
    analyze_endpoint_complexity,
    generate_default_instructions,
)

# =============================================================================
# Schema Tests
# =============================================================================


class TestLLMInstructionsSchema:
    """Tests for LLMInstructions Pydantic model."""

    def test_default_values(self) -> None:
        """Test default values are set correctly."""
        instructions = LLMInstructions()

        assert instructions.conversation_strategy == ConversationStrategy.STEP_BY_STEP
        assert instructions.complexity_score == 5
        assert instructions.parameter_handling == []
        assert instructions.prerequisite_flows == []
        assert instructions.example_conversation == []

    def test_complexity_score_bounds(self) -> None:
        """Test complexity score is bounded 1-10."""
        # Valid scores
        instructions = LLMInstructions(complexity_score=1)
        assert instructions.complexity_score == 1

        instructions = LLMInstructions(complexity_score=10)
        assert instructions.complexity_score == 10

        # Invalid scores should raise
        with pytest.raises(ValueError):  # noqa: PT011 -- test validates exception type is sufficient
            LLMInstructions(complexity_score=0)

        with pytest.raises(ValueError):  # noqa: PT011 -- test validates exception type is sufficient
            LLMInstructions(complexity_score=11)

    def test_full_instructions(self) -> None:
        """Test creating full instructions object."""
        instructions = LLMInstructions(
            conversation_strategy=ConversationStrategy.PREREQUISITE_FIRST,
            complexity_score=9,
            parameter_handling=[
                ParameterHandlingRule(
                    pattern=".*_id$",
                    type=ParameterType.RESOURCE_REFERENCE,
                    instructions="List resources first",
                )
            ],
            prerequisite_flows=[
                PrerequisiteFlow(
                    name="list_datastores",
                    description="List available datastores",
                    applies_to=["datastore_id"],
                )
            ],
            example_conversation=[
                ConversationTurn(
                    agent="What name for the VM?",
                    user="web-01",
                    context="Start with simple required field",
                )
            ],
            user_guidance=UserGuidance(
                complexity_warning="This is complex", time_estimate="10 minutes"
            ),
            reasoning_hints="This endpoint creates VMs",
        )

        assert instructions.complexity_score == 9
        assert len(instructions.parameter_handling) == 1
        assert len(instructions.prerequisite_flows) == 1
        assert len(instructions.example_conversation) == 1
        assert instructions.user_guidance is not None

    def test_serialization(self) -> None:
        """Test instructions can be serialized to dict."""
        instructions = LLMInstructions(
            complexity_score=7, conversation_strategy=ConversationStrategy.STEP_BY_STEP
        )

        d = instructions.model_dump()

        assert d["complexity_score"] == 7
        assert d["conversation_strategy"] == "step_by_step"

    def test_deserialization(self) -> None:
        """Test instructions can be created from dict."""
        data = {
            "conversation_strategy": "progressive",
            "complexity_score": 6,
            "parameter_handling": [
                {"pattern": ".*_id$", "type": "resource_reference", "instructions": "Handle IDs"}
            ],
        }

        instructions = LLMInstructions(**data)

        assert instructions.conversation_strategy == ConversationStrategy.PROGRESSIVE
        assert instructions.complexity_score == 6
        assert len(instructions.parameter_handling) == 1


class TestParameterHandlingRule:
    """Tests for ParameterHandlingRule model."""

    def test_basic_rule(self) -> None:
        """Test creating a basic rule."""
        rule = ParameterHandlingRule(
            pattern=".*_id$",
            type=ParameterType.RESOURCE_REFERENCE,
            instructions="List available resources",
        )

        assert rule.pattern == ".*_id$"
        assert rule.type == ParameterType.RESOURCE_REFERENCE
        assert rule.hints is None
        assert rule.examples is None

    def test_rule_with_hints(self) -> None:
        """Test rule with hints."""
        rule = ParameterHandlingRule(
            pattern="type:array",
            type=ParameterType.ARRAY,
            instructions="Handle arrays",
            hints={"default_count": "1", "max_items": "10"},
        )

        assert rule.hints is not None
        assert rule.hints["default_count"] == "1"

    def test_rule_with_examples(self) -> None:
        """Test rule with examples."""
        rule = ParameterHandlingRule(
            pattern="type:enum",
            type=ParameterType.ENUM,
            instructions="Present options",
            examples=[{"field": "disk_type", "options": ["IDE", "SATA", "SCSI"]}],
        )

        assert rule.examples is not None
        assert len(rule.examples) == 1


class TestUserGuidance:
    """Tests for UserGuidance model."""

    def test_empty_guidance(self) -> None:
        """Test guidance with defaults."""
        guidance = UserGuidance()

        assert guidance.complexity_warning is None
        assert guidance.common_pitfalls == []
        assert guidance.best_practices == []
        assert guidance.time_estimate is None

    def test_full_guidance(self) -> None:
        """Test full guidance."""
        guidance = UserGuidance(
            complexity_warning="This is complex",
            common_pitfalls=["Don't forget X", "Check Y first"],
            best_practices=["Start with Z"],
            time_estimate="5-10 minutes",
        )

        assert guidance.complexity_warning == "This is complex"
        assert len(guidance.common_pitfalls) == 2
        assert len(guidance.best_practices) == 1


# =============================================================================
# Complexity Analysis Tests
# =============================================================================


class TestEndpointComplexityAnalysis:
    """Tests for endpoint complexity analysis."""

    def test_simple_endpoint(self) -> None:
        """Test analyzing a simple endpoint."""
        analysis = analyze_endpoint_complexity(
            endpoint_id="test-1", method="GET", path="/api/items", body_schema=None
        )

        assert analysis.total_parameters == 0
        assert analysis.computed_complexity_score == 1

    def test_endpoint_with_id_references(self) -> None:
        """Test endpoint with ID reference fields."""
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "datastore_id": {"type": "string"},
                "network_id": {"type": "string"},
            },
            "required": ["name", "datastore_id"],
        }

        analysis = analyze_endpoint_complexity(
            endpoint_id="test-2", method="POST", path="/api/vms", body_schema=schema
        )

        assert analysis.total_parameters == 3
        assert analysis.required_parameters == 2
        assert analysis.has_id_references is True
        assert len(analysis.id_reference_fields) == 2
        assert "datastore_id" in analysis.id_reference_fields
        assert "network_id" in analysis.id_reference_fields

    def test_endpoint_with_nested_objects(self) -> None:
        """Test endpoint with nested objects."""
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "placement": {
                    "type": "object",
                    "properties": {
                        "datastore": {"type": "string"},
                        "cluster": {"type": "string"},
                    },
                },
            },
        }

        analysis = analyze_endpoint_complexity(
            endpoint_id="test-3", method="POST", path="/api/resources", body_schema=schema
        )

        assert analysis.nested_depth >= 1
        assert "placement" in analysis.nested_objects

    def test_endpoint_with_arrays(self) -> None:
        """Test endpoint with array fields."""
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "disks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"size": {"type": "integer"}, "type": {"type": "string"}},
                    },
                },
            },
        }

        analysis = analyze_endpoint_complexity(
            endpoint_id="test-4", method="POST", path="/api/vms", body_schema=schema
        )

        assert analysis.has_arrays is True
        assert "disks" in analysis.array_fields

    def test_endpoint_with_enums(self) -> None:
        """Test endpoint with enum fields."""
        schema = {
            "type": "object",
            "properties": {"disk_type": {"type": "string", "enum": ["IDE", "SATA", "SCSI"]}},
        }

        analysis = analyze_endpoint_complexity(
            endpoint_id="test-5", method="POST", path="/api/disks", body_schema=schema
        )

        assert analysis.has_enums is True
        assert "disk_type" in analysis.enum_fields

    def test_complex_vm_endpoint(self) -> None:
        """Test analyzing a complex VM creation endpoint."""
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "placement": {
                    "type": "object",
                    "properties": {
                        "datastore_id": {"type": "string"},
                        "cluster_id": {"type": "string"},
                        "folder": {"type": "string"},
                    },
                    "required": ["datastore_id", "cluster_id"],
                },
                "disks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "size_gb": {"type": "integer"},
                            "type": {"type": "string", "enum": ["IDE", "SATA", "SCSI"]},
                        },
                    },
                },
                "nics": {
                    "type": "array",
                    "items": {"type": "object", "properties": {"network_id": {"type": "string"}}},
                },
                "cpu_count": {"type": "integer"},
                "memory_mb": {"type": "integer"},
            },
            "required": ["name", "placement"],
        }

        analysis = analyze_endpoint_complexity(
            endpoint_id="create-vm", method="POST", path="/api/vcenter/vm", body_schema=schema
        )

        # Should be high complexity
        assert analysis.computed_complexity_score >= 7
        assert analysis.has_id_references is True
        assert analysis.has_arrays is True
        assert analysis.has_enums is True
        assert analysis.nested_depth >= 1  # placement is nested


# =============================================================================
# Instruction Generation Tests
# =============================================================================


class TestInstructionGeneration:
    """Tests for auto-generating instructions."""

    def test_simple_endpoint_instructions(self) -> None:
        """Test generating instructions for simple endpoint."""
        analysis = EndpointComplexityAnalysis(
            endpoint_id="test",
            method="GET",
            path="/items",
            total_parameters=2,
            required_parameters=1,
        )
        analysis.compute_score()

        instructions = generate_default_instructions(analysis)

        # Simple endpoints should use ALL_AT_ONCE
        assert instructions.conversation_strategy == ConversationStrategy.ALL_AT_ONCE
        assert instructions.complexity_score <= 3

    def test_id_reference_endpoint_instructions(self) -> None:
        """Test generating instructions for endpoint with ID references."""
        analysis = EndpointComplexityAnalysis(
            endpoint_id="test",
            method="POST",
            path="/vms",
            total_parameters=5,
            required_parameters=3,
            has_id_references=True,
            id_reference_fields=["datastore_id", "network_id"],
        )
        analysis.compute_score()

        instructions = generate_default_instructions(analysis)

        # Should use PREREQUISITE_FIRST for ID references
        assert instructions.conversation_strategy == ConversationStrategy.PREREQUISITE_FIRST
        assert "datastore_id" in instructions.reasoning_hints

    def test_complex_endpoint_instructions(self) -> None:
        """Test generating instructions for complex endpoint."""
        analysis = EndpointComplexityAnalysis(
            endpoint_id="test",
            method="POST",
            path="/complex",
            total_parameters=15,
            required_parameters=8,
            nested_depth=3,
            has_arrays=True,
            has_id_references=True,
            id_reference_fields=["a_id", "b_id", "c_id"],
            nested_objects=["config", "settings", "metadata"],
            array_fields=["items", "tags"],
        )
        analysis.compute_score()

        instructions = generate_default_instructions(analysis)

        # Complex endpoints should have warnings
        assert instructions.user_guidance is not None
        assert instructions.user_guidance.complexity_warning is not None
        assert instructions.complexity_score >= 7


# =============================================================================
# Default Rules Tests
# =============================================================================


class TestDefaultRules:
    """Tests for default parameter handling rules."""

    def test_default_rules_exist(self) -> None:
        """Test that default rules are defined."""
        assert len(DEFAULT_PARAMETER_RULES) >= 4

    def test_id_reference_rule(self) -> None:
        """Test ID reference rule exists and is configured."""
        id_rule = next((r for r in DEFAULT_PARAMETER_RULES if r.pattern == ".*_id$"), None)

        assert id_rule is not None
        assert id_rule.type == ParameterType.RESOURCE_REFERENCE
        assert "list" in id_rule.instructions.lower()

    def test_array_rule(self) -> None:
        """Test array rule exists."""
        array_rule = next(
            (r for r in DEFAULT_PARAMETER_RULES if r.type == ParameterType.ARRAY), None
        )

        assert array_rule is not None
        assert "array" in array_rule.pattern.lower()

    def test_enum_rule(self) -> None:
        """Test enum rule exists."""
        enum_rule = next((r for r in DEFAULT_PARAMETER_RULES if r.type == ParameterType.ENUM), None)

        assert enum_rule is not None
        assert "options" in enum_rule.instructions.lower()
