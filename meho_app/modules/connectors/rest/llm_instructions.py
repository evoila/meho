# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
LLM Instructions Schema for Schema-Guided Parameter Collection.

This module defines the data models for storing per-endpoint LLM guidance
that teaches the agent HOW to help users through complex parameter collection.

The key insight is that instead of hardcoded workflows or rigid templates,
we provide guidance metadata that the LLM interprets dynamically.

Example:
    For POST /api/vcenter/vm (create VM), the LLM instructions might include:
    - conversation_strategy: "step_by_step"
    - complexity_score: 9
    - parameter_handling: rules for *_id fields, nested objects, arrays
    - example_conversation: few-shot examples

This enables the agent to:
    1. Recognize when to offer "list available resources"
    2. Break down complex nested objects conversationally
    3. Guide through arrays one item at a time
    4. Adapt to user expertise level
"""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ConversationStrategy(StrEnum):
    """Strategy for collecting parameters from user."""

    STEP_BY_STEP = "step_by_step"  # Break into small questions
    ALL_AT_ONCE = "all_at_once"  # Ask for all params together (simple endpoints)
    PROGRESSIVE = "progressive"  # Start simple, add complexity as needed
    PREREQUISITE_FIRST = "prerequisite_first"  # List resources first, then ask for selections


class ParameterType(StrEnum):
    """Type of parameter for handling rules."""

    RESOURCE_REFERENCE = "resource_reference"  # Points to another resource (e.g., datastore_id)
    NESTED_OBJECT = "nested_object"  # Complex object requiring breakdown
    ARRAY = "array"  # List of items
    ENUM = "enum"  # Fixed set of options
    COMPUTED = "computed"  # Can be auto-calculated
    SIMPLE = "simple"  # Simple scalar value


class ParameterHandlingRule(BaseModel):
    """
    Rule for handling specific parameter patterns.

    The LLM uses these rules to determine how to collect each parameter type.
    """

    pattern: str = Field(
        description="Regex or type pattern (e.g., '.*_id$', 'type:object', 'type:array')"
    )

    type: ParameterType = Field(description="How this parameter should be handled")

    instructions: str = Field(
        description="Natural language instructions for the LLM on how to handle this parameter type"
    )

    hints: dict[str, str] | None = Field(
        default=None,
        description="Specific hints (e.g., list_endpoint_hint, create_endpoint_hint, default_value)",
    )

    examples: list[dict[str, Any]] | None = Field(
        default=None, description="Example parameter instances showing the pattern"
    )


class PrerequisiteFlow(BaseModel):
    """
    Common prerequisite pattern for discovering/creating dependent resources.

    Example: Before creating a VM, user might need to:
    - List available datastores
    - List available networks
    - Optionally create a new network
    """

    name: str = Field(description="Flow pattern name (e.g., 'list_then_select')")
    description: str = Field(description="What this flow does")
    applies_to: list[str] = Field(description="Resource types this applies to")
    steps: list[str] | None = Field(default=None, description="Suggested step sequence")


class ConversationTurn(BaseModel):
    """Example conversation turn for few-shot learning."""

    agent: str = Field(description="What the agent says")
    user: str = Field(description="What the user responds")
    context: str | None = Field(
        default=None, description="Why agent said this (for LLM understanding)"
    )


class UserGuidance(BaseModel):
    """Helpful tips to show users during parameter collection."""

    complexity_warning: str | None = Field(
        default=None, description="Warning about operation complexity"
    )
    common_pitfalls: list[str] = Field(default_factory=list, description="Common mistakes to avoid")
    best_practices: list[str] = Field(default_factory=list, description="Recommended approaches")
    time_estimate: str | None = Field(
        default=None, description="Estimated time (e.g., '5-10 minutes')"
    )


class LLMInstructions(BaseModel):
    """
    Instructions that guide the LLM in helping users build requests
    to complex API endpoints.

    These instructions are stored per-endpoint and retrieved when the
    agent needs to help a user call that endpoint.

    Key design principle: The LLM interprets these instructions dynamically
    rather than following a rigid script. This allows adaptation to:
    - User expertise level
    - Context from conversation
    - Partial information already provided
    """

    conversation_strategy: ConversationStrategy = Field(
        default=ConversationStrategy.STEP_BY_STEP,
        description="Overall approach for collecting parameters",
    )

    complexity_score: int = Field(
        default=5,
        ge=1,
        le=10,
        description="1=simple (1-2 params), 10=very complex (nested, arrays, prerequisites)",
    )

    parameter_handling: list[ParameterHandlingRule] = Field(
        default_factory=list, description="Rules for handling different parameter patterns"
    )

    prerequisite_flows: list[PrerequisiteFlow] = Field(
        default_factory=list, description="Common prerequisite sequences for this endpoint"
    )

    example_conversation: list[ConversationTurn] = Field(
        default_factory=list,
        description="Example conversation showing how to guide users (few-shot)",
    )

    user_guidance: UserGuidance | None = Field(
        default=None, description="Tips and warnings to show users"
    )

    reasoning_hints: str | None = Field(
        default=None, description="Additional context to help LLM reason about this endpoint"
    )


class EndpointComplexityAnalysis(BaseModel):
    """
    Result of analyzing an endpoint's schema for complexity.

    This is used by the instruction generator to determine what
    kind of guidance to create.
    """

    endpoint_id: str
    method: str
    path: str

    # Complexity indicators
    total_parameters: int = 0
    required_parameters: int = 0
    nested_depth: int = 0
    has_arrays: bool = False
    has_id_references: bool = False
    has_enums: bool = False

    # Detected patterns
    id_reference_fields: list[str] = Field(default_factory=list)
    nested_objects: list[str] = Field(default_factory=list)
    array_fields: list[str] = Field(default_factory=list)
    enum_fields: list[str] = Field(default_factory=list)

    # Computed score
    computed_complexity_score: int = 1

    def compute_score(self) -> int:
        """Calculate complexity score based on indicators."""
        score = 1

        # Base score from parameter count
        if self.total_parameters > 10:
            score += 2
        elif self.total_parameters > 5:
            score += 1

        # Required parameters add complexity
        if self.required_parameters > 5:
            score += 2
        elif self.required_parameters > 2:
            score += 1

        # Nested depth
        score += min(self.nested_depth, 3)

        # Special patterns
        if self.has_arrays:
            score += 1
        if self.has_id_references:
            score += 2  # Need to discover resources
        if len(self.id_reference_fields) > 2:
            score += 1

        self.computed_complexity_score = min(score, 10)
        return self.computed_complexity_score


# =============================================================================
# Default Rules - Applied to all endpoints unless overridden
# =============================================================================

DEFAULT_PARAMETER_RULES: list[ParameterHandlingRule] = [
    ParameterHandlingRule(
        pattern=".*_id$",
        type=ParameterType.RESOURCE_REFERENCE,
        instructions="""
When you encounter a parameter ending in '_id' (e.g., datastore_id, network_id):

1. Identify what resource it references (remove _id suffix)
2. Ask user: "Would you like me to list available [resource]s, or do you have a specific ID?"
3. If user says "list" or "show me":
   - Search for list endpoint: "GET /api/[resource]" or similar
   - Call that endpoint
   - Present results: "Found X [resource]s: [name1], [name2], ... Which one?"
4. If user provides an ID directly, validate format and use it
5. Store the selected/created ID for use in the main request
""",
        hints={
            "list_endpoint_pattern": "GET /api/{resource_type}",
            "validation": "UUID or alphanumeric string",
        },
    ),
    ParameterHandlingRule(
        pattern="type:object",
        type=ParameterType.NESTED_OBJECT,
        instructions="""
For nested objects (objects within the request body):

1. Identify the object's purpose from its name and description
2. Announce to user: "Let's configure [object_name] ([brief_description])"
3. List required fields in the object
4. Guide through EACH required field using appropriate handling rules
5. After required fields, ask: "Would you like to configure optional settings for [object_name]?"
6. If yes, guide through optional fields
7. If no, use defaults or omit optional fields
8. Confirm: "Great! [Object_name] configured with [summary]"
""",
    ),
    ParameterHandlingRule(
        pattern="type:array",
        type=ParameterType.ARRAY,
        instructions="""
For array parameters (lists of items):

1. Explain what items in the array represent
2. Check if there's a common default (e.g., "most VMs need just 1 disk")
3. Ask: "How many [items] do you want to configure? (Most common: [default_count])"
4. For EACH item:
   a. Announce: "Configuring [item_type] #[index]"
   b. Guide through item schema using appropriate rules
   c. Confirm: "[Item_type] #[index] configured"
5. After each item, ask: "Add another [item_type]? (yes/no)"
6. If yes, repeat; if no, proceed
7. Summarize: "Total [items]: [count] configured"
""",
        hints={"max_items_recommendation": "4-5 (ask if user really needs more)"},
    ),
    ParameterHandlingRule(
        pattern="enum:.*",
        type=ParameterType.ENUM,
        instructions="""
For enum fields (fixed set of valid options):

1. Present ALL valid options clearly
2. If there's a recommended/common choice, highlight it
3. Format: "[Field_name]? Options: [opt1], [opt2], [opt3] (recommended: [opt])"
4. Accept exact match or fuzzy match (e.g., "scsi" matches "SCSI")
5. If invalid choice, re-present options: "That's not a valid option. Please choose from: [options]"
""",
    ),
    ParameterHandlingRule(
        pattern="required:false",
        type=ParameterType.SIMPLE,
        instructions="""
For optional parameters that have sensible defaults:

1. After all required fields collected, ask: "Would you like to customize [optional_field]?"
2. Show default value: "(Default: [default_value])"
3. If user says "no" or "use default", skip
4. If user says "yes" or "customize", ask for value
5. Don't ask about every optional field - group related ones
""",
    ),
]


# =============================================================================
# Utility Functions
# =============================================================================


def analyze_endpoint_complexity(
    endpoint_id: str,
    method: str,
    path: str,
    body_schema: dict[str, Any] | None,
    parameters: list[dict[str, Any]] | None = None,
) -> EndpointComplexityAnalysis:
    """
    Analyze an endpoint's schema to determine complexity.

    This is used to auto-generate LLM instructions.
    """
    analysis = EndpointComplexityAnalysis(
        endpoint_id=endpoint_id,
        method=method,
        path=path,
    )

    if not body_schema:
        return analysis

    def analyze_schema(schema: dict[str, Any], path: str = "", depth: int = 0) -> None:
        """Recursively analyze schema."""
        if depth > analysis.nested_depth:
            analysis.nested_depth = depth

        schema.get("type")
        properties = schema.get("properties", {})
        required = schema.get("required", [])

        for prop_name, prop_schema in properties.items():
            full_path = f"{path}.{prop_name}" if path else prop_name
            analysis.total_parameters += 1

            if prop_name in required:
                analysis.required_parameters += 1

            prop_type = prop_schema.get("type")

            # Check for ID references
            if prop_name.endswith("_id") or prop_name.endswith("_ids"):
                analysis.has_id_references = True
                analysis.id_reference_fields.append(full_path)

            # Check for enums
            if "enum" in prop_schema:
                analysis.has_enums = True
                analysis.enum_fields.append(full_path)

            # Check for arrays
            if prop_type == "array":
                analysis.has_arrays = True
                analysis.array_fields.append(full_path)
                if "items" in prop_schema:
                    analyze_schema(prop_schema["items"], full_path + "[]", depth + 1)

            # Check for nested objects
            elif prop_type == "object":
                analysis.nested_objects.append(full_path)
                analyze_schema(prop_schema, full_path, depth + 1)

    analyze_schema(body_schema)
    analysis.compute_score()

    return analysis


def generate_default_instructions(
    analysis: EndpointComplexityAnalysis,
) -> LLMInstructions:
    """
    Generate default LLM instructions based on complexity analysis.

    This provides a starting point that can be refined by admins.
    """
    # Determine strategy based on complexity
    if analysis.computed_complexity_score <= 3:
        strategy = ConversationStrategy.ALL_AT_ONCE
    elif analysis.has_id_references:
        strategy = ConversationStrategy.PREREQUISITE_FIRST
    elif analysis.computed_complexity_score >= 7:
        strategy = ConversationStrategy.STEP_BY_STEP
    else:
        strategy = ConversationStrategy.PROGRESSIVE

    # Build reasoning hints
    hints_parts = []
    if analysis.has_id_references:
        hints_parts.append(
            f"This endpoint has {len(analysis.id_reference_fields)} ID reference fields: "
            f"{', '.join(analysis.id_reference_fields)}. "
            "Offer to list available resources for each."
        )
    if analysis.has_arrays:
        hints_parts.append(
            f"Arrays: {', '.join(analysis.array_fields)}. "
            "Ask how many items, then guide through each."
        )
    if analysis.nested_objects:
        hints_parts.append(
            f"Nested objects: {', '.join(analysis.nested_objects[:5])}. "
            "Break down into sub-conversations."
        )

    # Build user guidance
    user_guidance = None
    if analysis.computed_complexity_score >= 7:
        user_guidance = UserGuidance(
            complexity_warning=(
                f"This operation requires {analysis.required_parameters} required fields "
                f"across {len(analysis.nested_objects)} nested objects."
            ),
            time_estimate="5-10 minutes for full configuration",
        )

    return LLMInstructions(
        conversation_strategy=strategy,
        complexity_score=analysis.computed_complexity_score,
        parameter_handling=DEFAULT_PARAMETER_RULES.copy(),
        reasoning_hints="\n".join(hints_parts) if hints_parts else None,
        user_guidance=user_guidance,
    )
