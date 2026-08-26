#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# Build the "MEHO for Claude Desktop" .mcpb bundle.
#
# Usage: build.sh <version> [output-dir]
#
#   version     Semver stamped into the bundled manifest and the file
#               name (the release pipeline passes the git tag without
#               its leading `v`; a local build can pass any semver).
#   output-dir  Where the .mcpb lands (default: ./dist next to this
#               script). Created if absent.
#
# The pinned `@anthropic-ai/mcpb` CLI validates the manifest against the
# MANIFEST 0.3 schema before zipping, so a malformed manifest fails the
# build (and, in CI, the PR / release job) rather than shipping.

set -euo pipefail

# @anthropic-ai/mcpb CLI version. Pinned so the schema the manifest is
# validated against — and the bundle layout — are reproducible across
# local builds and CI. Bump deliberately.
MCPB_VERSION="2.1.2"

VERSION="${1:-}"
if [[ -z "${VERSION}" ]]; then
  echo "usage: build.sh <version> [output-dir]" >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="${2:-${SCRIPT_DIR}/dist}"
mkdir -p "${OUT_DIR}"

# Stage only the runtime files (manifest + server/) so build.sh, the
# README, and the .gitignore never end up inside the shipped bundle.
# The manifest is committed with a placeholder version (0.0.0); the real
# version is injected into the staged copy so the source tree stays clean.
STAGE="$(mktemp -d)"
trap 'rm -rf "${STAGE}"' EXIT

jq --arg v "${VERSION}" '.version = $v' "${SCRIPT_DIR}/manifest.json" \
  > "${STAGE}/manifest.json"
cp -R "${SCRIPT_DIR}/server" "${STAGE}/server"

OUT_FILE="${OUT_DIR}/meho-claude-desktop-${VERSION}.mcpb"
npx --yes "@anthropic-ai/mcpb@${MCPB_VERSION}" pack "${STAGE}" "${OUT_FILE}"

echo "Built ${OUT_FILE}"
