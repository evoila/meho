#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# PreToolUse reflex hook for the plugin's `call_operation` tool. The first
# time a session calls `call_operation`, it emits a one-time, advisory
# reminder: read the broadcast feed with `meho_broadcast_recent` before
# changing a target. People read the feed; only agents post. The plugin
# logs in as a person, and MEHO refuses an announcement from a person's
# login, so the reminder says to skip `meho_broadcast_announce`. It never
# denies or blocks the call.
#
# Matcher note (hooks.json): a tool from a plugin-bundled MCP server is
# scoped as `mcp__plugin_<plugin>_<server>__<tool>`. Both the plugin and
# the MCP server here are named `meho`, so the tool is
# `mcp__plugin_meho_meho__call_operation`. A bare `mcp__meho__...`
# matcher never fires for a plugin-bundled server.
#
# Reminder channel: PreToolUse plain stdout goes to the debug log, so the
# nudge is returned as `hookSpecificOutput.additionalContext` in a JSON
# object on stdout. `permissionDecision` is deliberately omitted so the
# normal permission flow proceeds (no allow/deny side effect).
#
# Session state lives in a marker file keyed by session_id under a tmp
# dir, so the reminder fires at most once per session.

# Session-scoped marker directory. tmp is the right home for ephemeral
# per-session state; keying on session_id keeps parallel sessions apart.
state_dir="${TMPDIR:-/tmp}/meho-plugin-hooks"
mkdir -p "$state_dir" 2>/dev/null || true

# Pull session_id out of the hook's JSON stdin without a jq dependency.
# An unparseable payload falls back to a fixed bucket so dedupe still
# holds within this process's view.
payload="$(cat 2>/dev/null || true)"
sid="$(printf '%s' "$payload" | sed -n 's/.*"session_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)"
sid="$(printf '%s' "$sid" | tr -cd 'A-Za-z0-9._-')"
[ -n "$sid" ] || sid="nosession"

reminded_marker="${state_dir}/${sid}.reminded"

# One nudge per session.
if [ ! -e "$reminded_marker" ]; then
  : > "$reminded_marker" 2>/dev/null || true
  msg="MEHO reflex: before you change a target, check meho_broadcast_recent (with filter.target) for agents already working on it. People read the feed; only agents post. Your login is a person's login, so skip meho_broadcast_announce: MEHO refuses it, and your operations are already recorded in the audit log. Advisory only: this appears once per session and does not block the call."
  printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":"%s"}}\n' "$msg"
fi

exit 0
