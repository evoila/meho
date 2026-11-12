#!/usr/bin/env bash
# First step of each CI job that can run on the second runner pool.
#
# A runner of that pool cannot reach the private image mirror. On such a
# runner (the pod has MEHO_CI_SITE=extra) this script writes the public
# docker.io name of each MEHO_TEST_*_IMAGE to $GITHUB_ENV, and the later steps
# of the job use it. On every other runner it changes nothing.
#
# Why a step and not an `if:` in the workflow: MEHO_CI_SITE is set on the
# runner pod, so it is only known while a step runs. A write to $GITHUB_ENV
# replaces the job-level `env:` value for the later steps.
#
# The public name is `docker.io/` plus the private name without its first two
# parts (`<registry>/<project>/`). So the tag or digest stays pinned in one
# place, the `env:` line of the job. A job with a Keycloak line needs no
# special case if it moves to this pool later.
set -euo pipefail

if [[ "${MEHO_CI_SITE:-}" != "extra" ]]; then
  echo "Not the second runner pool: the test images stay as the job sets them."
  exit 0
fi

while read -r name; do
  [[ "${name}" =~ ^MEHO_TEST_[A-Z0-9_]+_IMAGE$ ]] || continue
  value="${!name}"
  # A first part with no dot or colon is not a registry: a short Docker Hub
  # name, which the Docker daemon reads as docker.io.
  first="${value%%/*}"
  if [[ "${first}" == "docker.io" || "${first}" != *[.:]* ]]; then
    echo "${name}: already a public name"
    continue
  fi
  rest="${value#*/*/}"
  if [[ "${rest}" == "${value}" ]]; then
    echo "::error title=Second runner pool::${name} has no project part, so its public name is not known."
    exit 1
  fi
  printf '%s=docker.io/%s\n' "${name}" "${rest}" >> "${GITHUB_ENV}"
  echo "${name}: using the public image docker.io/${rest}"
done < <(compgen -e | sort)

# The Docker Hub login step goes straight to registry-1.docker.io and
# auth.docker.io, even when the Docker daemon has a registry mirror. This
# pool pulls through its own Docker Hub cache and needs no login, and a host
# it cannot reach must not fail the job. The step skips when this is empty.
echo "DOCKERHUB_USERNAME=" >> "${GITHUB_ENV}"
echo "The Docker Hub login step is skipped on this pool."
