# Slack

> Last verified: v2.3

MEHO's Slack connector enables querying channel history, searching messages, posting replies, and adding reactions -- giving investigations access to team communication context. The agent can pull Slack messages as a data source during cross-system investigations, and post investigation results back to channels.

## Authentication

| Method | Credential Fields | Notes |
|--------|------------------|-------|
| Bot Token | `slack_bot_token` | Required. `xoxb-*` bot token for all operations except search |
| User Token | `slack_user_token` | Optional. `xoxp-*` user token required for `search_messages` |

### Setup

1. **Create a Slack app** at [api.slack.com/apps](https://api.slack.com/apps) > **Create New App** > **From scratch**
2. **Configure bot token scopes** under **OAuth & Permissions** > **Bot Token Scopes**:
    - `channels:history` -- Read channel message history
    - `channels:read` -- List channels
    - `chat:write` -- Post messages and threaded replies
    - `reactions:write` -- Add emoji reactions
    - `users:read` -- Get user profile information
3. **Install the app** to your workspace and copy the **Bot User OAuth Token** (`xoxb-...`)
4. **Optionally**, add a **User Token** (`xoxp-...`) with `search:read` scope for message search
5. **Add the connector in MEHO** using the bot token (and optionally user token) as credentials

## Operations

| Operation | Trust Level | Description |
|-----------|-------------|-------------|
| `list_channels` | READ | List accessible Slack channels with member counts and topics |
| `get_channel_history` | READ | Fetch recent messages from a channel with timestamps and authors |
| `search_messages` | READ | Search messages across channels (requires user token) |
| `get_user_info` | READ | Get user profile information (name, email, status) |
| `post_message` | WRITE | Post a message or threaded reply to a channel |
| `add_reaction` | WRITE | Add an emoji reaction to a message |

## Slack Bot (Socket Mode)

When a Slack connector is active with both `slack_bot_token` and `slack_app_token` configured, MEHO starts a Slack bot in Socket Mode on application startup. The bot listens for the `/meho` slash command to trigger investigations directly from Slack.

### Additional Setup for Bot

1. **Enable Socket Mode** in your Slack app settings and generate an **App-Level Token** (`xapp-...`) with `connections:write` scope
2. **Create a Slash Command** (`/meho`) under your app's **Slash Commands** settings
3. **Add `slack_app_token`** to your connector credentials alongside the bot token

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MEHO_SLACK_MODE` | `socket` | Bot connection mode (`socket` for Socket Mode) |
| `MEHO_FEATURE_SLACK` | `true` | Enable/disable Slack connector and bot |
