#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
#
# Push a clean orphan commit to the public evoila/meho repository.
#
# This script:
#   1. Copies the repo (excluding .git/, .planning/, .claude/, .cursor/)
#   2. Creates a fresh git repo with a single orphan commit
#   3. Verifies no private artifacts leaked into the commit
#   4. Pushes to the public repo using PUBLIC_REPO_PAT
#
# Usage:
#   PUBLIC_REPO_PAT="ghp_..." bash scripts/push-orphan.sh
#
# Run from the repository root directory.

set -euo pipefail

# ------------------------------------------------------------------
# Validate environment
# ------------------------------------------------------------------
if [ -z "${PUBLIC_REPO_PAT:-}" ]; then
  echo "ERROR: PUBLIC_REPO_PAT environment variable is not set."
  echo ""
  echo "Create a fine-grained PAT at:"
  echo "  https://github.com/settings/personal-access-tokens/new"
  echo ""
  echo "Scope: evoila/meho, Permission: Contents (read + write)"
  echo ""
  echo "Usage:"
  echo '  PUBLIC_REPO_PAT="ghp_..." bash scripts/push-orphan.sh'
  exit 1
fi

# Verify we're in the repo root
if [ ! -f "meho_app/core/licensing.py" ]; then
  echo "ERROR: Run this script from the repository root directory."
  exit 1
fi

# ------------------------------------------------------------------
# Create temp directory and copy repo
# ------------------------------------------------------------------
TMPDIR=$(mktemp -d)
echo "Working in temp directory: $TMPDIR"

echo "Copying repository (excluding private directories)..."
rsync -a \
  --exclude='.git' \
  --exclude='.planning' \
  --exclude='.claude' \
  --exclude='.cursor' \
  . "$TMPDIR/"

# ------------------------------------------------------------------
# Verify no private directories leaked
# ------------------------------------------------------------------
echo "Verifying no private artifacts in copy..."
LEAKED=""
for dir in .planning .claude .cursor; do
  if [ -d "$TMPDIR/$dir" ]; then
    LEAKED="$LEAKED $dir"
  fi
done
if [ -n "$LEAKED" ]; then
  echo "LEAK DETECTED: Private directories found in copy:$LEAKED"
  rm -rf "$TMPDIR"
  exit 1
fi
echo "  No private directories found."

# ------------------------------------------------------------------
# Create orphan commit
# ------------------------------------------------------------------
echo "Initializing fresh git repository..."
cd "$TMPDIR"
git init
git config user.name "meho-mirror-bot"
git config user.email "mirror-bot@evoila.com"

echo "Staging all files..."
git add -A

echo "Creating orphan commit..."
git commit -m "Initial public release of MEHO v2.3"

# ------------------------------------------------------------------
# Final leak check on the committed tree
# ------------------------------------------------------------------
echo "Final leak check on committed tree..."
if git ls-tree -r HEAD --name-only | grep -qE '^\.(planning|claude|cursor)/'; then
  echo "LEAK DETECTED: Private paths found in committed tree!"
  git ls-tree -r HEAD --name-only | grep -E '^\.(planning|claude|cursor)/'
  rm -rf "$TMPDIR"
  exit 1
fi
echo "  Committed tree is clean."

# ------------------------------------------------------------------
# Push to public repo
# ------------------------------------------------------------------
echo "Adding public remote..."
git remote add origin "https://x-access-token:${PUBLIC_REPO_PAT}@github.com/evoila/meho.git"

echo "Pushing to public repo (evoila/meho)..."
git push --force -u origin main

echo ""
echo "============================================================"
echo "SUCCESS: Orphan commit pushed to https://github.com/evoila/meho"
echo "============================================================"
echo ""
echo "Next steps:"
echo "  1. Verify https://github.com/evoila/meho shows code without .planning/"
echo "  2. Add PUBLIC_REPO_PAT secret to the private repo"
echo "  3. Configure branch rulesets on the public repo"
echo "  4. Store the Ed25519 private key securely (see Task 1 output)"
echo ""

# ------------------------------------------------------------------
# Cleanup
# ------------------------------------------------------------------
cd -
rm -rf "$TMPDIR"
echo "Temp directory cleaned up."
