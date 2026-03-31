"""Shared templating engine for workflows and composite endpoints.

This module provides a unified Jinja2-based templating system that:
- Supports secure, sandboxed template rendering
- Provides custom filters for data operations (sum, avg, selectattr, etc.)
- Validates expressions before execution
- Shared between workflows (Phase 3) and composite endpoints (TASK-74)

Expression Syntax:
    {{ inputs.app_name }}                              # Input parameter
    {{ steps.step1.output }}                           # Full step output
    {{ steps.step1.output.elements }}                  # Nested access
    {{ steps.step1.output.elements | length }}         # Count items
    {{ steps.step1.output.elements | sum('capacity.cpu') }}  # Aggregate
    {{ value | default('N/A') }}                       # Fallback
"""

from .context import StepContext, TemplateContext
from .filters import register_custom_filters
from .jinja_engine import JinjaEngine, RenderError
from .validator import ExpressionValidator, ValidationError

__all__ = [
    "JinjaEngine",
    "RenderError",
    "TemplateContext",
    "StepContext",
    "ExpressionValidator",
    "ValidationError",
    "register_custom_filters",
]

