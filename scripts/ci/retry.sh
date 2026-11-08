#!/usr/bin/env bash
# Run a command for a CI job, and run it again when it fails.
#
# Usage: retry.sh <command> [<argument>...]
#
# It tries three times and waits longer after each failure (5 s, then
# 15 s). Use it for a command that downloads something, for example a
# model. For a plain file download use fetch_with_retry.sh.
set -uo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: $0 <command> [<argument>...]" >&2
  exit 2
fi

pauses=(5 15)
tries=$((${#pauses[@]} + 1))

for ((try = 1; try <= tries; try++)); do
  "$@" && exit 0
  status=$?
  if ((try < tries)); then
    pause="${pauses[$((try - 1))]}"
    echo "::warning::'$1' failed (try ${try} of ${tries}). Trying again in ${pause} s."
    sleep "${pause}"
  fi
done

echo "::error::'$1' failed after ${tries} tries."
exit "${status}"
