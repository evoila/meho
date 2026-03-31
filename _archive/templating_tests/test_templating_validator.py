"""Unit tests for template expression validation."""

import pytest
from jinja2 import Environment

from meho_core.templating.context import StepContext, TemplateContext
from meho_core.templating.validator import (
    ExpressionValidator,
    ValidationError,
    ValidationResult,
)


class TestValidationResult:
    """Tests for ValidationResult dataclass."""

    def test_valid_result(self):
        """Should create valid result."""
        result = ValidationResult(is_valid=True)
        
        assert result.is_valid is True
        assert result.error is None
        assert result.warnings == []
        assert result.variables_used == set()

    def test_invalid_result_with_error(self):
        """Should create invalid result with error."""
        result = ValidationResult(
            is_valid=False,
            error="Syntax error",
        )
        
        assert result.is_valid is False
        assert result.error == "Syntax error"

    def test_result_with_warnings(self):
        """Should include warnings."""
        result = ValidationResult(
            is_valid=True,
            warnings=["Warning 1", "Warning 2"],
        )
        
        assert result.is_valid is True
        assert len(result.warnings) == 2


class TestValidationError:
    """Tests for ValidationError exception."""

    def test_basic_error(self):
        """Should create basic error."""
        error = ValidationError("Invalid expression")
        assert str(error) == "Invalid expression"

    def test_error_with_expression(self):
        """Should include expression in error."""
        error = ValidationError(
            "Invalid syntax",
            expression="{{ bad",
        )
        error_str = str(error)
        assert "Invalid syntax" in error_str
        assert "{{ bad" in error_str

    def test_error_with_details(self):
        """Should include details in error."""
        error = ValidationError(
            "Validation failed",
            expression="{{ x }}",
            details="Variable 'x' not found",
        )
        error_str = str(error)
        assert "Validation failed" in error_str
        assert "{{ x }}" in error_str
        assert "not found" in error_str


class TestExpressionValidator:
    """Tests for ExpressionValidator."""

    def setup_method(self):
        """Set up validator for each test."""
        from meho_core.templating.filters import register_custom_filters
        
        self.env = Environment()
        register_custom_filters(self.env)  # Register filters for validation tests
        self.validator = ExpressionValidator(self.env)

    def test_validate_empty_expression(self):
        """Should reject empty expression."""
        result = self.validator.validate("")
        
        assert result.is_valid is False
        assert "empty" in result.error.lower()

    def test_validate_valid_simple_expression(self):
        """Should accept valid simple expression."""
        result = self.validator.validate("{{ x }}")
        
        assert result.is_valid is True
        assert result.error is None

    def test_validate_syntax_error(self):
        """Should reject syntax errors."""
        result = self.validator.validate("{{ bad syntax")
        
        assert result.is_valid is False
        assert "syntax" in result.error.lower()

    def test_validate_dangerous_pattern_double_underscore(self):
        """Should reject double underscore (private attributes)."""
        result = self.validator.validate("{{ obj.__class__ }}")
        
        assert result.is_valid is False
        assert "dangerous" in result.error.lower()

    def test_validate_dangerous_pattern_import(self):
        """Should reject import statements."""
        result = self.validator.validate("{{ 'import' in 'test' }}")
        
        # This should be valid syntax, but we check for 'import' keyword
        # The actual check looks for the string 'import' in the expression
        result2 = self.validator.validate("import os")
        assert result2.is_valid is False
        assert "dangerous" in result2.error.lower()

    def test_validate_dangerous_pattern_eval(self):
        """Should reject eval."""
        result = self.validator.validate("{{ eval('1+1') }}")
        
        assert result.is_valid is False
        assert "dangerous" in result.error.lower()

    def test_validate_dangerous_pattern_exec(self):
        """Should reject exec."""
        result = self.validator.validate("{{ exec('print(1)') }}")
        
        assert result.is_valid is False
        assert "dangerous" in result.error.lower()

    def test_extract_variables(self):
        """Should extract variable names from expression."""
        result = self.validator.validate("{{ steps.step1.output }}")
        
        assert result.is_valid is True
        assert "steps" in result.variables_used

    def test_extract_multiple_variables(self):
        """Should extract multiple variables."""
        result = self.validator.validate(
            "{{ steps.step1.output }} {{ inputs.name }}"
        )
        
        assert result.is_valid is True
        assert "steps" in result.variables_used
        assert "inputs" in result.variables_used

    def test_validate_with_context(self):
        """Should validate against provided context."""
        context = TemplateContext(
            steps={"step1": StepContext(output={"x": 1})},
            inputs={"name": "test"},
        )
        
        result = self.validator.validate(
            "{{ steps.step1.output }}",
            context=context,
        )
        
        assert result.is_valid is True

    def test_validate_unknown_variable_warning(self):
        """Should warn about unknown variables."""
        context = TemplateContext()
        
        result = self.validator.validate(
            "{{ unknown_var }}",
            context=context,
        )
        
        # Should still be valid (might be runtime variable)
        # but should have warning
        assert result.is_valid is True
        assert len(result.warnings) > 0
        assert "unknown_var" in result.warnings[0]

    def test_validate_complex_expression(self):
        """Should validate complex expressions."""
        result = self.validator.validate(
            "{{ steps.step1.output.elements | length }}"
        )
        
        assert result.is_valid is True

    def test_validate_filter_usage_known_filter(self):
        """Should validate known filter usage."""
        # Register a simple filter
        self.env.filters["test_filter"] = lambda x: x
        
        result = self.validator.validate_filter_usage(
            "test_filter",
            [],
        )
        
        assert result.is_valid is True

    def test_validate_filter_usage_unknown_filter(self):
        """Should reject unknown filter."""
        result = self.validator.validate_filter_usage(
            "unknown_filter",
            [],
        )
        
        assert result.is_valid is False
        assert "unknown" in result.error.lower()

    def test_validate_filter_usage_insufficient_args(self):
        """Should check filter argument count."""
        result = self.validator.validate_filter_usage(
            "sum_attr",
            [],  # Requires 1 arg
        )
        
        assert result.is_valid is False
        assert "argument" in result.error.lower()

    def test_validate_multiline_template(self):
        """Should validate multi-line templates."""
        template = """
        Total: {{ items | length }}
        Sum: {{ items | sum }}
        """
        
        result = self.validator.validate(template)
        assert result.is_valid is True

