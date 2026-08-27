#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# PostToolUse reflex hook for the plugin's `meho_broadcast_announce` tool.
# Records that this session has announced (or reported) intent by writing
# a session-scoped marker. pre-call-operation.sh reads the marker to
# suppress its announce reminder, and stop-report.sh reads it to suppress
# its report reminder. `meho_broadcast_announce` serves both the start and
# completion phases, so a single marker covers "announced/reported".
#
# Matcher (hooks.json): `mcp__plugin_meho_meho__meho_broadcast_announce` —
# the plugin-scoped tool-name form (see pre-call-operation.sh). Pure
# side effect; the hook emits no decision and cannot block (PostToolUse
# runs after the tool has already executed).

state_dir="${TMPDIR:-/tmp}/meho-plugin-hooks"
mkdir -p "$state_dir" 2>/dev/null || true

payload="$(cat 2>/dev/null || true)"
sid="$(printf '%s' "$payload" | sed -n 's/.*"session_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)"
sid="$(printf '%s' "$sid" | tr -cd 'A-Za-z0-9._-')"
[ -n "$sid" ] || sid="nosession"

: > "${state_dir}/${sid}.announced" 2>/dev/null || true

exit 0
