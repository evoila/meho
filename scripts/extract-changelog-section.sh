#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
#
# Extract a single ## [<version>] section body from a Keep a Changelog
# 1.1.0 file.
#
# Used by .github/workflows/release.yml — both the validate-tag pre-flight
# (to enforce the "graduated heading is also non-empty" invariant) and the
# publish-to-public-repo job (to feed `gh release create --notes-file`).
# Centralising the extraction logic in one place removes the divergence
# risk noted in docs/codebase/release-and-deployment.md.
#
# Usage:
#   scripts/extract-changelog-section.sh <version> [<changelog-path>]
#
# Writes the section body (excluding the heading and the bottom KAC
# link-reference block) to stdout. The section ends at either the next
# `## ` heading (any other version section) or the first link-reference
# line (`^[<text>]: <whitespace>`). Both terminators handle the common
# cases — multi-version files and the "single or final version section
# above the bottom link refs" layout.
#
# Why awk literal-prefix matching: version strings contain dots, which
# behave as wildcards in regex. `index($0, target) == 1` keeps the match
# strictly literal. The closing `]` in the target acts as the version
# terminator, so a search for `## [0.1.1]` does not collide with a
# heading like `## [0.1.10] - <date>`.
#
# Exit codes:
#   0  awk completed successfully (output may still be empty if the
#      section is missing or empty — the caller decides whether to treat
#      empty output as an error)
#   1  CHANGELOG file does not exist or is unreadable
#   2  bad command-line arguments

set -euo pipefail

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
  echo "usage: $0 <version> [<changelog-path>]" >&2
  exit 2
fi

VERSION="$1"
CHANGELOG="${2:-CHANGELOG.md}"

if [ ! -r "$CHANGELOG" ]; then
  echo "$0: cannot read CHANGELOG file: $CHANGELOG" >&2
  exit 1
fi

awk -v target="## [$VERSION]" '
  /^## / {
    if (in_section) exit
    in_section = (index($0, target) == 1)
    next
  }
  in_section && /^\[[^]]+\]:[[:space:]]/ { exit }
  in_section
' "$CHANGELOG"
