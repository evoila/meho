# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Quick LLM inference utility for one-shot calls.

Use this for simple LLM tasks that don't need the full agent loop:
- Quick classification decisions
- Summary generation
- Data extraction
- Yes/no questions

Example:
    >>> from meho_app.modules.agents.base.inference import infer
    >>>
    >>> # Simple string response
    >>> summary = await infer(
    ...     system_prompt="Summarize in one sentence.",
    ...     message="Long text here...",
    ... )
    >>> print(summary)  # "This is a summary."
    >>>
    >>> # Structured output
    >>> class Decision(BaseModel):
    ...     answer: bool
    ...     confidence: float
    ...     reason: str
    >>>
    >>> result = await infer(
    ...     system_prompt="Is this a Kubernetes-related question?",
    ...     message="How do I restart a pod?",
    ...     output_schema=Decision,
    ... )
    >>> print(result.answer)  # True
    >>> print(result.confidence)  # 0.95
    >>>
    >>> # With detailed result for observability
    >>> response, details = await infer_with_details(
    ...     system_prompt="Summarize this.",
    ...     message="Long text...",
    ... )
    >>> print(details.prompt_tokens)  # 150
    >>> print(details.completion_tokens)  # 50
"""

from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass
from datetime import UTC
from typing import TYPE_CHECKING, Any, TypeVar, overload

from pydantic import BaseModel

# Note: truncate_payload import removed - we store full data for observability

if TYPE_CHECKING:
    from pydantic_ai import Agent

# Type variable for structured output
T = TypeVar("T", bound=BaseModel)


@dataclass
class InferenceDetails:
    """Detailed information about an LLM inference call.

    Used for deep observability and transcript persistence.
    """

    system_prompt: str
    message: str
    response: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    duration_ms: float = 0.0
    estimated_cost_usd: float | None = None


# Module-level agent (lazy-loaded)
_inference_agent: Agent[None, str] | None = None


def _get_agent() -> Agent[None, str]:
    """Get or create the inference agent (lazy singleton).

    Configures Anthropic-specific settings (prompt caching) when the
    default model is Anthropic. Non-Anthropic models get no special settings.

    Returns:
        PydanticAI Agent instance configured with default model.
    """
    global _inference_agent

    if _inference_agent is None:
        from pydantic_ai import Agent, InstrumentationSettings

        from meho_app.core.config import get_config
        from meho_app.modules.agents.config.models import ModelConfig

        config = get_config()
        model_config = ModelConfig(name=config.llm_model)

        # Use centralized model settings factory (Phase 82)
        from meho_app.modules.agents.agent_factories import get_model_settings

        model_settings = get_model_settings(task_type="inference")

        _inference_agent = Agent(
            model_config.name,
            model_settings=model_settings,
            instrument=InstrumentationSettings(),
        )

    return _inference_agent


def reset_agent() -> None:
    """Reset the cached agent (useful for testing)."""
    global _inference_agent
    _inference_agent = None


@overload
async def infer(
    system_prompt: str,
    message: str,
    output_schema: None = None,
    *,
    model: str | None = None,
    temperature: float | None = None,
    timeout: float | None = None,  # noqa: ASYNC109 -- timeout handled at caller level
    max_tokens: int | None = None,
) -> str: ...


@overload
async def infer(
    system_prompt: str,
    message: str,
    output_schema: type[T],
    *,
    model: str | None = None,
    temperature: float | None = None,
    timeout: float | None = None,  # noqa: ASYNC109 -- timeout handled at caller level
    max_tokens: int | None = None,
) -> T: ...


async def infer(
    system_prompt: str,
    message: str,
    output_schema: type[T] | None = None,
    *,
    model: str | None = None,
    temperature: float | None = None,
    timeout: float | None = None,  # noqa: ASYNC109 -- timeout handled at caller level
    max_tokens: int | None = None,
) -> str | T:
    """Quick one-shot LLM inference.

    Stateless, no history, minimal overhead. Use for:
    - Quick classification decisions
    - Summary generation
    - Data extraction
    - Simple Q&A

    Args:
        system_prompt: Instructions for the LLM.
        message: The user message / content to process.
        output_schema: Optional Pydantic model for structured output.
            If None, returns plain string.
        model: Optional model override (default: config.llm_model).
        temperature: Optional temperature override.
        timeout: Optional timeout override in seconds (default: config.llm_inference_timeout).
        max_tokens: Optional max output tokens. When None, no cap is applied.

    Returns:
        If output_schema is None: str response
        If output_schema is provided: validated Pydantic model instance

    Example:
        >>> # String output
        >>> answer = await infer(
        ...     "Answer yes or no.",
        ...     "Is Python a programming language?"
        ... )
        >>> # answer = "Yes"

        >>> # Structured output
        >>> class Sentiment(BaseModel):
        ...     score: float  # -1 to 1
        ...     label: str    # positive/negative/neutral
        >>>
        >>> result = await infer(
        ...     "Analyze sentiment.",
        ...     "I love this product!",
        ...     Sentiment
        ... )
        >>> # result.score = 0.9, result.label = "positive"
    """
    from pydantic_ai import Agent, InstrumentationSettings

    from meho_app.core.config import get_config

    config = get_config()
    effective_timeout = timeout if timeout is not None else config.llm_inference_timeout

    # Build model settings
    model_settings: dict[str, Any] = {}
    if temperature is not None:
        model_settings["temperature"] = temperature
    if max_tokens is not None:
        model_settings["max_tokens"] = max_tokens

    # Use custom model if specified, otherwise default agent
    _instrument = InstrumentationSettings()

    if model is not None:
        if output_schema is not None:
            agent: Agent[None, T] = Agent(
                model,
                output_type=output_schema,
                instrument=_instrument,
            )
        else:
            agent = Agent(model, instrument=_instrument)
    else:
        agent = _get_agent()

        # If structured output needed, create typed agent
        if output_schema is not None:
            agent = Agent(
                agent.model,  # type: ignore[arg-type]
                output_type=output_schema,
                instrument=_instrument,
            )

    # Run inference with timeout
    import asyncio

    try:
        result = await asyncio.wait_for(
            agent.run(
                message,
                instructions=system_prompt,
                model_settings=model_settings if model_settings else None,
            ),
            timeout=effective_timeout,
        )
    except TimeoutError:
        raise TimeoutError(f"LLM inference timed out after {effective_timeout} seconds") from None

    # Extract output from PydanticAI v1.63+ result
    return result.output


# Convenience aliases
quick_llm = infer
one_shot = infer


async def infer_with_details(
    system_prompt: str,
    message: str,
    *,
    model: str | None = None,
    temperature: float | None = None,
    timeout: float | None = None,  # noqa: ASYNC109 -- timeout handled at caller level
    max_tokens: int | None = None,
) -> tuple[str, InferenceDetails]:
    """LLM inference with detailed metrics for observability.

    This is like infer() but returns additional details about the call
    including token usage, duration, and cost estimate.

    Args:
        system_prompt: Instructions for the LLM.
        message: The user message / content to process.
        model: Optional model override (default: config.llm_model).
        temperature: Optional temperature override.
        timeout: Optional timeout override in seconds (default: config.llm_inference_timeout).
        max_tokens: Optional max output tokens. When None, no cap is applied.

    Returns:
        Tuple of (response string, InferenceDetails with metrics).

    Example:
        >>> response, details = await infer_with_details(
        ...     "Summarize this text.",
        ...     "Long text here...",
        ... )
        >>> print(response)  # "This is a summary."
        >>> print(details.total_tokens)  # 200
        >>> print(details.duration_ms)  # 1234.5
    """
    from pydantic_ai import Agent, InstrumentationSettings

    from meho_app.core.config import get_config
    from meho_app.modules.agents.base.detailed_events import estimate_cost

    config = get_config()
    model_name = model or config.llm_model
    effective_timeout = timeout if timeout is not None else config.llm_inference_timeout

    # Build model settings
    model_settings: dict[str, Any] = {}
    if temperature is not None:
        model_settings["temperature"] = temperature
    if max_tokens is not None:
        model_settings["max_tokens"] = max_tokens

    # Create agent
    agent: Agent[None, str] = Agent(model_name, instrument=InstrumentationSettings())

    # Run inference with timing
    start_time = time.perf_counter()

    import asyncio

    try:
        result = await asyncio.wait_for(
            agent.run(
                message,
                instructions=system_prompt,
                model_settings=model_settings if model_settings else None,
            ),
            timeout=effective_timeout,
        )
    except TimeoutError:
        raise TimeoutError(f"LLM inference timed out after {effective_timeout} seconds") from None

    duration_ms = (time.perf_counter() - start_time) * 1000

    # Extract output from PydanticAI v1.63+ result
    response = result.output

    # Extract token usage from PydanticAI result
    prompt_tokens = 0
    completion_tokens = 0
    total_tokens = 0

    # PydanticAI stores usage in result._usage or result.usage()
    if hasattr(result, "usage"):
        try:
            usage = result.usage()
            if usage:
                prompt_tokens = getattr(usage, "request_tokens", 0) or 0
                completion_tokens = getattr(usage, "response_tokens", 0) or 0
                total_tokens = (
                    getattr(usage, "total_tokens", 0) or prompt_tokens + completion_tokens
                )
        except Exception:  # noqa: S110 -- intentional silent exception handling
            pass

    # Estimate cost
    cost = estimate_cost(model_name, prompt_tokens, completion_tokens)

    details = InferenceDetails(
        system_prompt=system_prompt,
        message=message,
        response=response,
        model=model_name,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        duration_ms=duration_ms,
        estimated_cost_usd=cost,
    )

    return response, details


async def infer_structured(
    prompt: str,
    response_model: type[T],
    *,
    model: str | None = None,
    temperature: float | None = None,
    instructions: str | None = None,
    timeout: float | None = None,  # noqa: ASYNC109 -- timeout handled at caller level
    max_tokens: int | None = None,
) -> T:
    """Convenience wrapper for structured output inference.

    Args:
        prompt: The full prompt (combined system + message).
        response_model: Pydantic model for the structured response.
        model: Optional model override.
        temperature: Optional temperature override.
        instructions: Optional system instructions override. If provided,
            replaces the default "Respond with valid JSON..." instruction.
            Useful for passing domain-specific system prompts (e.g. JSONFlux
            SQL generation prompt) while keeping the user prompt separate.
        timeout: Optional timeout override in seconds (default: config.llm_inference_timeout).
        max_tokens: Optional max output tokens. When None, no cap is applied.

    Returns:
        Validated Pydantic model instance.

    Example:
        >>> class SearchIntent(BaseModel):
        ...     query: str
        ...     reasoning: str
        >>>
        >>> intent = await infer_structured(
        ...     "What operation should we search for to list pods?",
        ...     SearchIntent
        ... )
        >>> print(intent.query)  # "list pods"
    """
    from pydantic_ai import Agent, InstrumentationSettings

    from meho_app.core.config import get_config
    from meho_app.modules.agents.base.detailed_events import (
        DetailedEvent,
        EventDetails,
        TokenUsage,
        estimate_cost,
        serialize_pydantic_messages,
    )
    from meho_app.modules.agents.persistence.event_context import (
        get_transcript_collector,
    )

    config = get_config()
    model_name = model or config.llm_model
    effective_timeout = timeout if timeout is not None else config.llm_inference_timeout

    # Build model settings
    model_settings: dict[str, Any] = {}
    if temperature is not None:
        model_settings["temperature"] = temperature
    if max_tokens is not None:
        model_settings["max_tokens"] = max_tokens

    # Create agent with structured output
    agent: Agent[None, T] = Agent(
        model_name,
        output_type=response_model,
        instrument=InstrumentationSettings(),
    )

    # Run inference with timing
    start_time = time.perf_counter()

    import asyncio
    from datetime import datetime
    from uuid import uuid4

    effective_instructions = (
        instructions
        if instructions is not None
        else "Respond with valid JSON matching the required schema."
    )

    try:
        result = await asyncio.wait_for(
            agent.run(
                prompt,
                instructions=effective_instructions,
                model_settings=model_settings if model_settings else None,
            ),
            timeout=effective_timeout,
        )
    except TimeoutError:
        raise TimeoutError(f"LLM inference timed out after {effective_timeout} seconds") from None

    duration_ms = (time.perf_counter() - start_time) * 1000

    # Extract output from PydanticAI v1.63+ result
    output = result.output

    # Extract token usage from PydanticAI result
    prompt_tokens = 0
    completion_tokens = 0
    total_tokens = 0

    if hasattr(result, "usage"):
        try:
            usage = result.usage()
            if usage:
                prompt_tokens = getattr(usage, "request_tokens", 0) or 0
                completion_tokens = getattr(usage, "response_tokens", 0) or 0
                total_tokens = (
                    getattr(usage, "total_tokens", 0) or prompt_tokens + completion_tokens
                )
        except Exception:  # noqa: S110 -- intentional silent exception handling
            pass

    # Emit LLM event to transcript collector if available
    collector = get_transcript_collector()
    if collector:
        cost = estimate_cost(model_name, prompt_tokens, completion_tokens)

        # Serialize messages from PydanticAI result
        messages = None
        if hasattr(result, "new_messages"):
            with contextlib.suppress(Exception):
                messages = serialize_pydantic_messages(result.new_messages())

        # Get parsed/structured output
        parsed = None
        if hasattr(output, "model_dump"):
            with contextlib.suppress(Exception):
                parsed = output.model_dump()

        # Convert output to string for response field
        response_str = str(output.model_dump()) if hasattr(output, "model_dump") else str(output)

        event = DetailedEvent(
            id=str(uuid4()),
            timestamp=datetime.now(tz=UTC),  # Use naive UTC for database compatibility
            type="llm_call",
            summary=f"LLM call to {model_name} ({response_model.__name__})",
            details=EventDetails(
                llm_prompt=prompt,  # Full prompt, no truncation
                llm_response=response_str,  # Full response, no truncation
                llm_messages=messages,  # Full messages array
                llm_parsed=parsed,  # Structured output
                token_usage=TokenUsage(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                    estimated_cost_usd=cost,
                ),
                llm_duration_ms=duration_ms,
                model=model_name,
            ),
        )
        await collector.add(event)

    return output  # type: ignore[return-value]
