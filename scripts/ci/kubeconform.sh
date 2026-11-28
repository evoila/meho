#!/usr/bin/env bash
# Run kubeconform with a local folder of Kubernetes schemas.
#
# Usage: kubeconform.sh <kubeconform argument>...
#
# kubeconform downloads the JSON schema of each kind it checks from an
# outside server. With -cache it keeps each schema it downloads in a
# folder and reads it from there next time. The CI job restores and saves
# that folder with actions/cache, so a run normally downloads nothing.
#
# The folder is KUBECONFORM_CACHE (default ~/.cache/meho-kubeconform-schemas).
# When a schema download fails, the whole check runs again: three tries at
# most, with 5 s and then 15 s between them (KUBECONFORM_PAUSES changes
# them). The log says each time it tries again. Any other failure is a
# finding in the manifests and fails at once.
set -uo pipefail

cache="${KUBECONFORM_CACHE:-${HOME}/.cache/meho-kubeconform-schemas}"
mkdir -p "${cache}"
# shellcheck disable=SC2206
pauses=(${KUBECONFORM_PAUSES:-5 15})
tries=$((${#pauses[@]} + 1))

for ((try = 1; try <= tries; try++)); do
  out="$(kubeconform -cache "${cache}" "$@" 2>&1)"
  status=$?
  printf '%s\n' "${out}"
  if ((status == 0)); then
    exit 0
  fi
  if ! grep -q 'failed downloading schema' <<<"${out}"; then
    exit "${status}"
  fi
  if ((try < tries)); then
    pause="${pauses[$((try - 1))]}"
    echo "::warning::kubeconform could not download a schema (try ${try} of ${tries}). Trying again in ${pause} s."
    sleep "${pause}"
  fi
done

echo "::error::kubeconform could not download a schema after ${tries} tries."
exit "${status}"
