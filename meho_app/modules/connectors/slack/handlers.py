# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Slack Operation Handlers.

Standalone async functions that implement each Slack operation.
Each handler accepts the Slack client(s) and parameters, returning
a dict that the connector wraps in an OperationResult.

The search_messages handler requires an optional user_client (xoxp-* token)
since Slack's search.messages API only works with user tokens, not bot tokens.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from slack_sdk.web.async_client import AsyncWebClient


async def _handle_get_channel_history(
    client: AsyncWebClient,
    params: dict[str, Any],
) -> dict[str, Any]:
    """
    Fetch channel message history via conversations.history.

    Args:
        client: Slack bot client (AsyncWebClient).
        params: Must include channel_id. Optional: oldest, latest, limit.

    Returns:
        Dict with messages list and has_more flag.
    """
    kwargs: dict[str, Any] = {"channel": params["channel_id"]}

    if "oldest" in params:
        kwargs["oldest"] = str(params["oldest"])
    if "latest" in params:
        kwargs["latest"] = str(params["latest"])
    if "limit" in params:
        kwargs["limit"] = min(int(params["limit"]), 999)  # Slack max is 999

    response = await client.conversations_history(**kwargs)
    messages: list[dict[str, Any]] = response.get("messages", [])

    return {
        "messages": [
            {
                "user": m.get("user"),
                "text": m.get("text"),
                "ts": m.get("ts"),
                "thread_ts": m.get("thread_ts"),
                "type": m.get("type"),
            }
            for m in messages
        ],
        "has_more": response.get("has_more", False),
        "channel_id": params["channel_id"],
    }


async def _handle_search_messages(
    client: AsyncWebClient,  # noqa: ARG001
    user_client: AsyncWebClient | None,
    params: dict[str, Any],
) -> dict[str, Any]:
    """
    Search messages across channels via search.messages.

    Requires a user token (xoxp-*). If only a bot token is available,
    returns an actionable error message instead of raising.

    Args:
        client: Slack bot client (unused, kept for signature consistency).
        user_client: Slack user client (xoxp-* token), or None.
        params: Must include query string.

    Returns:
        Dict with search results or error guidance.
    """
    if user_client is None:
        return {
            "warning": (
                "search_messages requires a user token (xoxp-*). "
                "Add a slack_user_token credential to this connector to enable search. "
                "Alternative: use get_channel_history for targeted searches in specific channels."
            ),
            "matches": [],
            "total": 0,
        }

    query = params["query"]
    response = await user_client.search_messages(query=query)

    messages_data: dict[str, Any] = response.get("messages", {})
    matches = messages_data.get("matches", [])

    return {
        "matches": [
            {
                "text": m.get("text"),
                "ts": m.get("ts"),
                "channel": m.get("channel", {}).get("name")
                if isinstance(m.get("channel"), dict)
                else m.get("channel"),
                "channel_id": m.get("channel", {}).get("id")
                if isinstance(m.get("channel"), dict)
                else None,
                "user": m.get("user"),
                "permalink": m.get("permalink"),
            }
            for m in matches
        ],
        "total": messages_data.get("total", len(matches)),
        "query": query,
    }


async def _handle_list_channels(
    client: AsyncWebClient,
    params: dict[str, Any],
) -> dict[str, Any]:
    """
    List accessible channels via conversations.list.

    Args:
        client: Slack bot client.
        params: Optional types filter (default: public_channel,private_channel).

    Returns:
        Dict with channels list.
    """
    types = params.get("types", "public_channel,private_channel")
    response = await client.conversations_list(types=types)

    channels: list[dict[str, Any]] = response.get("channels", [])

    return {
        "channels": [
            {
                "id": ch.get("id"),
                "name": ch.get("name"),
                "topic": ch.get("topic", {}).get("value", "")
                if isinstance(ch.get("topic"), dict)
                else "",
                "purpose": ch.get("purpose", {}).get("value", "")
                if isinstance(ch.get("purpose"), dict)
                else "",
                "num_members": ch.get("num_members", 0),
                "is_private": ch.get("is_private", False),
            }
            for ch in channels
        ],
    }


async def _handle_get_user_info(
    client: AsyncWebClient,
    params: dict[str, Any],
) -> dict[str, Any]:
    """
    Get user profile information via users.info.

    Args:
        client: Slack bot client.
        params: Must include user_id.

    Returns:
        Dict with user profile data.
    """
    response = await client.users_info(user=params["user_id"])

    user: dict[str, Any] = response.get("user", {})
    profile = user.get("profile", {})

    return {
        "id": user.get("id"),
        "real_name": user.get("real_name", ""),
        "display_name": profile.get("display_name", ""),
        "email": profile.get("email", ""),
        "tz": user.get("tz", ""),
        "status_text": profile.get("status_text", ""),
        "is_bot": user.get("is_bot", False),
        "deleted": user.get("deleted", False),
    }


async def _handle_post_message(
    client: AsyncWebClient,
    params: dict[str, Any],
) -> dict[str, Any]:
    """
    Post a message to a channel via chat.postMessage.

    Supports threaded replies via thread_ts parameter.

    Args:
        client: Slack bot client.
        params: Must include channel and text. Optional: thread_ts.

    Returns:
        Dict with posted message confirmation.
    """
    kwargs: dict[str, Any] = {
        "channel": params["channel"],
        "text": params["text"],
    }
    if "thread_ts" in params:
        kwargs["thread_ts"] = params["thread_ts"]

    response = await client.chat_postMessage(**kwargs)

    return {
        "ok": response.get("ok"),
        "ts": response.get("ts"),
        "channel": response.get("channel"),
    }


async def _handle_add_reaction(
    client: AsyncWebClient,
    params: dict[str, Any],
) -> dict[str, Any]:
    """
    Add an emoji reaction to a message via reactions.add.

    Args:
        client: Slack bot client.
        params: Must include channel, timestamp, and name (emoji without colons).

    Returns:
        Dict with reaction confirmation.
    """
    response = await client.reactions_add(
        channel=params["channel"],
        timestamp=params["timestamp"],
        name=params["name"],
    )

    return {
        "ok": response.get("ok"),
    }
