"""
Tests for JinjaEngine.evaluate_expression() (Task 19 - Phase 3).

Tests evaluating Jinja2 expressions to Python objects (not strings).
"""
import pytest
from meho_core.templating import JinjaEngine, TemplateContext, StepContext, RenderError


class TestEvaluateExpression:
    """Test evaluate_expression method that returns Python objects."""
    
    def test_evaluate_returns_list(self):
        """Test that evaluate_expression returns actual list, not string."""
        engine = JinjaEngine()
        ctx = TemplateContext(
            steps={},
            inputs={'items': [1, 2, 3, 4, 5]}
        )
        
        result = engine.evaluate_expression("{{ inputs['items'] }}", ctx)
        
        assert isinstance(result, list)
        assert result == [1, 2, 3, 4, 5]
    
    def test_evaluate_returns_dict(self):
        """Test that evaluate_expression returns actual dict."""
        engine = JinjaEngine()
        ctx = TemplateContext(
            steps={"step1": StepContext(output={"key": "value"})},
            inputs={}
        )
        
        result = engine.evaluate_expression("{{ steps.step1.output }}", ctx)
        
        assert isinstance(result, dict)
        assert result == {"key": "value"}
    
    def test_evaluate_returns_int(self):
        """Test that evaluate_expression returns actual int."""
        engine = JinjaEngine()
        ctx = TemplateContext(
            steps={},
            inputs={'count': 42}
        )
        
        result = engine.evaluate_expression("{{ inputs['count'] }}", ctx)
        
        assert isinstance(result, int)
        assert result == 42
    
    def test_evaluate_returns_bool(self):
        """Test that evaluate_expression returns actual boolean."""
        engine = JinjaEngine()
        ctx = TemplateContext(
            steps={},
            inputs={'value': 15}
        )
        
        result = engine.evaluate_expression("{{ inputs['value'] > 10 }}", ctx)
        
        assert isinstance(result, bool)
        assert result is True
    
    def test_evaluate_with_filter(self):
        """Test evaluate_expression with filter operations."""
        engine = JinjaEngine()
        ctx = TemplateContext(
            steps={},
            inputs={'numbers': [1, 2, 3, 4, 5]}
        )
        
        result = engine.evaluate_expression("{{ inputs['numbers'] | length }}", ctx)
        
        assert isinstance(result, int)
        assert result == 5
    
    def test_evaluate_with_sum_filter(self):
        """Test evaluate_expression with sum filter."""
        engine = JinjaEngine()
        ctx = TemplateContext(
            steps={},
            inputs={'items': [{'value': 10}, {'value': 20}, {'value': 30}]}
        )
        
        result = engine.evaluate_expression("{{ inputs['items'] | sum('value') }}", ctx)
        
        assert isinstance(result, (int, float))
        assert result == 60
    
    def test_evaluate_complex_expression(self):
        """Test evaluate_expression with complex data access."""
        engine = JinjaEngine()
        ctx = TemplateContext(
            steps={"get_data": StepContext(output={"elements": [{"name": "a"}, {"name": "b"}]})},
            inputs={}
        )
        
        result = engine.evaluate_expression("{{ steps.get_data.output.elements }}", ctx)
        
        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["name"] == "a"
    
    def test_evaluate_without_braces(self):
        """Test that evaluate_expression works without {{ }} wrapper."""
        engine = JinjaEngine()
        ctx = TemplateContext(
            steps={},
            inputs={'data': [1, 2, 3]}
        )
        
        # Should work with or without {{ }}
        result1 = engine.evaluate_expression("inputs['data']", ctx)
        result2 = engine.evaluate_expression("{{ inputs['data'] }}", ctx)
        
        assert result1 == result2
        assert isinstance(result1, list)
    
    def test_evaluate_undefined_variable(self):
        """Test that evaluate_expression handles undefined variables gracefully."""
        engine = JinjaEngine()
        ctx = TemplateContext(steps={}, inputs={})
        
        # Jinja2 returns None for missing dict keys (graceful handling)
        result = engine.evaluate_expression("{{ inputs['missing'] }}", ctx)
        
        assert result is None
    
    def test_evaluate_vs_render(self):
        """Test difference between evaluate_expression and render_expression."""
        engine = JinjaEngine()
        ctx = TemplateContext(
            steps={},
            inputs={'items': [1, 2, 3]}
        )
        
        # evaluate_expression returns Python object
        evaluated = engine.evaluate_expression("{{ inputs['items'] }}", ctx)
        assert isinstance(evaluated, list)
        assert evaluated == [1, 2, 3]
        
        # render_expression returns string
        rendered = engine.render_expression("{{ inputs['items'] }}", ctx)
        assert isinstance(rendered, str)
        assert rendered == "[1, 2, 3]"

