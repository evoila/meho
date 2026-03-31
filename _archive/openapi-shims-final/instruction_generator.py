"""
LLM Instruction Generator for OpenAPI Endpoints.

DEPRECATED: This module has been moved to meho_app.modules.connectors.rest.instruction_generator
This file re-exports for backward compatibility.
"""
# Re-export from the new location
from meho_app.modules.connectors.rest.instruction_generator import (
    InstructionGenerator,
    generate_instructions_for_spec,
    should_generate_instructions,
)

__all__ = [
    "InstructionGenerator",
    "generate_instructions_for_spec",
    "should_generate_instructions",
]
