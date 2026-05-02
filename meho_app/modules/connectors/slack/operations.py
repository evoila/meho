# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Slack Operation Definitions.

Defines all 6 Slack operations for agent discovery via hybrid search.
Operations cover channel history, message search, channel listing,
user info, message posting, and emoji reactions.
"""

from meho_app.modules.connectors.base import OperationDefinition

SLACK_OPERATIONS_VERSION = "2026.03.27.1"

SLACK_OPERATIONS: list[OperationDefinition] = [
    # Read Operations
    OperationDefinition(
        operation_id="get_channel_history",
        name="Get Channel History",
        description=(
            "Fetch message history from a Slack channel. Returns messages with user IDs, "
            "timestamps, thread info, and text content. Use list_channels first to find "
            "channel IDs. Supports time-range filtering via oldest/latest epoch timestamps."
        ),
        category="messaging",
        parameters=[
            {
                "name": "channel_id",
                "type": "string",
                "required": True,
                "description": "Slack channel ID (e.g., C01234567). Use list_channels to find IDs.",
            },
            {
                "name": "oldest",
                "type": "string",
                "required": False,
                "description": "Only messages after this Unix epoch timestamp (e.g., '1234567890.123456')",
            },
            {
                "name": "latest",
                "type": "string",
                "required": False,
                "description": "Only messages before this Unix epoch timestamp",
            },
            {
                "name": "limit",
                "type": "integer",
                "required": False,
                "description": "Maximum number of messages to return (default 100, max 999)",
            },
        ],
        example="get_channel_history channel_id=C01234567 limit=50",
        response_entity_type="SlackMessage",
        response_identifier_field="ts",
        response_display_name_field="text",
    ),
    OperationDefinition(
        operation_id="search_messages",
        name="Search Messages",
        description=(
            "Search for messages across all accessible Slack channels. Requires a user "
            "token (xoxp-*) -- not available with bot token only. If no user token is "
            "configured, returns an actionable error suggesting get_channel_history as "
            "an alternative for targeted searches."
        ),
        category="messaging",
        parameters=[
            {
                "name": "query",
                "type": "string",
                "required": True,
                "description": "Search query string (supports Slack search syntax)",
            },
        ],
        example="search_messages query='deployment error in:production'",
        response_entity_type="SlackSearchResult",
        response_identifier_field="ts",
    ),
    OperationDefinition(
        operation_id="list_channels",
        name="List Channels",
        description=(
            "List Slack channels accessible to the bot. Returns channel names, IDs, "
            "topics, purposes, and member counts. Use the returned channel IDs for "
            "other operations like get_channel_history or post_message."
        ),
        category="messaging",
        parameters=[
            {
                "name": "types",
                "type": "string",
                "required": False,
                "description": (
                    "Comma-separated channel types to include "
                    "(default: 'public_channel,private_channel'). "
                    "Options: public_channel, private_channel, mpim, im"
                ),
            },
        ],
        example="list_channels types=public_channel,private_channel",
        response_entity_type="SlackChannel",
        response_identifier_field="id",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="get_user_info",
        name="Get User Info",
        description=(
            "Get profile information for a Slack user. Returns display name, real name, "
            "email, status, and timezone. Use this to resolve user IDs found in messages "
            "to human-readable names."
        ),
        category="messaging",
        parameters=[
            {
                "name": "user_id",
                "type": "string",
                "required": True,
                "description": "Slack user ID (e.g., U01234567)",
            },
        ],
        example="get_user_info user_id=U01234567",
        response_entity_type="SlackUser",
        response_identifier_field="id",
        response_display_name_field="real_name",
    ),
    # Write Operations
    OperationDefinition(
        operation_id="post_message",
        name="Post Message",
        description=(
            "Post a message to a Slack channel. Supports threaded replies via thread_ts "
            "parameter. Use list_channels to find channel IDs. This is a WRITE operation "
            "that sends visible messages to the channel."
        ),
        category="messaging",
        parameters=[
            {
                "name": "channel",
                "type": "string",
                "required": True,
                "description": "Channel ID to post to (e.g., C01234567)",
            },
            {
                "name": "text",
                "type": "string",
                "required": True,
                "description": "Message text (supports Slack mrkdwn formatting)",
            },
            {
                "name": "thread_ts",
                "type": "string",
                "required": False,
                "description": "Timestamp of parent message for threaded reply",
            },
        ],
        example="post_message channel=C01234567 text='Investigation complete: root cause identified'",
    ),
    OperationDefinition(
        operation_id="add_reaction",
        name="Add Reaction",
        description=(
            "Add an emoji reaction to a Slack message. Use this to acknowledge messages "
            "or signal status during investigations. The emoji name should not include "
            "colons (e.g., use 'eyes' not ':eyes:')."
        ),
        category="messaging",
        parameters=[
            {
                "name": "channel",
                "type": "string",
                "required": True,
                "description": "Channel ID containing the message",
            },
            {
                "name": "timestamp",
                "type": "string",
                "required": True,
                "description": "Timestamp of the message to react to",
            },
            {
                "name": "name",
                "type": "string",
                "required": True,
                "description": "Emoji name without colons (e.g., 'eyes', 'white_check_mark')",
            },
        ],
        example="add_reaction channel=C01234567 timestamp=1234567890.123456 name=eyes",
    ),
]
