# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Unit tests for SlackConnector.

Covers VALIDATION.md cases MSG-01a through MSG-01d:
- MSG-01a: Token validation on connect
- MSG-01b: get_channel_history operation
- MSG-01c: post_message with thread_ts
- MSG-01d: search_messages graceful degradation without user token

Uses sys.modules stubbing to avoid requiring slack_sdk installation.
"""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Stub slack_sdk and slack_bolt before importing connector.
# The connector does `from slack_sdk.web.async_client import AsyncWebClient`
# at call time (lazy import in connect()), so we control this via the
# _async_web_client_mod mock's AsyncWebClient attribute.
_slack_sdk_mock = MagicMock()
_async_web_client_mod = MagicMock()
sys.modules.setdefault("slack_sdk", _slack_sdk_mock)
sys.modules.setdefault("slack_sdk.web", MagicMock())
sys.modules.setdefault("slack_sdk.web.async_client", _async_web_client_mod)
# Provide a real SlackApiError class so isinstance() works in _parse_slack_error
_slack_errors_mod = MagicMock()


class _FakeSlackApiError(Exception):
    """Fake SlackApiError for testing isinstance checks."""

    def __init__(self, message="", response=None):
        super().__init__(message)
        self.response = response or {}


_slack_errors_mod.SlackApiError = _FakeSlackApiError
sys.modules["slack_sdk.errors"] = _slack_errors_mod
sys.modules.setdefault("slack_bolt", MagicMock())
sys.modules.setdefault("slack_bolt.async_app", MagicMock())
sys.modules.setdefault("slack_bolt.adapter", MagicMock())
sys.modules.setdefault("slack_bolt.adapter.socket_mode", MagicMock())
sys.modules.setdefault("slack_bolt.adapter.socket_mode.async_handler", MagicMock())

from meho_app.modules.connectors.slack.connector import SlackConnector


def _make_mock_client(**kwargs):
    """Create a mock AsyncWebClient with standard auth_test."""
    mock_client = AsyncMock()
    mock_client.auth_test = AsyncMock(
        return_value={"ok": True, "user": "meho-bot", "team": "TestTeam"}
    )
    for key, val in kwargs.items():
        setattr(mock_client, key, val)
    return mock_client


@pytest.fixture
def connector_with_bot_token():
    """Create a SlackConnector with a bot token only."""
    return SlackConnector(
        connector_id="test-connector-id",
        config={},
        credentials={"slack_bot_token": "xoxb-test-token"},
    )


@pytest.fixture
def connector_with_both_tokens():
    """Create a SlackConnector with bot and user tokens."""
    return SlackConnector(
        connector_id="test-connector-id",
        config={},
        credentials={
            "slack_bot_token": "xoxb-test-token",
            "slack_user_token": "xoxp-test-user-token",
        },
    )


@pytest.fixture
def connector_no_token():
    """Create a SlackConnector with no credentials."""
    return SlackConnector(
        connector_id="test-connector-id",
        config={},
        credentials={},
    )


async def _connect_with_mock(connector, mock_client=None, side_effect=None):
    """Helper to connect a connector with a mocked AsyncWebClient."""
    mod = sys.modules["slack_sdk.web.async_client"]
    if side_effect is not None:
        mod.AsyncWebClient = MagicMock(side_effect=side_effect)
    else:
        if mock_client is None:
            mock_client = _make_mock_client()
        mod.AsyncWebClient = MagicMock(return_value=mock_client)
    await connector.connect()
    return mock_client


# =========================================================================
# MSG-01a: Token validation on connect
# =========================================================================


@pytest.mark.asyncio
async def test_connect_validates_token(connector_with_bot_token):
    """MSG-01a: Verify connect validates token via auth_test."""
    mock_client = _make_mock_client()
    await _connect_with_mock(connector_with_bot_token, mock_client)

    assert connector_with_bot_token._is_connected is True
    mock_client.auth_test.assert_awaited_once()


@pytest.mark.asyncio
async def test_connect_missing_token(connector_no_token):
    """Verify connect raises ValueError when slack_bot_token is missing."""
    with pytest.raises(ValueError, match="slack_bot_token"):
        await connector_no_token.connect()


@pytest.mark.asyncio
async def test_connect_with_user_token_initializes_user_client(connector_with_both_tokens):
    """Verify connect initializes both bot and user clients when user token provided."""
    mock_client = _make_mock_client()
    await _connect_with_mock(connector_with_both_tokens, mock_client)

    assert connector_with_both_tokens._is_connected is True
    assert connector_with_both_tokens._user_client is not None


# =========================================================================
# MSG-01b: get_channel_history
# =========================================================================


@pytest.mark.asyncio
async def test_get_channel_history(connector_with_bot_token):
    """MSG-01b: Verify get_channel_history returns messages from conversations.history."""
    mock_client = _make_mock_client(
        conversations_history=AsyncMock(return_value={
            "messages": [
                {"user": "U123", "text": "hello", "ts": "123.456", "type": "message"},
                {"user": "U456", "text": "world", "ts": "789.012", "type": "message"},
            ],
            "has_more": False,
        }),
    )
    await _connect_with_mock(connector_with_bot_token, mock_client)

    result = await connector_with_bot_token.execute("get_channel_history", {"channel_id": "C123"})

    assert result.success is True
    assert len(result.data["messages"]) == 2
    assert result.data["messages"][0]["text"] == "hello"
    assert result.data["has_more"] is False
    mock_client.conversations_history.assert_awaited_once()


# =========================================================================
# MSG-01c: post_message with threaded reply
# =========================================================================


@pytest.mark.asyncio
async def test_post_message_threaded(connector_with_bot_token):
    """MSG-01c: Verify post_message passes thread_ts for threaded replies."""
    mock_client = _make_mock_client(
        chat_postMessage=AsyncMock(return_value={
            "ok": True,
            "ts": "789.012",
            "channel": "C123",
        }),
    )
    await _connect_with_mock(connector_with_bot_token, mock_client)

    result = await connector_with_bot_token.execute(
        "post_message",
        {"channel": "C123", "text": "test message", "thread_ts": "123.456"},
    )

    assert result.success is True
    assert result.data["ok"] is True
    assert result.data["ts"] == "789.012"

    # Verify thread_ts was passed to chat_postMessage
    call_kwargs = mock_client.chat_postMessage.call_args[1]
    assert call_kwargs["thread_ts"] == "123.456"


# =========================================================================
# MSG-01d: search_messages graceful degradation
# =========================================================================


@pytest.mark.asyncio
async def test_search_messages_no_user_token(connector_with_bot_token):
    """MSG-01d: Verify search_messages returns warning when no user token."""
    mock_client = _make_mock_client()
    await _connect_with_mock(connector_with_bot_token, mock_client)

    result = await connector_with_bot_token.execute("search_messages", {"query": "test"})

    assert result.success is True
    assert "warning" in result.data
    assert "user token" in result.data["warning"].lower()
    assert result.data["matches"] == []
    assert result.data["total"] == 0


@pytest.mark.asyncio
async def test_search_messages_with_user_token(connector_with_both_tokens):
    """Verify search_messages works when user token is available."""
    mock_bot_client = _make_mock_client()
    mock_user_client = AsyncMock()
    mock_user_client.search_messages = AsyncMock(return_value={
        "messages": {
            "matches": [
                {
                    "text": "found message",
                    "ts": "111.222",
                    "user": "U789",
                    "channel": {"name": "general", "id": "C001"},
                },
            ],
            "total": 1,
        }
    })

    # side_effect returns bot_client on first call, user_client on second
    await _connect_with_mock(
        connector_with_both_tokens,
        side_effect=[mock_bot_client, mock_user_client],
    )

    result = await connector_with_both_tokens.execute("search_messages", {"query": "test"})

    assert result.success is True
    assert len(result.data["matches"]) == 1
    assert result.data["matches"][0]["text"] == "found message"
    mock_user_client.search_messages.assert_awaited_once_with(query="test")


# =========================================================================
# Additional operations
# =========================================================================


@pytest.mark.asyncio
async def test_list_channels(connector_with_bot_token):
    """Verify list_channels returns channel list from conversations.list."""
    mock_client = _make_mock_client(
        conversations_list=AsyncMock(return_value={
            "channels": [
                {"id": "C123", "name": "general", "num_members": 42, "is_private": False},
            ],
        }),
    )
    await _connect_with_mock(connector_with_bot_token, mock_client)

    result = await connector_with_bot_token.execute("list_channels", {})

    assert result.success is True
    assert len(result.data["channels"]) == 1
    assert result.data["channels"][0]["name"] == "general"


@pytest.mark.asyncio
async def test_add_reaction(connector_with_bot_token):
    """Verify add_reaction calls reactions.add with correct parameters."""
    mock_client = _make_mock_client(
        reactions_add=AsyncMock(return_value={"ok": True}),
    )
    await _connect_with_mock(connector_with_bot_token, mock_client)

    result = await connector_with_bot_token.execute(
        "add_reaction",
        {"channel": "C123", "timestamp": "123.456", "name": "eyes"},
    )

    assert result.success is True
    assert result.data["ok"] is True
    mock_client.reactions_add.assert_awaited_once_with(
        channel="C123", timestamp="123.456", name="eyes"
    )


@pytest.mark.asyncio
async def test_unknown_operation(connector_with_bot_token):
    """Verify unknown operation returns error."""
    mock_client = _make_mock_client()
    await _connect_with_mock(connector_with_bot_token, mock_client)

    result = await connector_with_bot_token.execute("nonexistent_op", {})

    assert result.success is False
    assert "Unknown operation" in result.error
