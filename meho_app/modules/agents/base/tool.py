# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Base tool contract for all agent tools.

This module defines the abstract base class that all tools must implement.
Tools are the actions an agent can take to interact with external systems.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Generic, TypeVar

from pydantic import BaseModel

if TYPE_CHECKING:
    from meho_app.modules.agents.sse.emitter import EventEmitter

# Type variables for generic input/output
TInput = TypeVar("TInput", bound=BaseModel)
TOutput = TypeVar("TOutput", bound=BaseModel)


@dataclass
class BaseTool(ABC, Generic[TInput, TOutput]):
    """Abstract base class for all agent tools.

    Every tool MUST define:
        TOOL_NAME: Unique identifier used in LLM prompts
        TOOL_DESCRIPTION: Markdown description for LLM guidance
        InputSchema: Pydantic model for input validation
        OutputSchema: Pydantic model for output structure

    Every tool MUST implement:
        execute(): Async method that performs the tool's action

    Example:
        >>> @dataclass
        ... class MyTool(BaseTool[MyInput, MyOutput]):
        ...     TOOL_NAME = "my_tool"
        ...     TOOL_DESCRIPTION = "Does something useful."
        ...     InputSchema = MyInput
        ...     OutputSchema = MyOutput
        ...
        ...     async def execute(self, input, deps, emitter):
        ...         return MyOutput(result="done")
    """

    # Class attributes - MUST be defined by subclass
    TOOL_NAME: ClassVar[str]
    TOOL_DESCRIPTION: ClassVar[str]
    InputSchema: ClassVar[type[BaseModel]]
    OutputSchema: ClassVar[type[BaseModel]]

    @abstractmethod
    async def execute(
        self,
        tool_input: TInput,
        deps: Any,
        emitter: EventEmitter,
    ) -> TOutput:
        """Execute the tool with validated input.

        Args:
            tool_input: Validated Pydantic input model.
            deps: Agent dependencies (services, repositories, etc.).
            emitter: SSE event emitter for progress updates.

        Returns:
            Validated Pydantic output model.

        Raises:
            ToolExecutionError: If tool execution fails.
        """
        ...

    @classmethod
    def get_description_for_llm(cls) -> str:
        """Get formatted description for LLM system prompt.

        Returns:
            Markdown-formatted tool description with name and description.
        """
        return f"- **{cls.TOOL_NAME}**: {cls.TOOL_DESCRIPTION}"

    @classmethod
    def validate_input(cls, raw_args: dict[str, Any]) -> BaseModel:
        """Validate raw arguments against InputSchema.

        Args:
            raw_args: Dictionary of raw arguments from LLM.

        Returns:
            Validated Pydantic model instance.

        Raises:
            ValidationError: If arguments don't match schema.
        """
        return cls.InputSchema(**raw_args)

    @classmethod
    def get_input_schema_json(cls) -> dict[str, Any]:
        """Get JSON schema for the input.

        Returns:
            JSON schema dictionary for the InputSchema.
        """
        return cls.InputSchema.model_json_schema()

    @classmethod
    def get_output_schema_json(cls) -> dict[str, Any]:
        """Get JSON schema for the output.

        Returns:
            JSON schema dictionary for the OutputSchema.
        """
        return cls.OutputSchema.model_json_schema()
