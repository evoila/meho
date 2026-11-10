#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# Grep a MEHO settings export (meho-settings.yaml) for secret-shaped text.
#
# The settings file never carries a secret (docs/codebase/settings.md, "Never
# in the file"). This check fails when an export holds a JWT, a password key,
# or a PEM private-key block. It prints the line number and the kind of match,
# never the matching text.
#
# Usage: check-settings-export-secrets.sh <export.yaml>   (or "-" for stdin)
#
# Run by backend/tests/test_api_v1_settings.py against a live export on every
# CI run, and usable by hand against `meho settings export` output.
set -euo pipefail

file="${1:--}"
if [[ "$file" == "-" ]]; then
  content="$(cat)"
else
  content="$(cat -- "$file")"
fi

status=0
check() {
  local label="$1" pattern="$2" lines
  lines="$(printf '%s\n' "$content" | grep -nEi -- "$pattern" | cut -d: -f1 | paste -sd, - || true)"
  if [[ -n "$lines" ]]; then
    echo "settings export holds ${label} on line(s) ${lines}" >&2
    status=1
  fi
}

check "a JWT" 'eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.'
check "a PEM private key" '-----BEGIN ([A-Z0-9 ]+ )?PRIVATE KEY-----'
check "a password key" '(^|[[:space:]{,])"?[A-Za-z_]*(password|passwd|secret_key|client_secret|api_key|token)"?[[:space:]]*:'

if [[ "$status" -eq 0 ]]; then
  echo "settings export: no secret-shaped text found"
fi
exit "$status"
