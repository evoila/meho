"""Unit tests for templating context classes."""

import pytest

from meho_core.templating.context import StepContext, TemplateContext


class TestStepContext:
    """Tests for StepContext."""

    def test_init_with_output(self):
        """Should initialize with output data."""
        ctx = StepContext(output={"result": 42})
        
        assert ctx.output == {"result": 42}
        assert ctx.status_code is None
        assert ctx.error is None
        assert ctx.duration_ms is None

    def test_init_with_all_fields(self):
        """Should initialize with all optional fields."""
        ctx = StepContext(
            output={"data": "test"},
            status_code=200,
            error=None,
            duration_ms=123.45,
        )
        
        assert ctx.output == {"data": "test"}
        assert ctx.status_code == 200
        assert ctx.error is None
        assert ctx.duration_ms == 123.45

    def test_init_with_error(self):
        """Should handle error case."""
        ctx = StepContext(
            output=None,
            status_code=500,
            error="Internal Server Error",
        )
        
        assert ctx.output is None
        assert ctx.status_code == 500
        assert ctx.error == "Internal Server Error"

    def test_repr(self):
        """Should have readable repr."""
        ctx = StepContext(output={"test": 1}, status_code=200)
        repr_str = repr(ctx)
        
        assert "StepContext" in repr_str
        assert "dict" in repr_str
        assert "200" in repr_str


class TestTemplateContext:
    """Tests for TemplateContext."""

    def test_init_empty(self):
        """Should initialize with empty context."""
        ctx = TemplateContext()
        
        assert ctx.steps == {}
        assert ctx.inputs == {}
        assert ctx.env == {}

    def test_init_with_data(self):
        """Should initialize with provided data."""
        steps = {"step1": StepContext(output={"x": 1})}
        inputs = {"param": "value"}
        env = {"VAR": "test"}
        
        ctx = TemplateContext(steps=steps, inputs=inputs, env=env)
        
        assert ctx.steps == steps
        assert ctx.inputs == inputs
        assert ctx.env == env

    def test_to_dict(self):
        """Should convert to dict for Jinja2."""
        steps = {"step1": StepContext(output={"x": 1})}
        inputs = {"param": "value"}
        
        ctx = TemplateContext(steps=steps, inputs=inputs)
        result = ctx.to_dict()
        
        assert "steps" in result
        assert "inputs" in result
        assert "env" in result
        assert result["steps"] == steps
        assert result["inputs"] == inputs

    def test_from_workflow_execution(self):
        """Should create from workflow execution state."""
        step_outputs = {
            "step1": {"result": 42},
            "step2": {"items": [1, 2, 3]},
        }
        parameters = {"app_name": "test-app"}
        
        ctx = TemplateContext.from_workflow_execution(
            step_outputs=step_outputs,
            parameters=parameters,
        )
        
        assert len(ctx.steps) == 2
        assert ctx.steps["step1"].output == {"result": 42}
        assert ctx.steps["step2"].output == {"items": [1, 2, 3]}
        assert ctx.inputs == parameters

    def test_from_composite_execution(self):
        """Should create from composite endpoint execution."""
        step_results = {
            "step1": {
                "data": {"count": 10},
                "status_code": 200,
                "duration_ms": 50.5,
            },
            "step2": {
                "data": None,
                "status_code": 500,
                "error": "API Error",
                "duration_ms": 25.0,
            },
        }
        inputs = {"region": "us-west"}
        
        ctx = TemplateContext.from_composite_execution(
            step_results=step_results,
            inputs=inputs,
        )
        
        assert len(ctx.steps) == 2
        
        # Check step1
        assert ctx.steps["step1"].output == {"count": 10}
        assert ctx.steps["step1"].status_code == 200
        assert ctx.steps["step1"].duration_ms == 50.5
        
        # Check step2
        assert ctx.steps["step2"].output is None
        assert ctx.steps["step2"].status_code == 500
        assert ctx.steps["step2"].error == "API Error"
        
        assert ctx.inputs == inputs

    def test_repr(self):
        """Should have readable repr."""
        ctx = TemplateContext(
            steps={"step1": StepContext(output={})},
            inputs={"param1": "value"},
        )
        repr_str = repr(ctx)
        
        assert "TemplateContext" in repr_str
        assert "step1" in repr_str
        assert "param1" in repr_str

