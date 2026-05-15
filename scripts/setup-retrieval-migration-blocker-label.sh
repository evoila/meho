#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# setup-retrieval-migration-blocker-label.sh — create or refresh the
# `retrieval-migration-blocker` GitHub label on the target repo.
#
# Companion to the G4.3-T6 retire-checklist verb
# (`meho retrieval retire-checklist`): its criterion 5 reads
# `gh issue list --label retrieval-migration-blocker --state open`
# and pins the verdict at NOT YET while any open blocker exists. This
# script creates the label so operators have a single emergency-brake
# they can apply to any retrieval-regression issue.
#
# Idempotent: re-running updates the description / colour to the
# canonical values via `gh label create --force` (which is `create-or-
# update`). Re-runs are safe and cheap.
#
# Usage:
#   scripts/setup-retrieval-migration-blocker-label.sh            # apply against $REPO (default: evoila/meho)
#   scripts/setup-retrieval-migration-blocker-label.sh --dry-run  # print, do not mutate
#   REPO=evoila-bosnia/claude-rdc-hetzner-dc \
#     scripts/setup-retrieval-migration-blocker-label.sh          # apply against the consumer repo
#
# Requires: `gh` (authenticated as a maintainer with `repo:admin` or
# equivalent on the target repo).
#
# Verify after:
#   gh label list --repo "$REPO" --search retrieval-migration-blocker
#
# Documented in: docs/cross-repo/retrieval-retirement.md
set -Eeuo pipefail

REPO="${REPO:-evoila/meho}"
LABEL_NAME="retrieval-migration-blocker"
# Red-orange — matches the `priority:high` palette so the label reads as
# "stop the retire" at a glance in the GitHub issue list. Hex without
# the leading `#` per the gh API contract.
LABEL_COLOR="b60205"
LABEL_DESCRIPTION="Halts retire of a MEHO retrieval surface (kb / memory / operations); read by meho retrieval retire-checklist criterion 5."

DRY_RUN=0

for arg in "$@"; do
  case "$arg" in
    --dry-run)
      DRY_RUN=1
      ;;
    -h | --help)
      # Print the header doc comment (lines 2-31) so `--help` mirrors
      # this file's top-of-file documentation without duplicating it.
      # Line 31 is the "Documented in:" back-reference to the runbook.
      sed -n '2,31p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "setup-retrieval-migration-blocker-label.sh: unknown argument: $arg" >&2
      exit 2
      ;;
  esac
done

if ! command -v gh >/dev/null 2>&1; then
  echo "setup-retrieval-migration-blocker-label.sh: gh CLI is required but not installed" >&2
  exit 1
fi

echo "repo:         $REPO"
echo "label:        $LABEL_NAME"
echo "color:        #$LABEL_COLOR"
echo "description:  $LABEL_DESCRIPTION"

if [[ $DRY_RUN -eq 1 ]]; then
  echo "(dry-run; no API calls)"
  exit 0
fi

# `gh label create --force` is the documented create-or-update path:
# https://cli.github.com/manual/gh_label_create
#   > Use --force to update an existing label.
# Re-runs reconcile the description/colour without erroring on
# "label already exists" (which a bare `gh label create` does, exit 1).
echo "==> gh label create --force ($REPO)"
gh label create "$LABEL_NAME" \
  --repo "$REPO" \
  --color "$LABEL_COLOR" \
  --description "$LABEL_DESCRIPTION" \
  --force

echo "==> done. Verify:"
echo "    gh label list --repo $REPO --search $LABEL_NAME"
