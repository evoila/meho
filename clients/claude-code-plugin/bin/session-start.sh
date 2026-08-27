#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# SessionStart reflex hook. Fetches a compact digest of the tenant's
# recent activity plus the operator's scoped memory and knowledge, and
# prints it to stdout. Claude Code injects a SessionStart hook's stdout
# into the session as context, so read-before-start stops being a
# discipline the model has to remember — the digest is simply there.
#
# Fail-open is the whole contract. A missing `meho` CLI, an expired
# login, an unreachable backplane (no VPN), or a slow call all resolve
# to "print nothing, exit 0". The session must never break because this
# hook ran, and a half-fetched or error digest is worse than none.
#
# Not blocking-capable by design: SessionStart cannot block a session,
# and this hook has no reason to try.

# First line of defence and the fast path for the "no CLI" case: if
# `meho` is not on PATH we exit immediately using only shell builtins,
# so the hook stays silent and sub-second even when PATH is empty.
command -v meho >/dev/null 2>&1 || exit 0

# Bound every backplane call. Prefer coreutils `timeout` (Linux) or
# `gtimeout` (macOS + coreutils); if neither is present, run unbounded
# and rely on the per-hook `timeout` in hooks.json as the backstop.
timeout_bin=""
if command -v timeout >/dev/null 2>&1; then
  timeout_bin="timeout"
elif command -v gtimeout >/dev/null 2>&1; then
  timeout_bin="gtimeout"
fi

meho_read() {
  # meho_read SECONDS ARG...  — run `meho ARG...` under the timeout
  # wrapper, discarding stderr. Any failure surfaces as empty stdout.
  secs="$1"
  shift
  if [ -n "$timeout_bin" ]; then
    "$timeout_bin" "$secs" meho "$@" 2>/dev/null
  else
    meho "$@" 2>/dev/null
  fi
}

# Connectivity + auth gate. `meho status` renders identity + backplane
# health on success (non-empty stdout) and errors to stderr on failure.
# Bounded at 2s so the no-VPN / expired-login paths exit well under the
# 2s fail-open budget; a healthy /api/v1/health answers far faster.
status_out="$(meho_read 2 status)"
[ -n "$status_out" ] || exit 0

# Recent tenant activity is the durable form of the broadcast window:
# every MEHO op writes an audit row and emits a broadcast event, so
# `meho audit recent` is the bounded, non-streaming read of "what the
# tenant has been doing". (`meho status --watch` is the live SSE feed —
# unusable from a hook that must return promptly.)
#
# The checks plane runs sensor pins continuously under the `__sensor__`
# principal, so on any checks-enabled tenant those machine heartbeats
# dominate the newest audit rows and evict the operator/agent activity
# this window exists to surface. Fetch a wide page, drop the
# sensor-principal rows, THEN cut to the window budget — filtering
# before the truncation keeps the window full of real activity instead
# of collapsing to heartbeats (filter-after-truncate would show an all-
# sensor or empty window on a sensor-heavy tenant). `__sensor__` is a
# reserved sentinel the backplane forbids any operator from adopting,
# so a fixed-string drop of the token cannot suppress a real principal's
# rows. No CLI/backend change: the audit `--principal` filter is
# include-only (no invert) and server-side exclusion is out of scope,
# so the cheapest correct mechanism is a client-side drop here.
activity="$(meho_read 3 audit recent --limit 100 | grep -vF '__sensor__' | head -n 12)"

# Scoped memory recall — the operator's own notes/preferences in scope.
memory="$(meho_read 3 list --limit 8 | head -n 10)"

# Knowledge recall — the tenant's most recent knowledge-base entries.
knowledge="$(meho_read 3 kb list --limit 8 | head -n 10)"

body=""
if [ -n "$activity" ]; then
  body="${body}
## Recent tenant activity (broadcast window)
${activity}
"
fi
if [ -n "$memory" ]; then
  body="${body}
## Your MEHO memory (scoped)
${memory}
"
fi
if [ -n "$knowledge" ]; then
  body="${body}
## Recent MEHO knowledge
${knowledge}
"
fi

# Only emit the digest when at least one section carries content — an
# empty header is noise.
if [ -n "$body" ]; then
  printf '%s\n' "== MEHO reflex digest (auto-injected at session start; source: meho CLI) =="
  printf '%s\n' "$body"
fi

exit 0
