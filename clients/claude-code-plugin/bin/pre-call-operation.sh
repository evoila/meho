#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# PreToolUse reflex hook for the plugin's `call_operation` tool. If this
# session has not announced intent yet, it emits a one-time, advisory
# reminder naming `broadcast_announce`. It never denies or blocks the
# call — enforcement with teeth is the server-side announce gate's job;
# this is a warn-only nudge.
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
# Session state lives in marker files keyed by session_id under a
# tmp dir, written by this hook and by post-announce.sh.

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

used_marker="${state_dir}/${sid}.used"
announced_marker="${state_dir}/${sid}.announced"
reminded_marker="${state_dir}/${sid}.reminded"

# Record that this session reached for call_operation — the Stop hook
# reads this to decide whether a report reminder is due.
: > "$used_marker" 2>/dev/null || true

# One nudge per session: skip if the session already announced, or if the
# reminder already fired once.
if [ ! -e "$announced_marker" ] && [ ! -e "$reminded_marker" ]; then
  : > "$reminded_marker" 2>/dev/null || true
  msg="MEHO reflex: this session is calling call_operation without having announced intent. Call broadcast_announce (phase=start) so other operators watching the tenant feed see your work, and report with phase=completion when you finish. Advisory only — this appears once per session and does not block the call."
  printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":"%s"}}\n' "$msg"
fi

exit 0
