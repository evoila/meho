#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# Pull the test images of a CI job before pytest starts, so a short
# break of the image registry does not make the tests fail or skip.
#
# Usage: pull_test_images.sh [<image>...]
#
# The images are the arguments, plus the non-empty values of the job's
# MEHO_TEST_*_IMAGE and RYUK_CONTAINER_IMAGE variables (set by
# repo_secrets.sh). When MEHO_TEST_K3S_TAGS is set, the image name of
# MEHO_TEST_K3S_IMAGE with each of its tags is pulled too, the same way
# tests/integration/test_connectors_k8s_k3d.py picks its images.
#
# The script looks on the runner first: an image that is there is used,
# with no pull. Only a missing image is pulled: at most 3 tries, 10 s
# apart. Each pull may take at most PULL_TIME_LIMIT seconds. The job sets
# this value, so that the 3 tries and their 2 pauses fit into the time
# limit of its step. The pull runs quiet and its output is thrown away:
# the logs of this repository are public, and an error of Docker can name
# a host. The script prints only the image name, the exit code and a line
# when it tries again. When an image is still missing after 3 tries, the
# script fails with a clear text.
set -uo pipefail

tries=3
pause=10
limit="${PULL_TIME_LIMIT:-}"
if [[ ! "$limit" =~ ^[1-9][0-9]*$ ]]; then
  echo "::error title=Test images::SETUP FAILURE, not a test failure: PULL_TIME_LIMIT must be the time limit of one pull in seconds."
  exit 1
fi

images=("$@")
while IFS= read -r name; do
  case "$name" in
    MEHO_TEST_*_IMAGE | RYUK_CONTAINER_IMAGE) images+=("${!name}") ;;
  esac
done < <(compgen -e | LC_ALL=C sort)

if [[ -n "${MEHO_TEST_K3S_TAGS:-}" && -n "${MEHO_TEST_K3S_IMAGE:-}" ]]; then
  k3s="${MEHO_TEST_K3S_IMAGE}"
  repo="${k3s%:*}"
  # No tag, or the last colon is a registry port.
  if [[ "$repo" == "$k3s" || "${k3s##*:}" == */* ]]; then
    repo="$k3s"
  fi
  IFS=',' read -r -a tags <<<"${MEHO_TEST_K3S_TAGS}"
  for tag in "${tags[@]}"; do
    tag="${tag// /}"
    [[ -n "$tag" ]] && images+=("${repo}:${tag}")
  done
fi

declare -A seen=()
unique=()
for image in "${images[@]}"; do
  [[ -n "$image" && -z "${seen[$image]:-}" ]] || continue
  seen[$image]=1
  unique+=("$image")
done

if [[ ${#unique[@]} -eq 0 ]]; then
  echo "::error title=Test images::SETUP FAILURE, not a test failure: this job names no test image to pull."
  exit 1
fi

failed=0
for image in "${unique[@]}"; do
  if docker image inspect "$image" >/dev/null 2>&1; then
    echo "${image} is on the runner. The copy on the runner is used, with no pull."
    continue
  fi
  done_ok=0
  for ((try = 1; try <= tries; try++)); do
    timeout "$limit" docker pull --quiet "$image" >/dev/null 2>&1
    status=$?
    if [[ $status -eq 0 ]]; then
      echo "Pulled ${image} (try ${try} of ${tries})."
      done_ok=1
      break
    fi
    echo "Pull of ${image} failed with exit code ${status} (try ${try} of ${tries})."
    if ((try < tries)); then
      echo "::warning title=Test image::Trying the pull of ${image} again in ${pause} s."
      sleep "$pause"
    fi
  done
  ((done_ok)) && continue
  echo "::error title=Test image::SETUP FAILURE, not a test failure: the test image ${image} did not pull after ${tries} tries and is not on the runner."
  failed=1
done

exit "$failed"
