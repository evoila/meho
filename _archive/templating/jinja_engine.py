"""Secure Jinja2 templating engine for workflows and composite endpoints.

This module provides a sandboxed Jinja2 environment that:
- Prevents access to private attributes and dangerous operations
- Provides custom filters for data operations
- Validates expressions before rendering
- Handles errors gracefully with detailed messages
"""

from typing import Any, Dict, Optional

from jinja2 import (
    Environment,
    StrictUndefined,
    TemplateSyntaxError,
    UndefinedError,
    meta,
)
from jinja2.sandbox import SandboxedEnvironment

from .context import TemplateContext
from .filters import register_custom_filters
from .validator import ExpressionValidator, ValidationError


class JinjaEngine:
    """Secure Jinja2 templating engine.
    
    Features:
    - Sandboxed execution (no dangerous operations)
    - Custom filters (sum, avg, selectattr, etc.)
    - Expression validation
    - Helpful error messages
    
    Usage:
        engine = JinjaEngine()
        
        # Render expression
        result = engine.render_expression(
            "{{ steps.step1.output.elements | length }}",
            context=context
        )
        
        # Render template
        result = engine.render_template(
            "Total: {{ items | sum('value') }}",
            context=context
        )
    """

    def __init__(
        self,
        strict_undefined: bool = True,
        enable_validation: bool = True,
    ):
        """Initialize Jinja2 engine.
        
        Args:
            strict_undefined: If True, raise error on undefined variables
            enable_validation: If True, validate expressions before rendering
        """
        self.enable_validation = enable_validation
        
        # Create sandboxed environment
        # Note: If not strict, we don't set undefined at all (uses default)
        env_kwargs: dict[str, Any] = {"autoescape": False}
        if strict_undefined:
            env_kwargs["undefined"] = StrictUndefined
        self.env = SandboxedEnvironment(**env_kwargs)
        
        # Register custom filters
        register_custom_filters(self.env)
        
        # Create validator
        self.validator = ExpressionValidator(self.env)

    def render_expression(
        self,
        expression: str,
        context: TemplateContext,
    ) -> Any:
        """Render a single Jinja2 expression.
        
        Args:
            expression: Jinja2 expression (e.g., "{{ steps.step1.output }}")
            context: Template context with steps, inputs, env
            
        Returns:
            Rendered value (could be string, int, dict, list, etc.)
            
        Raises:
            ValidationError: If expression is invalid
            RenderError: If rendering fails
            
        Examples:
            >>> context = TemplateContext(
            ...     steps={"step1": StepContext(output={"count": 42})},
            ...     inputs={"name": "test"}
            ... )
            >>> engine.render_expression("{{ steps.step1.output.count }}", context)
            42
            >>> engine.render_expression("{{ inputs.name }}", context)
            'test'
        """
        # Validate expression if enabled
        if self.enable_validation:
            validation_result = self.validator.validate(expression, context)
            if not validation_result.is_valid:
                raise ValidationError(
                    f"Invalid expression: {validation_result.error}",
                    expression=expression,
                    details=validation_result.error,
                )

        # Render expression
        try:
            template = self.env.from_string(expression)
            result = template.render(**context.to_dict())
            return result
        except TemplateSyntaxError as e:
            raise RenderError(
                f"Syntax error in expression: {e}",
                expression=expression,
                details=str(e),
            ) from e
        except UndefinedError as e:
            raise RenderError(
                f"Undefined variable in expression: {e}",
                expression=expression,
                details=str(e),
            ) from e
        except Exception as e:
            raise RenderError(
                f"Error rendering expression: {e}",
                expression=expression,
                details=str(e),
            ) from e

    def render_template(
        self,
        template_str: str,
        context: TemplateContext,
    ) -> str:
        """Render a complete Jinja2 template.
        
        For multi-line templates with text and expressions.
        
        Args:
            template_str: Jinja2 template string
            context: Template context with steps, inputs, env
            
        Returns:
            Rendered string
            
        Raises:
            ValidationError: If template is invalid
            RenderError: If rendering fails
            
        Examples:
            >>> template = '''
            ... Total CPU: {{ items | sum('cpu') }} GHz
            ... Total Memory: {{ items | sum('memory') }} GB
            ... '''
            >>> engine.render_template(template, context)
            'Total CPU: 42.5 GHz\\nTotal Memory: 128 GB\\n'
        """
        # Validate template if enabled
        if self.enable_validation:
            validation_result = self.validator.validate(template_str, context)
            if not validation_result.is_valid:
                raise ValidationError(
                    f"Invalid template: {validation_result.error}",
                    expression=template_str,
                    details=validation_result.error,
                )

        # Render template
        try:
            template = self.env.from_string(template_str)
            result = template.render(**context.to_dict())
            return result
        except TemplateSyntaxError as e:
            raise RenderError(
                f"Syntax error in template: {e}",
                expression=template_str,
                details=str(e),
            ) from e
        except UndefinedError as e:
            raise RenderError(
                f"Undefined variable in template: {e}",
                expression=template_str,
                details=str(e),
            ) from e
        except Exception as e:
            raise RenderError(
                f"Error rendering template: {e}",
                expression=template_str,
                details=str(e),
            ) from e

    def extract_variables(self, template_str: str) -> set[str]:
        """Extract variable names from template.
        
        Useful for determining what context is needed.
        
        Args:
            template_str: Jinja2 template string
            
        Returns:
            Set of variable names (e.g., {"steps", "inputs"})
            
        Examples:
            >>> engine.extract_variables("{{ steps.step1.output }}")
            {'steps'}
            >>> engine.extract_variables("{{ inputs.name }} - {{ env.region }}")
            {'inputs', 'env'}
        """
        try:
            ast = self.env.parse(template_str)
            return meta.find_undeclared_variables(ast)
        except TemplateSyntaxError:
            return set()

    def evaluate_expression(
        self,
        expression: str,
        context: TemplateContext,
    ) -> Any:
        """Evaluate a Jinja2 expression and return the Python object.
        
        Unlike render_expression which converts to string, this returns
        the actual Python object (list, dict, int, etc.).
        
        Args:
            expression: Jinja2 expression (e.g., "{{ inputs.items }}")
            context: Template context
            
        Returns:
            Evaluated Python object (list, dict, int, bool, etc.)
            
        Examples:
            >>> context = TemplateContext(inputs={"items": [1, 2, 3]})
            >>> engine.evaluate_expression("{{ inputs['items'] }}", context)
            [1, 2, 3]  # Returns actual list, not string
        """
        # Validate expression if enabled
        if self.enable_validation:
            validation_result = self.validator.validate(expression, context)
            if not validation_result.is_valid:
                raise ValidationError(
                    f"Invalid expression: {validation_result.error}",
                    expression=expression,
                    details=validation_result.error,
                )
        
        # Remove {{ }} wrapper if present
        expr = expression.strip()
        if expr.startswith('{{') and expr.endswith('}}'):
            expr = expr[2:-2].strip()
        
        # Compile and evaluate expression
        try:
            compiled = self.env.compile_expression(expr)
            result = compiled(**context.to_dict())
            return result
        except Exception as e:
            raise RenderError(
                f"Error evaluating expression: {e}",
                expression=expression,
                details=str(e),
            ) from e
    
    def validate_expression(
        self,
        expression: str,
        context: Optional[TemplateContext] = None,
    ) -> bool:
        """Validate expression without rendering.
        
        Args:
            expression: Jinja2 expression
            context: Optional context to validate against
            
        Returns:
            True if valid, False otherwise
        """
        result = self.validator.validate(expression, context)
        return result.is_valid


class RenderError(Exception):
    """Error during template rendering.
    
    Provides detailed error information for debugging.
    """

    def __init__(
        self,
        message: str,
        expression: Optional[str] = None,
        details: Optional[str] = None,
    ):
        """Initialize render error.
        
        Args:
            message: High-level error message
            expression: The expression that failed
            details: Detailed error information
        """
        super().__init__(message)
        self.expression = expression
        self.details = details

    def __str__(self) -> str:
        parts = [super().__str__()]
        if self.expression:
            parts.append(f"Expression: {self.expression}")
        if self.details:
            parts.append(f"Details: {self.details}")
        return "\n".join(parts)

