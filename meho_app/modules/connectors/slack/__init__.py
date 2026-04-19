# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Slack Connector Module.

Provides Slack integration for channel queries, message posting, and reactions.

Supported operations:
- get_channel_history: Fetch channel message history
- search_messages: Search messages (requires user token)
- list_channels: List available channels
- get_user_info: Get user profile information
- post_message: Post messages and threaded replies
- add_reaction: Add emoji reactions to messages
"""

from meho_app.modules.connectors.slack.connector import SlackConnector
from meho_app.modules.connectors.slack.operations import SLACK_OPERATIONS, SLACK_OPERATIONS_VERSION
from meho_app.modules.connectors.slack.types import SLACK_TYPES

__all__ = [
    "SLACK_OPERATIONS",
    "SLACK_OPERATIONS_VERSION",
    "SLACK_TYPES",
    "SlackConnector",
]
