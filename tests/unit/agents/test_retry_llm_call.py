# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Tests for shared LLM retry logic (retry_llm_call)."""

from unittest.mock import AsyncMock, patch

import pytest

from meho_app.core.errors import LLMError
from meho_app.modules.agents.base.retry import retry_llm_call


def _make_http_error(status_code: int):
    """Create a mock ModelHTTPError with the given status code."""
    from pydantic_ai.exceptions import ModelHTTPError

    err = ModelHTTPError.__new__(ModelHTTPError)
    err.status_code = status_code
    err.model_name = "test-model"
    err.body = {"error": {"message": f"HTTP {status_code}"}}
    return err


def _make_api_error():
    """Create a mock ModelAPIError."""
    from pydantic_ai.exceptions import ModelAPIError

    err = ModelAPIError.__new__(ModelAPIError)
    return err


@pytest.mark.unit
class TestRetryLLMCall:
    """Tests for retry_llm_call function."""

    @pytest.mark.asyncio
    async def test_success_on_first_attempt(self):
        """Should return result on first successful call."""
        factory = AsyncMock(return_value="result")
        result = await retry_llm_call(factory)
        assert result == "result"
        factory.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("meho_app.modules.agents.base.retry.asyncio.sleep", new_callable=AsyncMock)
    async def test_retries_on_429_then_succeeds(self, mock_sleep):
        """Should retry on 429 and succeed on subsequent attempt."""
        factory = AsyncMock(side_effect=[_make_http_error(429), "success"])
        result = await retry_llm_call(factory, max_retries=3, base_delay=0.01)
        assert result == "success"
        assert factory.await_count == 2
        mock_sleep.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("meho_app.modules.agents.base.retry.asyncio.sleep", new_callable=AsyncMock)
    async def test_retries_on_500_then_succeeds(self, mock_sleep):
        """Should retry on 500 server error."""
        factory = AsyncMock(side_effect=[_make_http_error(500), "success"])
        result = await retry_llm_call(factory, max_retries=3, base_delay=0.01)
        assert result == "success"
        assert factory.await_count == 2

    @pytest.mark.asyncio
    @patch("meho_app.modules.agents.base.retry.asyncio.sleep", new_callable=AsyncMock)
    async def test_exhausts_retries_on_429_raises_llm_error(self, mock_sleep):
        """Should raise LLMError after exhausting retries on 429."""
        factory = AsyncMock(side_effect=[_make_http_error(429)] * 4)
        with pytest.raises(LLMError) as exc_info:
            await retry_llm_call(factory, max_retries=3, base_delay=0.01)

        assert exc_info.value.error_type == "rate_limit"
        assert exc_info.value.severity == "transient"
        assert factory.await_count == 4  # 1 initial + 3 retries

    @pytest.mark.asyncio
    async def test_no_retry_on_401(self):
        """Should not retry on 401 auth errors."""
        factory = AsyncMock(side_effect=_make_http_error(401))
        with pytest.raises(LLMError) as exc_info:
            await retry_llm_call(factory, max_retries=3)

        assert exc_info.value.error_type == "auth"
        assert exc_info.value.severity == "permanent"
        factory.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_retry_on_400(self):
        """Should not retry on 400 client errors."""
        factory = AsyncMock(side_effect=_make_http_error(400))
        with pytest.raises(LLMError) as exc_info:
            await retry_llm_call(factory, max_retries=3)

        assert exc_info.value.error_type == "unknown"
        assert exc_info.value.severity == "permanent"
        factory.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("meho_app.modules.agents.base.retry.asyncio.sleep", new_callable=AsyncMock)
    async def test_retries_on_model_api_error(self, mock_sleep):
        """Should retry on ModelAPIError (connection issues)."""
        factory = AsyncMock(side_effect=[_make_api_error(), "success"])
        result = await retry_llm_call(factory, max_retries=3, base_delay=0.01)
        assert result == "success"
        assert factory.await_count == 2

    @pytest.mark.asyncio
    @patch("meho_app.modules.agents.base.retry.asyncio.sleep", new_callable=AsyncMock)
    async def test_exhausts_retries_on_api_error_raises_llm_error(self, mock_sleep):
        """Should raise LLMError after exhausting retries on API error."""
        factory = AsyncMock(side_effect=[_make_api_error()] * 4)
        with pytest.raises(LLMError) as exc_info:
            await retry_llm_call(factory, max_retries=3, base_delay=0.01)

        assert exc_info.value.error_type == "connection"
        assert exc_info.value.severity == "transient"

    @pytest.mark.asyncio
    @patch("meho_app.modules.agents.base.retry.asyncio.sleep", new_callable=AsyncMock)
    async def test_exponential_backoff_delays(self, mock_sleep):
        """Should use exponential backoff with jitter."""
        factory = AsyncMock(side_effect=[_make_http_error(429), _make_http_error(429), "success"])
        await retry_llm_call(factory, max_retries=3, base_delay=1.0)

        assert mock_sleep.await_count == 2
        delays = [call.args[0] for call in mock_sleep.await_args_list]
        # First delay: base_delay * 2^0 * jitter (0.8-1.2) ~= 0.8-1.2
        assert 0.5 < delays[0] < 1.5
        # Second delay: base_delay * 2^1 * jitter (0.8-1.2) ~= 1.6-2.4
        assert 1.0 < delays[1] < 3.0
