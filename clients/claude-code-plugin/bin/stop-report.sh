#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# Stop reflex hook. If this session invoked call_operation but never
# announced or reported via meho_broadcast_announce, it emits a one-line
# report-on-completion reminder as `hookSpecificOutput.additionalContext`.
#
# Semantics (do not misread): additionalContext on a Stop hook does NOT
# let the turn end. Per the Claude Code hooks docs it continues the
# conversation so Claude can act on the feedback, under the same loop
# protections as `decision: block` — the `stop_hook_active` input and the
# harness's 8-consecutive-continuation cap. That one extra turn is the
# intended mechanism (it is how the report-on-completion nudge gets a
# chance to fire), but it must fire at most once. Two guards bound it:
#   1. `stop_hook_active` == true → the harness is already continuing
#      because of a stop hook; no-op so continuations never stack.
#   2. a per-session `.stop_reminded` marker → emit at most once per
#      session, even across independent Stop events after a clean stop.
# The hook never exits 2 (that would hard-block the stop); the single
# bounded continuation above is the only nudge it makes.
#
# State is the same session-scoped marker set the call_operation and
# meho_broadcast_announce hooks maintain: remind only when `.used` exists and
# `.announced` does not.

state_dir="${TMPDIR:-/tmp}/meho-plugin-hooks"
mkdir -p "$state_dir" 2>/dev/null || true

payload="$(cat 2>/dev/null || true)"
sid="$(printf '%s' "$payload" | sed -n 's/.*"session_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)"
sid="$(printf '%s' "$sid" | tr -cd 'A-Za-z0-9._-')"
[ -n "$sid" ] || sid="nosession"

# If the harness is already continuing because of a stop hook, adding
# another continuation would re-nag toward the 8-continuation cap. Bail
# before emitting anything. (Match the boolean literal directly; avoid
# sed alternation for BSD/GNU portability.)
stop_active="$(printf '%s' "$payload" | sed -n 's/.*"stop_hook_active"[[:space:]]*:[[:space:]]*true.*/true/p' | head -n 1)"
[ "$stop_active" = "true" ] && exit 0

used_marker="${state_dir}/${sid}.used"
announced_marker="${state_dir}/${sid}.announced"
reminded_marker="${state_dir}/${sid}.stop_reminded"

if [ -e "$used_marker" ] && [ ! -e "$announced_marker" ] && [ ! -e "$reminded_marker" ]; then
  : > "$reminded_marker" 2>/dev/null || true
  msg="MEHO reflex: this session invoked call_operation but never announced or reported via meho_broadcast_announce. Before ending, consider a meho_broadcast_announce with phase=completion and a short result summary so the tenant feed reflects what changed. Advisory only."
  printf '{"hookSpecificOutput":{"hookEventName":"Stop","additionalContext":"%s"}}\n' "$msg"
fi

exit 0
