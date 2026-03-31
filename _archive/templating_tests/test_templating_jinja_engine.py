"""Unit tests for Jinja2 templating engine."""

import pytest

from meho_core.templating.context import StepContext, TemplateContext
from meho_core.templating.jinja_engine import JinjaEngine, RenderError
from meho_core.templating.validator import ValidationError


class TestRenderError:
    """Tests for RenderError exception."""

    def test_basic_error(self):
        """Should create basic render error."""
        error = RenderError("Render failed")
        assert str(error) == "Render failed"

    def test_error_with_expression(self):
        """Should include expression in error."""
        error = RenderError(
            "Render failed",
            expression="{{ x }}",
        )
        error_str = str(error)
        assert "Render failed" in error_str
        assert "{{ x }}" in error_str

    def test_error_with_details(self):
        """Should include details in error."""
        error = RenderError(
            "Render failed",
            expression="{{ x }}",
            details="Variable not found",
        )
        error_str = str(error)
        assert "Render failed" in error_str
        assert "Variable not found" in error_str


class TestJinjaEngine:
    """Tests for JinjaEngine."""

    def setup_method(self):
        """Set up engine for each test."""
        self.engine = JinjaEngine()

    def test_init_default(self):
        """Should initialize with defaults."""
        engine = JinjaEngine()
        assert engine.enable_validation is True
        assert engine.env is not None
        assert engine.validator is not None

    def test_init_without_validation(self):
        """Should allow disabling validation."""
        engine = JinjaEngine(enable_validation=False)
        assert engine.enable_validation is False

    def test_render_simple_expression(self):
        """Should render simple expression."""
        context = TemplateContext(inputs={"name": "test"})
        result = self.engine.render_expression("{{ inputs.name }}", context)
        
        assert result == "test"

    def test_render_nested_access(self):
        """Should render nested data access."""
        context = TemplateContext(
            steps={
                "step1": StepContext(output={"data": {"count": 42}})
            }
        )
        result = self.engine.render_expression(
            "{{ steps.step1.output.data.count }}",
            context,
        )
        
        assert result == "42"

    def test_render_with_filter(self):
        """Should render expression with filter."""
        context = TemplateContext(
            steps={
                "step1": StepContext(output=[1, 2, 3])
            }
        )
        result = self.engine.render_expression(
            "{{ steps.step1.output | length }}",
            context,
        )
        
        assert result == "3"

    def test_render_custom_filter_sum(self):
        """Should use custom sum filter."""
        items = [
            {"value": 10},
            {"value": 20},
            {"value": 30},
        ]
        context = TemplateContext(
            steps={"step1": StepContext(output=items)}
        )
        result = self.engine.render_expression(
            "{{ steps.step1.output | sum('value') }}",
            context,
        )
        
        assert float(result) == 60.0

    def test_render_custom_filter_avg(self):
        """Should use custom avg filter."""
        items = [
            {"value": 10},
            {"value": 20},
            {"value": 30},
        ]
        context = TemplateContext(
            steps={"step1": StepContext(output=items)}
        )
        result = self.engine.render_expression(
            "{{ steps.step1.output | avg('value') }}",
            context,
        )
        
        assert float(result) == 20.0

    def test_render_custom_filter_selectattr(self):
        """Should use custom selectattr filter."""
        items = [
            {"status": "ACTIVE", "name": "item1"},
            {"status": "INACTIVE", "name": "item2"},
            {"status": "ACTIVE", "name": "item3"},
        ]
        context = TemplateContext(
            steps={"step1": StepContext(output=items)}
        )
        result = self.engine.render_expression(
            "{{ (steps.step1.output | selectattr('status', 'eq', 'ACTIVE')) | length }}",
            context,
        )
        
        assert result == "2"

    def test_render_with_default_filter(self):
        """Should use default filter for missing values."""
        context = TemplateContext(
            steps={"step1": StepContext(output={})}
        )
        result = self.engine.render_expression(
            "{{ steps.step1.output.missing | default('N/A') }}",
            context,
        )
        
        assert result == "N/A"

    def test_render_template_multiline(self):
        """Should render multi-line template."""
        context = TemplateContext(
            inputs={"name": "MEHO", "version": "1.0"}
        )
        template = """
Name: {{ inputs.name }}
Version: {{ inputs.version }}
        """
        result = self.engine.render_template(template, context)
        
        assert "MEHO" in result
        assert "1.0" in result

    def test_render_template_with_logic(self):
        """Should render template with conditional logic."""
        context = TemplateContext(inputs={"count": 5})
        template = """
{% if inputs.count > 3 %}
Many items
{% else %}
Few items
{% endif %}
        """
        result = self.engine.render_template(template, context)
        
        assert "Many items" in result
        assert "Few items" not in result

    def test_render_undefined_variable_strict(self):
        """Should raise error for undefined variable in strict mode."""
        context = TemplateContext()
        
        with pytest.raises(RenderError) as exc_info:
            self.engine.render_expression("{{ undefined_var }}", context)
        
        assert "undefined" in str(exc_info.value).lower()

    def test_render_invalid_expression_syntax(self):
        """Should raise ValidationError for invalid syntax."""
        context = TemplateContext()
        
        with pytest.raises(ValidationError):
            self.engine.render_expression("{{ bad syntax", context)

    def test_render_dangerous_pattern(self):
        """Should raise ValidationError for dangerous patterns."""
        context = TemplateContext()
        
        with pytest.raises(ValidationError) as exc_info:
            self.engine.render_expression("{{ obj.__class__ }}", context)
        
        assert "dangerous" in str(exc_info.value).lower()

    def test_render_without_validation(self):
        """Should skip validation when disabled."""
        engine = JinjaEngine(enable_validation=False, strict_undefined=False)
        context = TemplateContext()
        
        # This would fail validation but should render (to empty string)
        result = engine.render_expression("{{ missing | default('') }}", context)
        assert result == ""

    def test_extract_variables(self):
        """Should extract variables from template."""
        variables = self.engine.extract_variables("{{ steps.step1.output }}")
        assert "steps" in variables

    def test_extract_variables_multiple(self):
        """Should extract multiple variables."""
        variables = self.engine.extract_variables(
            "{{ steps.step1.output }} {{ inputs.name }} {{ env.region }}"
        )
        assert variables == {"steps", "inputs", "env"}

    def test_extract_variables_invalid_syntax(self):
        """Should return empty set for invalid syntax."""
        variables = self.engine.extract_variables("{{ bad syntax")
        assert variables == set()

    def test_validate_expression_valid(self):
        """Should validate correct expression."""
        assert self.engine.validate_expression("{{ x }}") is True

    def test_validate_expression_invalid(self):
        """Should reject invalid expression."""
        assert self.engine.validate_expression("{{ bad syntax") is False

    def test_render_json_filter(self):
        """Should use json filter for LLM prompts."""
        data = {"clusters": [{"name": "c1"}, {"name": "c2"}]}
        context = TemplateContext(
            steps={"step1": StepContext(output=data)}
        )
        result = self.engine.render_expression(
            "{{ steps.step1.output | json }}",
            context,
        )
        
        import json
        parsed = json.loads(result)
        assert parsed == data

    def test_render_groupby_filter(self):
        """Should use groupby filter."""
        items = [
            {"type": "web", "name": "s1"},
            {"type": "db", "name": "d1"},
            {"type": "web", "name": "s2"},
        ]
        context = TemplateContext(
            steps={"step1": StepContext(output=items)}
        )
        
        # Group and count
        template = """
        {% set groups = steps.step1.output | groupby('type') %}
        {{ groups.keys() | list | length }}
        """
        result = self.engine.render_template(template, context)
        
        assert "2" in result

    def test_sandboxed_execution(self):
        """Should prevent unsafe operations via sandbox."""
        context = TemplateContext()
        
        # Try to access __class__ (should fail during validation)
        with pytest.raises(ValidationError):
            self.engine.render_expression("{{ ''.__class__ }}", context)

    def test_render_with_step_metadata(self):
        """Should access step metadata (status_code, duration, etc.)."""
        context = TemplateContext(
            steps={
                "step1": StepContext(
                    output={"data": "test"},
                    status_code=200,
                    duration_ms=123.45,
                )
            }
        )
        
        # Access status code
        result = self.engine.render_expression(
            "{{ steps.step1.status_code }}",
            context,
        )
        assert result == "200"
        
        # Access duration
        result = self.engine.render_expression(
            "{{ steps.step1.duration_ms }}",
            context,
        )
        assert float(result) == 123.45

