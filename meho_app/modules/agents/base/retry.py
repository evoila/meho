# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Shared retry logic for LLM calls with exponential backoff."""

import asyncio
import random
from collections.abc import Callable
from typing import Any, TypeVar

from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError

from meho_app.core.errors import LLMError
from meho_app.core.otel import get_logger

T = TypeVar("T")

logger = get_logger(__name__)


def _get_provider_name() -> str:
    """Return human-readable provider name from config."""
    try:
        from meho_app.core.config import get_config

        provider = get_config().llm_provider
        return {"anthropic": "Anthropic Claude", "openai": "OpenAI", "ollama": "Ollama"}.get(
            provider, provider
        )
    except Exception:
        return "LLM"  # Fallback if config not available


def _get_api_key_hint() -> str:
    """Return provider-specific API key env var name."""
    try:
        from meho_app.core.config import get_config

        provider = get_config().llm_provider
        return {
            "anthropic": "ANTHROPIC_API_KEY",
            "openai": "OPENAI_API_KEY",
            "ollama": "OLLAMA_BASE_URL",
        }.get(provider, "API key")
    except Exception:
        return "API key"  # Fallback


async def retry_llm_call(  # NOSONAR (cognitive complexity)
    coro_factory: Callable,
    max_retries: int = 3,
    base_delay: float = 1.0,
    logger_ref: Any = None,
) -> T:
    """Retry LLM calls with exponential backoff for transient errors.

    Retries on 429 (rate limit), 500, 502, 503, 529 status codes.
    No retry for auth failures (401) or other permanent errors.

    Args:
        coro_factory: Callable that returns a new coroutine each time.
        max_retries: Maximum number of retry attempts.
        base_delay: Base delay in seconds (doubled each attempt with jitter).
        logger_ref: Logger instance for retry messages.
    """
    _logger = logger_ref or logger

    for attempt in range(max_retries + 1):
        try:
            result: T = await coro_factory()
            return result
        except ModelHTTPError as e:
            if e.status_code in (429, 500, 502, 503, 529) and attempt < max_retries:
                delay = base_delay * (2**attempt) * (0.8 + random.random() * 0.4)  # noqa: S311 -- non-cryptographic context, random OK
                _logger.warning(
                    f"LLM call failed (HTTP {e.status_code}), retrying in {delay:.1f}s "
                    f"(attempt {attempt + 1}/{max_retries})"
                )
                await asyncio.sleep(delay)
                continue
            if e.status_code == 429:
                raise LLMError(
                    "rate_limit",
                    "transient",
                    f"{_get_provider_name()} is rate limited -- all retry attempts exhausted",
                    remediation="Wait a minute and try again",
                ) from e
            elif e.status_code == 401:
                raise LLMError(
                    "auth",
                    "permanent",
                    f"{_get_provider_name()} authentication failed",
                    remediation=f"Check {_get_api_key_hint()} environment variable",
                ) from e
            elif e.status_code >= 500:
                raise LLMError(
                    "connection",
                    "transient",
                    f"{_get_provider_name()} returned server error ({e.status_code})",
                    remediation=f"{_get_provider_name()} may be experiencing issues -- try again shortly",
                ) from e
            else:
                raise LLMError(
                    "unknown",
                    "permanent",
                    f"{_get_provider_name()} error (HTTP {e.status_code})",
                    remediation="Check API configuration",
                ) from e
        except ModelAPIError as e:
            if attempt < max_retries:
                delay = base_delay * (2**attempt) * (0.8 + random.random() * 0.4)  # noqa: S311 -- non-cryptographic context, random OK
                _logger.warning(
                    f"LLM connection error, retrying in {delay:.1f}s "
                    f"(attempt {attempt + 1}/{max_retries})"
                )
                await asyncio.sleep(delay)
                continue
            raise LLMError(
                "connection",
                "transient",
                f"Cannot reach {_get_provider_name()} API",
                remediation="Check network connectivity and API configuration",
            ) from e

    # Unreachable: the loop always returns or raises, but mypy needs this
    raise LLMError(
        "unknown",
        "transient",
        f"All {max_retries} retry attempts exhausted",
        remediation="Check API configuration and try again",
    )
