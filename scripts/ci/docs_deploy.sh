#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# Publishes the docs of one `v*` tag with mike onto the gh-pages branch of
# the remote `origin`. .github/workflows/docs-site.yml runs it; its header
# explains the versions.
#
#   final tag (v1.0.0, v1.0.3)  publishes under MAJOR.MINOR (1.0), moves
#                               `latest` there and points the site root at
#                               `latest`.
#   rc tag, minor not final yet publishes under MAJOR.MINOR (1.0): the rc
#   (v1.0.0-rc.1, no v1.0.x)    docs are that minor's docs in progress.
#                               `latest` and the site root stay where they
#                               are.
#   rc tag, minor already final publishes under the rc's own name
#   (v1.0.1-rc.1 after v1.0.0)  (1.0.1-rc.1). `latest` points at the 1.0
#                               folder, so writing the rc into 1.0 would
#                               show rc pages to every reader of `latest`.
#
# mike commits locally; the script pushes with a plain `git push`. When the
# push is refused because a main republish (docs_republish.sh) moved
# gh-pages, it fetches gh-pages again, picks the version again and builds
# again, at most 3 times (scripts/ci/docs_pages.sh).
#
# Usage: docs_deploy.sh <tag name>
# Prints the published version and, when GITHUB_OUTPUT is set, writes
# `version=<it>` there for the reachability check.
# MIKE (optional) is the mike command; default: the locked docs toolchain.
set -euo pipefail

TAG="${1:?usage: docs_deploy.sh <tag name>}"
read -r -a MIKE <<< "${MIKE:-uv run --project backend --no-sync mike}"

# Require a full vMAJOR.MINOR prefix: a malformed tag (e.g. `v1-hotfix`)
# must fail here, not publish a garbage entry in the version selector.
case "${TAG}" in
  v[0-9]*.[0-9]*) ;;
  *)
    echo "Ref '${TAG}' is not a vMAJOR.MINOR[.PATCH] tag — nothing to deploy." >&2
    exit 1
    ;;
esac
MINOR=$(printf '%s' "${TAG#v}" | sed -E 's/^([0-9]+\.[0-9]+).*/\1/')

# shellcheck source=scripts/ci/docs_pages.sh
. "$(dirname "${BASH_SOURCE[0]}")/docs_pages.sh"

attempt_deploy() {
  pages_fetch || return 1
  case "${TAG}" in
    *-*)
      # Read on every attempt: the final tag of the minor can arrive while
      # this run waits. A failed read must stop the deploy, not count as
      # "no final tag".
      TAGS=$(git ls-remote --tags origin "refs/tags/v${MINOR}.*") || return 1
      if printf '%s\n' "${TAGS}" | awk '{print $2}' | grep -q -v -e '-' -e '^$'; then
        VERSION="${TAG#v}"
      else
        VERSION="${MINOR}"
      fi
      "${MIKE[@]}" deploy "${VERSION}" || return 1
      ;;
    *)
      VERSION="${MINOR}"
      "${MIKE[@]}" deploy --update-aliases "${VERSION}" latest || return 1
      "${MIKE[@]}" set-default latest || return 1
      ;;
  esac
  git push origin gh-pages || return 2
}

VERSION=""
pages_publish attempt_deploy

echo "Published docs version ${VERSION}."
if [ -n "${GITHUB_OUTPUT:-}" ]; then
  printf 'version=%s\n' "${VERSION}" >> "${GITHUB_OUTPUT}"
fi
