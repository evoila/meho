#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# Stop reflex hook. If this session invoked call_operation but never
# announced or reported via broadcast_announce, it emits a one-line
# report-on-completion reminder. Advisory only: it returns the nudge as
# `hookSpecificOutput.additionalContext` and exits 0, so the session
# stops normally. (Exit 2 would prevent stopping — a blocking decision
# this hook must never make.)
#
# State is the same session-scoped marker set the call_operation and
# broadcast_announce hooks maintain: remind only when `.used` exists and
# `.announced` does not.

state_dir="${TMPDIR:-/tmp}/meho-plugin-hooks"
mkdir -p "$state_dir" 2>/dev/null || true

payload="$(cat 2>/dev/null || true)"
sid="$(printf '%s' "$payload" | sed -n 's/.*"session_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)"
sid="$(printf '%s' "$sid" | tr -cd 'A-Za-z0-9._-')"
[ -n "$sid" ] || sid="nosession"

used_marker="${state_dir}/${sid}.used"
announced_marker="${state_dir}/${sid}.announced"

if [ -e "$used_marker" ] && [ ! -e "$announced_marker" ]; then
  msg="MEHO reflex: this session invoked call_operation but never announced or reported via broadcast_announce. Before ending, consider a broadcast_announce with phase=completion and a short result summary so the tenant feed reflects what changed. Advisory only."
  printf '{"hookSpecificOutput":{"hookEventName":"Stop","additionalContext":"%s"}}\n' "$msg"
fi

exit 0
