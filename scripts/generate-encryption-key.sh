#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
#
# Generates a Fernet-compatible encryption key for CREDENTIAL_ENCRYPTION_KEY.
#
# Usage:
#   scripts/generate-encryption-key.sh                # prints "CREDENTIAL_ENCRYPTION_KEY=<key>"
#   scripts/generate-encryption-key.sh >> .env        # appends to your .env file
#   scripts/generate-encryption-key.sh --raw          # prints just the key (no prefix)
#
# A Fernet key is 32 random bytes encoded as 44 chars of url-safe base64. The
# stdlib `secrets` module is used so this script has no third-party Python
# dependencies and works on a fresh machine before `uv sync` runs.

set -euo pipefail

if ! command -v python3 >/dev/null 2>&1; then
  echo "error: python3 is required to generate a Fernet key" >&2
  exit 1
fi

key="$(python3 -c 'import base64, secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())')"

case "${1:-}" in
  --raw)
    printf '%s\n' "${key}"
    ;;
  ""|--env)
    printf 'CREDENTIAL_ENCRYPTION_KEY=%s\n' "${key}"
    ;;
  -h|--help)
    sed -n '2,15p' "$0"
    ;;
  *)
    echo "error: unknown argument: $1" >&2
    echo "usage: $0 [--raw|--env|-h]" >&2
    exit 2
    ;;
esac
