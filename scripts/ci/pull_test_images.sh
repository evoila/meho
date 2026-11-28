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
# Each image gets at most 3 tries, 10 s apart. The pull runs quiet and
# its output is thrown away: the logs of this repository are public, and
# an error of Docker can name a host. The script prints only the image
# name, the exit code and a line when it tries again. When an image did
# not pull after 3 tries, the script passes only if the image is already
# on the runner. Otherwise it fails with a clear text.
set -uo pipefail

tries=3
pause=10

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
  pulled=0
  for ((try = 1; try <= tries; try++)); do
    docker pull --quiet "$image" >/dev/null 2>&1
    status=$?
    if [[ $status -eq 0 ]]; then
      echo "Pulled ${image} (try ${try} of ${tries})."
      pulled=1
      break
    fi
    echo "Pull of ${image} failed with exit code ${status} (try ${try} of ${tries})."
    if ((try < tries)); then
      echo "::warning title=Test image::Trying the pull of ${image} again in ${pause} s."
      sleep "$pause"
    fi
  done
  ((pulled)) && continue
  if docker image inspect "$image" >/dev/null 2>&1; then
    echo "::warning title=Test image::${image} did not pull after ${tries} tries. The copy on the runner is used."
  else
    echo "::error title=Test image::SETUP FAILURE, not a test failure: the test image ${image} did not pull after ${tries} tries and is not on the runner."
    failed=1
  fi
done

exit "$failed"
