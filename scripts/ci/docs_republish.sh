#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# Publishes the docs of the current checkout (main) again under the version
# of the newest final release tag, and points `latest` at it. So a docs fix
# that merges on main is on the public site without waiting for the next
# tag. .github/workflows/docs-site.yml runs it after a push to main that
# changes docs-site/ or mkdocs.yml.
#
# The newest final tag is the highest v<MAJOR>.<MINOR>.<PATCH> tag without a
# `-` (a release candidate never counts). Its MAJOR.MINOR is the version:
# the same name a tag deploy uses (scripts/ci/docs_deploy.sh).
#
# Guards on the gh-pages branch:
#   * mike commits locally first, the script pushes only after its checks;
#   * it pushes nothing when the site is the same as on the remote branch
#     (no empty commit; the build time line of the pages does not count);
#   * it refuses to push when a file outside the republished version folder
#     would be deleted (other versions, versions.json, the site root). Inside
#     that folder the pages follow main: a page that main moved or removed
#     goes away there, and the script lists it;
#   * the push is a plain push, and the file counts before and after are
#     printed;
#   * when the push is refused because gh-pages moved (a tag deploy), it
#     fetches the branch again, finds the newest final tag again, builds
#     again and checks again, at most 3 times, then fails
#     (scripts/ci/docs_pages.sh).
#
# Usage: docs_republish.sh
# Prints the published version and, when GITHUB_OUTPUT is set, writes
# `version=<it>` there for the reachability check. Nothing is written when
# nothing was published.
# MIKE (optional) is the mike command; default: the locked docs toolchain.
set -euo pipefail

read -r -a MIKE <<< "${MIKE:-uv run --project backend --no-sync mike}"

# shellcheck source=scripts/ci/docs_pages.sh
. "$(dirname "${BASH_SOURCE[0]}")/docs_pages.sh"

# Sets VERSION to the MAJOR.MINOR of the newest final tag on origin.
# Returns 1 when origin cannot be read or has no final tag.
find_version() {
  local tags newest
  # A failed read must stop the run, not count as "no tag".
  tags=$(git ls-remote --tags --refs origin 'refs/tags/v*') || return 1
  newest=$(printf '%s\n' "${tags}" | awk '{print $2}' | sed 's|^refs/tags/||' \
    | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | tail -n 1)
  if [ -z "${newest}" ]; then
    echo "No final release tag found — nothing to publish." >&2
    return 1
  fi
  VERSION=$(printf '%s' "${newest#v}" | sed -E 's/^([0-9]+\.[0-9]+).*/\1/')
  echo "Newest release tag ${newest}; republishing docs version ${VERSION} from main."
}

attempt_publish() {
  # The branch exists after the first tag deploy.
  pages_fetch || return 1
  git show-ref --verify --quiet refs/heads/gh-pages || {
    echo "gh-pages does not exist yet — a tag deploy makes it first." >&2
    return 1
  }
  # Found again on every attempt: a refused push can mean that a tag deploy
  # of a newer release just moved `latest`.
  find_version || return 1
  BEFORE_COUNT=$(git ls-tree -r --name-only refs/remotes/origin/gh-pages | wc -l) || return 1

  "${MIKE[@]}" deploy --update-aliases "${VERSION}" latest || return 1
  "${MIKE[@]}" set-default latest || return 1

  AFTER_COUNT=$(git ls-tree -r --name-only refs/heads/gh-pages | wc -l) || return 1
  echo "gh-pages file count: ${BEFORE_COUNT} (remote), ${AFTER_COUNT} (new)."

  # The build writes its own build time into the pages ("Build Date UTC"), so
  # the trees differ on every build. That line does not count as a change.
  if git diff --quiet -I'^Build Date UTC' refs/remotes/origin/gh-pages refs/heads/gh-pages; then
    echo "The published site is already the same — nothing to push."
    NOTHING_PUSHED=1
    return 0
  fi
  DELETED=$(git diff --name-only --no-renames --diff-filter=D \
    refs/remotes/origin/gh-pages refs/heads/gh-pages) || return 1
  OUTSIDE=$(printf '%s\n' "${DELETED}" | grep -v -e "^${VERSION//./\\.}/" -e '^$' || true)
  if [ -n "${OUTSIDE}" ]; then
    echo "gh-pages would lose files outside ${VERSION}/ — not pushing:" >&2
    printf '%s\n' "${OUTSIDE}" >&2
    return 1
  fi
  if [ -n "${DELETED}" ]; then
    echo "Pages that main removed, deleted from ${VERSION}/:"
    printf '%s\n' "${DELETED}"
  fi

  git push origin gh-pages || return 2
}

VERSION=""
NOTHING_PUSHED=0
pages_publish attempt_publish
if [ "${NOTHING_PUSHED}" -eq 1 ]; then
  exit 0
fi

echo "Published docs version ${VERSION}."
if [ -n "${GITHUB_OUTPUT:-}" ]; then
  printf 'version=%s\n' "${VERSION}" >> "${GITHUB_OUTPUT}"
fi
