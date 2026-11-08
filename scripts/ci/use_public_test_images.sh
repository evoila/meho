#!/usr/bin/env bash
# First step of every CI job that sets MEHO_TEST_*_IMAGE.
#
# A runner of the second pool cannot reach the private image mirror. On such
# a runner (the pod has MEHO_CI_SITE=extra) this script writes the public
# Docker Hub name of each test image to $GITHUB_ENV, and the later steps of
# the job use it. On every other runner it changes nothing.
#
# Why a step and not an `if:` in the workflow: MEHO_CI_SITE is set on the
# runner pod, so it is only known while a step runs. A write to $GITHUB_ENV
# replaces the job-level `env:` value for the later steps.
#
# The public name is the private name without its first two parts
# (`<registry>/<project>/`). So the tag or digest stays pinned in one place,
# the `env:` line of the job. Keycloak needs no special case: the line in the
# job gives this step its name too.
set -euo pipefail

if [[ "${MEHO_CI_SITE:-}" != "extra" ]]; then
  echo "Not the second runner pool: the test images stay as the job sets them."
  exit 0
fi

while read -r name; do
  [[ "${name}" =~ ^MEHO_TEST_[A-Z0-9_]+_IMAGE$ ]] || continue
  value="${!name}"
  # A name whose first part has no dot or colon has no registry in front.
  # It is a public Docker Hub name already.
  if [[ "${value%%/*}" != *[.:]* ]]; then
    echo "${name}: already a public name"
    continue
  fi
  public="${value#*/*/}"
  if [[ "${public}" == "${value}" ]]; then
    echo "::error title=Second runner pool::${name} has no project part, so its public name is not known."
    exit 1
  fi
  printf '%s=%s\n' "${name}" "${public}" >> "${GITHUB_ENV}"
  echo "${name}: using the public image ${public}"
done < <(compgen -e | sort)
