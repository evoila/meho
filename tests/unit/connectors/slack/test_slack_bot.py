# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Unit tests for SlackBot slash command handling.

Covers VALIDATION.md cases MSG-01e and MSG-01g:
- MSG-01e: Slash command ack behavior (empty text, with prompt)
- MSG-01g: Feature flag gating
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Stub slack_sdk and slack_bolt before importing bot
sys.modules.setdefault("slack_sdk", MagicMock())
sys.modules.setdefault("slack_sdk.web", MagicMock())
sys.modules.setdefault("slack_sdk.web.async_client", MagicMock())
sys.modules.setdefault("slack_sdk.errors", MagicMock())
sys.modules.setdefault("slack_bolt", MagicMock())
sys.modules.setdefault("slack_bolt.async_app", MagicMock())
sys.modules.setdefault("slack_bolt.adapter", MagicMock())
sys.modules.setdefault("slack_bolt.adapter.socket_mode", MagicMock())
sys.modules.setdefault("slack_bolt.adapter.socket_mode.async_handler", MagicMock())

from meho_app.modules.connectors.slack.bot import SlackBot


@pytest.fixture
def slack_bot():
    """Create a SlackBot instance for testing."""
    return SlackBot(
        bot_token="xoxb-test-token",
        app_token="xapp-test-token",
        connector_id="test-connector-id",
        tenant_id="test-tenant-id",
    )


# =========================================================================
# MSG-01e: Slash command ack behavior
# =========================================================================


@pytest.mark.asyncio
async def test_slash_command_empty_text(slack_bot):
    """MSG-01e: Verify /meho with empty text returns usage message."""
    ack = AsyncMock()
    client = AsyncMock()
    respond = AsyncMock()
    body = {"text": "", "channel_id": "C1", "user_id": "U1"}

    await slack_bot._handle_meho_command(ack=ack, body=body, client=client, respond=respond)

    ack.assert_awaited_once()
    ack_arg = ack.call_args[0][0]
    assert "Usage" in ack_arg or "usage" in ack_arg.lower()

    # Should NOT post a channel message or create a task
    client.chat_postMessage.assert_not_awaited()


@pytest.mark.asyncio
async def test_slash_command_with_prompt(slack_bot):
    """MSG-01e: Verify /meho with prompt acks and starts investigation."""
    ack = AsyncMock()
    client = AsyncMock()
    client.chat_postMessage = AsyncMock(return_value={"ts": "999.888", "channel": "C1"})
    respond = AsyncMock()
    body = {
        "text": "investigate high latency",
        "channel_id": "C1",
        "user_id": "U1",
        "user_name": "testuser",
    }

    with patch("asyncio.create_task") as mock_create_task:
        await slack_bot._handle_meho_command(ack=ack, body=body, client=client, respond=respond)

    # ack should be called with investigation start message
    ack.assert_awaited_once()
    ack_arg = ack.call_args[0][0]
    assert "investigation" in ack_arg.lower() or "starting" in ack_arg.lower()

    # Should post a visible channel message for threading
    client.chat_postMessage.assert_awaited_once()
    post_kwargs = client.chat_postMessage.call_args[1]
    assert post_kwargs["channel"] == "C1"
    assert "high latency" in post_kwargs["text"].lower() or "investigate" in post_kwargs["text"].lower()

    # Should dispatch investigation as async task
    mock_create_task.assert_called_once()


@pytest.mark.asyncio
async def test_slash_command_whitespace_only(slack_bot):
    """Verify /meho with only whitespace text returns usage message."""
    ack = AsyncMock()
    client = AsyncMock()
    respond = AsyncMock()
    body = {"text": "   ", "channel_id": "C1", "user_id": "U1"}

    await slack_bot._handle_meho_command(ack=ack, body=body, client=client, respond=respond)

    ack.assert_awaited_once()
    ack_arg = ack.call_args[0][0]
    assert "Usage" in ack_arg or "usage" in ack_arg.lower()


# =========================================================================
# MSG-01g: Feature flag gating
# =========================================================================


def test_feature_flag_disabled():
    """MSG-01g: Verify FeatureFlags(slack=False) correctly reports disabled."""
    from meho_app.core.feature_flags import FeatureFlags

    flags = FeatureFlags(slack=False)
    assert flags.slack is False


def test_feature_flag_enabled_by_default():
    """Verify FeatureFlags defaults to slack=True."""
    from meho_app.core.feature_flags import FeatureFlags

    flags = FeatureFlags()
    assert flags.slack is True
