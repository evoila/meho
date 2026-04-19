#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
#
# Assemble the public-mirror staging tree from the allowlist.
#
# Reads .github/workflows/public-allowlist.txt (or an override via --allowlist),
# validates every entry, copies each into a temp staging directory, runs the
# required-paths check and the derived-denylist defensive check, and prints
# the resulting staging path on stdout.
#
# See docs/codebase/public-mirror.md for the full pipeline context.
#
# Usage:
#   scripts/assemble-public-tree.sh                    # prints staging path
#   scripts/assemble-public-tree.sh --dry-run          # prints file list
#   scripts/assemble-public-tree.sh --allowlist PATH   # override location
#
# Exit codes:
#   0  success
#   1  allowlist missing, invalid entries, or staging-tree defensive-check failure
#   2  bad command-line arguments

set -euo pipefail

DRY_RUN=0
ALLOWLIST=""

print_help() {
  cat <<'HELP'
Assemble the public-mirror staging tree from the allowlist.

Reads .github/workflows/public-allowlist.txt (or an override via --allowlist),
validates every entry, copies each into a temp staging directory, runs the
required-paths check and the derived-denylist defensive check, and prints
the resulting staging path on stdout.

See docs/codebase/public-mirror.md for the full pipeline context.

Usage:
  scripts/assemble-public-tree.sh                    # prints staging path
  scripts/assemble-public-tree.sh --dry-run          # prints file list
  scripts/assemble-public-tree.sh --allowlist PATH   # override location

Exit codes:
  0  success
  1  allowlist missing, invalid entries, or staging-tree defensive-check failure
  2  bad command-line arguments
HELP
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      ;;
    --allowlist)
      shift
      if [ $# -eq 0 ]; then
        echo "::error::--allowlist requires a path" >&2
        exit 2
      fi
      ALLOWLIST="$1"
      ;;
    -h|--help)
      print_help
      exit 0
      ;;
    *)
      echo "::error::Unknown argument: $1" >&2
      exit 2
      ;;
  esac
  shift
done

# Resolve the repo root from the script location so the script works regardless
# of the caller's cwd. This is what makes `STAGING=$(scripts/assemble-public-tree.sh)`
# from the workflow and `./scripts/assemble-public-tree.sh --dry-run` from any
# subdirectory both work.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

if [ -z "$ALLOWLIST" ]; then
  ALLOWLIST=".github/workflows/public-allowlist.txt"
fi

if [ ! -f "$ALLOWLIST" ]; then
  echo "::error::Allowlist file not found: $ALLOWLIST" >&2
  exit 1
fi

# Validate every allowlist entry before touching the filesystem. The allowlist
# is user input to a force-pushed publish pipeline; accepting malformed entries
# silently is a latent leak/no-op risk.
INVALID=0
while IFS= read -r entry || [ -n "$entry" ]; do
  entry="${entry%%#*}"
  entry="$(printf '%s\n' "$entry" | awk '{$1=$1;print}')"
  [ -z "$entry" ] && continue

  case "$entry" in
    /*)
      echo "::error::Allowlist entry must be relative to repo root: '$entry'" >&2
      INVALID=1
      ;;
    -*)
      echo "::error::Allowlist entry must not start with '-' (would be parsed as a cp/mkdir option): '$entry'" >&2
      INVALID=1
      ;;
    *..*)
      echo "::error::Allowlist entry must not contain '..': '$entry'" >&2
      INVALID=1
      ;;
    *\**|*\?*|*\[*)
      echo "::error::Allowlist entry must not contain glob characters (*, ?, []): '$entry'" >&2
      INVALID=1
      ;;
    */)
      echo "::error::Allowlist entry must not have a trailing slash: '$entry'" >&2
      INVALID=1
      ;;
  esac
done < "$ALLOWLIST"

if [ "$INVALID" -ne 0 ]; then
  echo "::error::Allowlist contains invalid entries (see above). Aborting." >&2
  exit 1
fi

# Dry-run: print what would be copied and exit without creating a staging dir.
if [ "$DRY_RUN" -eq 1 ]; then
  echo "Files that would be copied into staging:" >&2
  while IFS= read -r entry || [ -n "$entry" ]; do
    entry="${entry%%#*}"
    entry="$(printf '%s\n' "$entry" | awk '{$1=$1;print}')"
    [ -z "$entry" ] && continue

    if [ ! -e "$entry" ]; then
      echo "  MISSING: $entry" >&2
    elif [ -d "$entry" ]; then
      find "$entry" -type f
    else
      echo "$entry"
    fi
  done < "$ALLOWLIST"
  exit 0
fi

STAGING="$(mktemp -d)"
ALLOWED_TOP="$(mktemp)"

# Only clean up on failure — the caller needs $STAGING on success.
cleanup() {
  local status=$?
  rm -f "$ALLOWED_TOP"
  if [ "$status" -ne 0 ]; then
    rm -rf "$STAGING"
  fi
}
trap cleanup EXIT

MISSING=0
COPIED=0

while IFS= read -r entry || [ -n "$entry" ]; do
  entry="${entry%%#*}"
  entry="$(printf '%s\n' "$entry" | awk '{$1=$1;print}')"
  [ -z "$entry" ] && continue

  # Record top-level component for the derived defensive check below.
  echo "${entry%%/*}" >> "$ALLOWED_TOP"

  if [ ! -e "$entry" ]; then
    echo "::warning::Allowlist entry missing from repo: $entry" >&2
    MISSING=$((MISSING + 1))
    continue
  fi

  # Preserve directory structure for nested paths (e.g. foo/bar.txt). `--`
  # forces end-of-options on cp/mkdir as belt-and-braces against any entry
  # that somehow reaches here with a leading dash.
  mkdir -p -- "$STAGING/$(dirname "$entry")"
  cp -a -- "$entry" "$STAGING/$(dirname "$entry")/"
  COPIED=$((COPIED + 1))
done < "$ALLOWLIST"

echo "Copied $COPIED allowlist entries into staging." >&2
if [ "$MISSING" -gt 0 ]; then
  echo "::warning::$MISSING allowlist entries were missing (see warnings above)." >&2
fi

# Sanity check: staging must contain at least the product source tree.
for required in meho_app pyproject.toml README.md LICENSE; do
  if [ ! -e "$STAGING/$required" ]; then
    echo "::error::Required path '$required' missing from staging tree — allowlist is broken." >&2
    exit 1
  fi
done

# Defensive check: every top-level entry in the staging tree must correspond
# to an allowlist entry. The allowlist is the single source of truth — anything
# top-level in staging not covered by it is a leak. Scoped to maxdepth 1
# because nested fixtures inside legitimately shipped directories (e.g. a
# sample spec containing its own .vscode) are fine; only the top level is
# gated.
sort -u "$ALLOWED_TOP" -o "$ALLOWED_TOP"

LEAKED=""
while IFS= read -r staged; do
  name="$(basename "$staged")"
  if ! grep -qxF "$name" "$ALLOWED_TOP"; then
    LEAKED="${LEAKED}${staged}"$'\n'
  fi
done < <(find "$STAGING" -mindepth 1 -maxdepth 1)

if [ -n "$LEAKED" ]; then
  echo "::error::Staging tree contains top-level paths not covered by the allowlist:" >&2
  printf '%s' "$LEAKED" >&2
  exit 1
fi

echo "Staging tree assembled cleanly." >&2

# Stdout contract: the staging path, and nothing else.
echo "$STAGING"
