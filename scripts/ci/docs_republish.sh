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
#   * it refuses to push a branch with fewer files than the remote one has;
#   * the push is a plain push, and the file counts before and after are
#     printed.
#
# Usage: docs_republish.sh
# Prints the published version and, when GITHUB_OUTPUT is set, writes
# `version=<it>` there for the reachability check. Nothing is written when
# nothing was published.
# MIKE (optional) is the mike command; default: the locked docs toolchain.
set -euo pipefail

read -r -a MIKE <<< "${MIKE:-uv run --project backend --no-sync mike}"

# A failed read must stop the run, not count as "no tag".
TAGS=$(git ls-remote --tags --refs origin 'refs/tags/v*')
NEWEST=$(printf '%s\n' "${TAGS}" | awk '{print $2}' | sed 's|^refs/tags/||' \
  | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | tail -n 1 || true)
if [ -z "${NEWEST}" ]; then
  echo "No final release tag found — nothing to publish." >&2
  exit 1
fi
VERSION=$(printf '%s' "${NEWEST#v}" | sed -E 's/^([0-9]+\.[0-9]+).*/\1/')
echo "Newest release tag ${NEWEST}; republishing docs version ${VERSION} from main."

# The branch exists after the first tag deploy.
git fetch origin gh-pages:refs/remotes/origin/gh-pages
git update-ref refs/heads/gh-pages refs/remotes/origin/gh-pages
BEFORE_COUNT=$(git ls-tree -r --name-only refs/remotes/origin/gh-pages | wc -l)

"${MIKE[@]}" deploy --update-aliases "${VERSION}" latest
"${MIKE[@]}" set-default latest

AFTER_COUNT=$(git ls-tree -r --name-only refs/heads/gh-pages | wc -l)
echo "gh-pages file count: ${BEFORE_COUNT} (remote), ${AFTER_COUNT} (new)."

# The build writes its own build time into the pages ("Build Date UTC"), so
# the trees differ on every build. That line does not count as a change.
if git diff --quiet -I'^Build Date UTC' refs/remotes/origin/gh-pages refs/heads/gh-pages; then
  echo "The published site is already the same — nothing to push."
  exit 0
fi
if [ "${AFTER_COUNT}" -lt "${BEFORE_COUNT}" ]; then
  echo "gh-pages would lose files (${BEFORE_COUNT} -> ${AFTER_COUNT}) — not pushing." >&2
  exit 1
fi

git push origin gh-pages
echo "Published docs version ${VERSION}."
if [ -n "${GITHUB_OUTPUT:-}" ]; then
  printf 'version=%s\n' "${VERSION}" >> "${GITHUB_OUTPUT}"
fi
