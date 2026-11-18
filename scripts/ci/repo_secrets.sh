#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# Checks the repository secrets a CI job reads, hides the parts of their
# values in the rest of the job log, and sets the test image names.
#
# The secrets hold internal names, and the run logs of a public
# repository can be read by everyone. They are secrets, not repository
# variables, because GitHub shows a secret as `***` in every log line,
# also in the `env:` it prints before a step runs. A variable shows in
# clear there, before any `::add-mask::` can hide it. This script adds
# masks only for the parts of a value (the proxy host, the shelf name
# without its owner) that GitHub does not hide on its own.
#
# Usage:
#   repo_secrets.sh NAME... < image-lines
#
#   NAME        a secret this job needs (CI_IMAGE_PROXY,
#               SPEC_SHELF_REPOSITORY). Its value is in the environment
#               variable of the same name.
#   image-lines one line per image: `ENV_NAME: PROXY/<image>`. The
#               script writes ENV_NAME=<CI_IMAGE_PROXY>/<image> to
#               $GITHUB_ENV. Without CI_IMAGE_PROXY it writes
#               docker.io/<image>. A line with `PROXY_ONLY/<image>` gets an
#               empty value instead (the test that reads it then skips).
#
# REQUIRE_SECRETS=true (set in this repository) makes an empty secret
# fail the job, so no test skips quietly. In another repository (a fork
# that runs the workflows itself) an empty secret is fine, and the
# images come from Docker Hub.

set -euo pipefail

fail() {
  echo "::error title=Repository secret::$*"
  exit 1
}

missing=0
for name in "$@"; do
  value="${!name:-}"
  if [ -z "$value" ]; then
    if [ "${REQUIRE_SECRETS:-}" = "true" ]; then
      echo "::error title=Repository secret missing::The repository secret ${name} is empty. CI in this repository needs it. Set it under Settings > Secrets and variables > Actions > Secrets."
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
