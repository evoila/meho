"""Context objects for Jinja2 template rendering."""

from typing import Any, Dict, Optional


class StepContext:
    """Context for a single step's output.
    
    Provides dot-notation access to step results:
        {{ steps.step1.output.elements }}
        {{ steps.step1.status_code }}
    """

    def __init__(
        self,
        output: Any,
        status_code: Optional[int] = None,
        error: Optional[str] = None,
        duration_ms: Optional[float] = None,
    ):
        """Initialize step context.
        
        Args:
            output: The step's output data (typically a dict or list)
            status_code: HTTP status code (for API calls)
            error: Error message if step failed
            duration_ms: Execution duration in milliseconds
        """
        self.output = output
        self.status_code = status_code
        self.error = error
        self.duration_ms = duration_ms

    def __repr__(self) -> str:
        return f"StepContext(output={type(self.output).__name__}, status={self.status_code})"


class TemplateContext:
    """Complete context for template rendering.
    
    Provides access to:
    - steps: Step outputs from workflow/composite execution
    - inputs: User-provided parameters
    - env: Environment variables (read-only)
    
    Usage:
        context = TemplateContext(
            steps={"step1": StepContext(output={"result": 42})},
            inputs={"app_name": "my-app"}
        )
        
        # In template:
        {{ steps.step1.output.result }}  # → 42
        {{ inputs.app_name }}             # → "my-app"
    """

    def __init__(
        self,
        steps: Optional[Dict[str, StepContext]] = None,
        inputs: Optional[Dict[str, Any]] = None,
        env: Optional[Dict[str, str]] = None,
    ):
        """Initialize template context.
        
        Args:
            steps: Map of step_id → StepContext
            inputs: User-provided parameters
            env: Environment variables (read-only, optional)
        """
        self.steps = steps or {}
        self.inputs = inputs or {}
        self.env = env or {}

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for Jinja2 rendering.
        
        Returns:
            Dict with 'steps', 'inputs', and 'env' keys
        """
        return {
            "steps": self.steps,
            "inputs": self.inputs,
            "env": self.env,
        }

    @classmethod
    def from_workflow_execution(
        cls,
        step_outputs: Dict[str, Any],
        parameters: Dict[str, Any],
    ) -> "TemplateContext":
        """Create context from workflow execution state.
        
        Args:
            step_outputs: Map of step_id → raw output data
            parameters: Workflow input parameters
            
        Returns:
            TemplateContext ready for rendering
        """
        steps = {
            step_id: StepContext(output=output)
            for step_id, output in step_outputs.items()
        }
        return cls(steps=steps, inputs=parameters)

    @classmethod
    def from_composite_execution(
        cls,
        step_results: Dict[str, Dict[str, Any]],
        inputs: Dict[str, Any],
    ) -> "TemplateContext":
        """Create context from composite endpoint execution.
        
        Args:
            step_results: Map of step_id → full result dict (status, data, etc.)
            inputs: Composite endpoint input parameters
            
        Returns:
            TemplateContext ready for rendering
        """
        steps = {
            step_id: StepContext(
                output=result.get("data"),
                status_code=result.get("status_code"),
                error=result.get("error"),
                duration_ms=result.get("duration_ms"),
            )
            for step_id, result in step_results.items()
        }
        return cls(steps=steps, inputs=inputs)

    def __repr__(self) -> str:
        return (
            f"TemplateContext("
            f"steps={list(self.steps.keys())}, "
            f"inputs={list(self.inputs.keys())}"
            f")"
        )

