# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Slack Type Definitions.

Defines entity types for Slack resources returned by connector operations.
These are registered in the connector_type table for agent discovery.
"""

from meho_app.modules.connectors.base import TypeDefinition

SLACK_TYPES: list[TypeDefinition] = [
    TypeDefinition(
        type_name="SlackMessage",
        description=(
            "A Slack message in a channel or thread. Contains the message text, "
            "sender user ID, timestamp (unique identifier), and optional thread "
            "timestamp for threaded conversations."
        ),
        category="messaging",
        properties=[
            {"name": "ts", "type": "string", "description": "Message timestamp (unique ID)"},
            {"name": "user", "type": "string", "description": "User ID of the sender"},
            {"name": "text", "type": "string", "description": "Message text content"},
            {
                "name": "thread_ts",
                "type": "string",
                "description": "Parent message timestamp (if in a thread)",
            },
            {"name": "type", "type": "string", "description": "Message type (usually 'message')"},
        ],
    ),
    TypeDefinition(
        type_name="SlackChannel",
        description=(
            "A Slack channel (public or private). Contains the channel name, ID, "
            "topic, purpose, and member count. Channel IDs are required for most "
            "Slack API operations."
        ),
        category="messaging",
        properties=[
            {"name": "id", "type": "string", "description": "Channel ID (e.g., C01234567)"},
            {"name": "name", "type": "string", "description": "Channel name (without #)"},
            {"name": "topic", "type": "string", "description": "Channel topic"},
            {"name": "purpose", "type": "string", "description": "Channel purpose"},
            {
                "name": "num_members",
                "type": "integer",
                "description": "Number of channel members",
            },
            {"name": "is_private", "type": "boolean", "description": "Whether channel is private"},
        ],
    ),
    TypeDefinition(
        type_name="SlackUser",
        description=(
            "A Slack workspace user. Contains display name, real name, email, "
            "status, and timezone. Use get_user_info to resolve user IDs from "
            "messages to human-readable names."
        ),
        category="messaging",
        properties=[
            {"name": "id", "type": "string", "description": "User ID (e.g., U01234567)"},
            {"name": "real_name", "type": "string", "description": "User's real name"},
            {"name": "display_name", "type": "string", "description": "User's display name"},
            {"name": "email", "type": "string", "description": "User's email address"},
            {"name": "tz", "type": "string", "description": "User's timezone"},
            {
                "name": "status_text",
                "type": "string",
                "description": "User's current status text",
            },
        ],
    ),
]
