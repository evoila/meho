"""Expression validation for Jinja2 templates.

Validates that expressions:
- Have valid syntax
- Reference existing context variables
- Use correct filter syntax
- Don't attempt dangerous operations
"""

from dataclasses import dataclass
from typing import Optional, Set

from jinja2 import Environment, TemplateSyntaxError, meta

from .context import TemplateContext


@dataclass
class ValidationResult:
    """Result of expression validation.
    
    Attributes:
        is_valid: Whether expression is valid
        error: Error message if invalid
        warnings: List of non-fatal warnings
        variables_used: Set of variables referenced in expression
    """

    is_valid: bool
    error: Optional[str] = None
    warnings: Optional[list[str]] = None
    variables_used: Optional[Set[str]] = None

    def __post_init__(self) -> None:
        if self.warnings is None:
            self.warnings = []
        if self.variables_used is None:
            self.variables_used = set()


class ValidationError(Exception):
    """Error during expression validation.
    
    Raised when an expression is invalid and cannot be rendered safely.
    """

    def __init__(
        self,
        message: str,
        expression: Optional[str] = None,
        details: Optional[str] = None,
    ):
        """Initialize validation error.
        
        Args:
            message: High-level error message
            expression: The invalid expression
            details: Detailed validation failure information
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


class ExpressionValidator:
    """Validates Jinja2 expressions before rendering.
    
    Performs static analysis to catch errors early:
    - Syntax errors
    - Undefined variable references
    - Invalid filter usage
    - Potentially unsafe operations
    
    Usage:
        validator = ExpressionValidator(jinja_env)
        result = validator.validate(expression, context)
        if not result.is_valid:
            print(f"Error: {result.error}")
    """

    def __init__(self, env: Environment):
        """Initialize validator.
        
        Args:
            env: Jinja2 environment (for parsing and filters)
        """
        self.env = env

    def validate(
        self,
        expression: str,
        context: Optional[TemplateContext] = None,
    ) -> ValidationResult:
        """Validate a Jinja2 expression.
        
        Args:
            expression: Jinja2 expression or template
            context: Optional context to validate against
            
        Returns:
            ValidationResult with is_valid flag and error details
            
        Examples:
            >>> result = validator.validate("{{ steps.step1.output }}")
            >>> result.is_valid
            True
            
            >>> result = validator.validate("{{ invalid syntax")
            >>> result.is_valid
            False
            >>> result.error
            'Syntax error: ...'
        """
        # Check for empty expression
        if not expression or not expression.strip():
            return ValidationResult(
                is_valid=False,
                error="Expression cannot be empty",
            )

        # Check syntax
        try:
            ast = self.env.parse(expression)
        except TemplateSyntaxError as e:
            return ValidationResult(
                is_valid=False,
                error=f"Syntax error: {e}",
            )

        # Extract variables used
        try:
            variables_used = meta.find_undeclared_variables(ast)
        except Exception as e:
            return ValidationResult(
                is_valid=False,
                error=f"Error analyzing expression: {e}",
            )

        # If context provided, validate variable references
        warnings = []
        if context is not None:
            available_vars = {"steps", "inputs", "env"}
            
            for var in variables_used:
                if var not in available_vars:
                    warnings.append(
                        f"Variable '{var}' not in standard context "
                        f"(expected: {', '.join(available_vars)})"
                    )

            # Check if referenced steps exist
            if "steps" in variables_used:
                # This is a basic check; deep validation would require
                # parsing the AST to extract specific step IDs
                pass

        # Check for potentially dangerous operations
        # The sandboxed environment handles most of this, but we can
        # add extra checks here if needed
        dangerous_patterns = [
            "__",  # Private attributes
            "import",  # Module imports
            "eval",  # Code evaluation
            "exec",  # Code execution
        ]
        
        for pattern in dangerous_patterns:
            if pattern in expression:
                return ValidationResult(
                    is_valid=False,
                    error=f"Expression contains potentially dangerous pattern: {pattern}",
                )

        # Validation passed
        return ValidationResult(
            is_valid=True,
            warnings=warnings,
            variables_used=variables_used,
        )

    def validate_filter_usage(
        self,
        filter_name: str,
        args: list[str],
    ) -> ValidationResult:
        """Validate that a filter is used correctly.
        
        Args:
            filter_name: Name of the filter (e.g., "sum", "avg")
            args: Arguments passed to the filter
            
        Returns:
            ValidationResult indicating if usage is correct
        """
        # Check if filter exists
        if filter_name not in self.env.filters:
            return ValidationResult(
                is_valid=False,
                error=f"Unknown filter: {filter_name}",
            )

        # For custom filters, validate expected argument count
        # This is a basic check; more sophisticated validation would
        # inspect the filter function signature
        custom_filters_args = {
            "sum_attr": 1,  # Requires attribute path
            "avg_attr": 1,
            "selectattr_custom": 3,  # Requires attr, op, value
            "groupby_attr": 1,
            "json_dumps": 0,  # Optional indent
        }

        if filter_name in custom_filters_args:
            expected = custom_filters_args[filter_name]
            if len(args) < expected:
                return ValidationResult(
                    is_valid=False,
                    error=f"Filter '{filter_name}' requires at least {expected} argument(s)",
                )

        return ValidationResult(is_valid=True)

