"""
LLM Instructions Schema for Schema-Guided Parameter Collection.

DEPRECATED: This module has been moved to meho_app.modules.connectors.rest.llm_instructions
This file re-exports for backward compatibility.
"""
# Re-export from the new location
from meho_app.modules.connectors.rest.llm_instructions import (
    ConversationStrategy,
    ParameterType,
    ParameterHandlingRule,
    PrerequisiteFlow,
    ConversationTurn,
    UserGuidance,
    LLMInstructions,
    EndpointComplexityAnalysis,
    DEFAULT_PARAMETER_RULES,
    analyze_endpoint_complexity,
    generate_default_instructions,
)

__all__ = [
    "ConversationStrategy",
    "ParameterType",
    "ParameterHandlingRule",
    "PrerequisiteFlow",
    "ConversationTurn",
    "UserGuidance",
    "LLMInstructions",
    "EndpointComplexityAnalysis",
    "DEFAULT_PARAMETER_RULES",
    "analyze_endpoint_complexity",
    "generate_default_instructions",
]
