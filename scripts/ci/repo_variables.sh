#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# Checks the repository variables a CI job reads, hides their values in
# the rest of the job log, and sets the test image names.
#
# The variables hold internal names, and the run logs of a public
# repository can be read by everyone. GitHub prints each step's `env:` in
# the log, so the workflow passes the values only to the step that runs
# this script. From here on `::add-mask::` shows them as `***`.
#
# Usage:
#   repo_variables.sh NAME... < image-lines
#
#   NAME        a variable this job needs (CI_IMAGE_PROXY,
#               SPEC_SHELF_REPOSITORY). Its value is in the environment
#               variable of the same name.
#   image-lines one line per test image: `ENV_NAME: PROXY/<image>`. The
#               script writes ENV_NAME=<CI_IMAGE_PROXY>/<image> to
#               $GITHUB_ENV. Without CI_IMAGE_PROXY it writes
#               docker.io/<image>. A line with `PROXY_ONLY/<image>` gets an
#               empty value instead (the test that reads it then skips).
#
# REQUIRE_VARIABLES=true (set in this repository) makes an empty variable
# fail the job, so no test skips quietly. In another repository (a fork
# that runs the workflows itself) an empty variable is fine, and the
# images come from Docker Hub.

set -euo pipefail

fail() {
  echo "::error title=Repository variable::$*"
  exit 1
}

missing=0
for name in "$@"; do
  value="${!name:-}"
  if [ -z "$value" ]; then
    if [ "${REQUIRE_VARIABLES:-}" = "true" ]; then
      echo "::error title=Repository variable missing::The repository variable ${name} is empty. CI in this repository needs it. Set it under Settings > Secrets and variables > Actions > Variables."
      missing=1
    fi
    continue
  fi
  echo "::add-mask::${value}"
done
[ "$missing" -eq 0 ] || exit 1

proxy="${CI_IMAGE_PROXY:-}"
if [ -n "$proxy" ]; then
  host="${proxy%%/*}"
  project="${proxy#*/}"
  if [ "$proxy" = "$host" ] || [ -z "$host" ] || [ -z "$project" ]; then
    fail "CI_IMAGE_PROXY must have the form <host>/<project>."
  fi
  echo "::add-mask::${host}"
fi
# The spec shelf repository name can show without its owner.
shelf="${SPEC_SHELF_REPOSITORY:-}"
if [ -n "$shelf" ] && [ "$shelf" != "${shelf#*/}" ]; then
  echo "::add-mask::${shelf#*/}"
fi

while IFS= read -r line; do
  [ -n "${line// /}" ] || continue
  key="${line%%:*}"
  image="${line#*:}"
  image="${image# }"
  case "$image" in
    PROXY/*) path="${image#PROXY/}" fallback="docker.io/${path}" ;;
    PROXY_ONLY/*) path="${image#PROXY_ONLY/}" fallback="" ;;
    *) fail "bad image line (want 'NAME: PROXY/<image>'): ${line}" ;;
  esac
  if [ -n "$proxy" ]; then
    echo "${key}=${proxy}/${path}" >> "$GITHUB_ENV"
  else
    echo "${key}=${fallback}" >> "$GITHUB_ENV"
  fi
done
