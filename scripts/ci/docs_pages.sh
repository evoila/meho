# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
# shellcheck shell=bash
#
# Helpers for the two scripts that write the gh-pages branch:
# docs_deploy.sh (a tag) and docs_republish.sh (main). They run in two
# concurrency groups of .github/workflows/docs-site.yml, so one can push
# gh-pages while the other builds. Then the second push is refused, and
# the script fetches gh-pages again and builds again on top of it.
#
# Sourced, not run.

PAGES_MAX_ATTEMPTS=3

# Makes the local gh-pages branch the same as the one on origin. When origin
# has no gh-pages yet (the first deploy), the local branch is removed and
# mike starts a new one. Returns 1 when origin cannot be read.
pages_fetch() {
  local rc=0
  git ls-remote --exit-code --heads origin gh-pages >/dev/null || rc=$?
  if [ "${rc}" -eq 2 ]; then
    if git show-ref --verify --quiet refs/heads/gh-pages; then
      git update-ref -d refs/heads/gh-pages || return 1
    fi
    return 0
  fi
  [ "${rc}" -eq 0 ] || return 1
  git fetch origin gh-pages:refs/remotes/origin/gh-pages || return 1
  git update-ref refs/heads/gh-pages refs/remotes/origin/gh-pages
}

# pages_publish <function>: runs <function> until it succeeds, at most
# PAGES_MAX_ATTEMPTS times. <function> starts with pages_fetch, finds its
# version again, builds onto the local gh-pages and pushes it. It returns 0
# when it is done, 2 when the push was refused, and 1 on any other error.
# It runs inside `||`, so set -e is off there: each step needs `|| return 1`.
pages_publish() {
  local attempt=1 rc
  while true; do
    rc=0
    "$1" || rc=$?
    if [ "${rc}" -ne 2 ]; then
      return "${rc}"
    fi
    if [ "${attempt}" -ge "${PAGES_MAX_ATTEMPTS}" ]; then
      echo "The push to gh-pages was refused ${PAGES_MAX_ATTEMPTS} times — giving up." >&2
      return 1
    fi
    echo "The push to gh-pages was refused (attempt ${attempt} of ${PAGES_MAX_ATTEMPTS}) — fetching it again." >&2
    attempt=$((attempt + 1))
  done
}
