# Slack Connector

Use Slack operations to read channel history, search messages, post messages, and interact with channels.

## Operations

- **get_channel_history**: Read messages from a specific channel. Use `list_channels` first to find the channel ID.
- **search_messages**: Search across all channels (requires user token). Falls back to error if only bot token. Use `get_channel_history` with specific channels as alternative.
- **list_channels**: Discover available channels and their IDs. Always call this before get_channel_history if you don't have a channel ID.
- **get_user_info**: Look up user display name and details by user ID found in messages.
- **post_message**: Send a message to a channel. Use `thread_ts` parameter to reply in a thread.
- **add_reaction**: React to a message with an emoji (e.g., acknowledge an alert).

## Investigation Tips

- Channel names are NOT channel IDs. Always use `list_channels` to resolve names to IDs first.
- Message timestamps (`ts`) are unique identifiers. Use them for threading and reactions.
- When investigating an issue discussed in Slack, start with `search_messages` (if available) or `get_channel_history` on the relevant channel.
- `get_channel_history` supports `oldest` and `latest` epoch timestamps to narrow time ranges.
- Post investigation findings back to the channel using `post_message` with a clear summary.
