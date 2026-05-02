# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
E2E test fixtures.

Provides helpers for tests that need real services and real LLM calls.
Inherits root conftest env setup + ALLOW_MODEL_REQUESTS=False guard.

Use the `allow_real_llm` fixture or `override_allow_model_requests(True)`
context manager to opt-in to real LLM calls in specific tests.
"""

import os

import pytest
from pydantic_ai.models import override_allow_model_requests


# ============================================================================
# LLM Override Helpers
# ============================================================================


@pytest.fixture
def allow_real_llm():
    """
    Fixture that enables real LLM API calls for the duration of a test.

    Usage:
        async def test_real_agent(allow_real_llm):
            with allow_real_llm:
                # Real LLM calls are allowed here
                result = await agent.run(...)
    """
    return override_allow_model_requests(True)


# ============================================================================
# Skip Helpers
# ============================================================================

requires_anthropic_key = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="Real LLM test requires ANTHROPIC_API_KEY environment variable",
)

# Custom pytest mark for real LLM tests (can be used to filter: pytest -m real_llm)
real_llm = pytest.mark.real_llm
