#!/usr/bin/env bash
# Download one file for a CI job, and try again when it fails.
#
# Usage: fetch_with_retry.sh <url> <output-file> [<sha256>]
#
# It tries three times and waits longer after each failure (5 s, then
# 15 s). A try fails when the download fails or, if a sha256 is given,
# when the file does not match it. A half-sent file (for example the body
# of an HTTP 504) then costs one try, not the whole run.
set -euo pipefail

if [[ $# -lt 2 || $# -gt 3 ]]; then
  echo "usage: $0 <url> <output-file> [<sha256>]" >&2
  exit 2
fi

url="$1"
out="$2"
sha="${3:-}"
pauses=(5 15)
tries=$((${#pauses[@]} + 1))

for ((try = 1; try <= tries; try++)); do
  if curl -fsSL --connect-timeout 20 --max-time 300 -o "${out}" "${url}" \
     && { [[ -z "${sha}" ]] || echo "${sha}  ${out}" | sha256sum -c - >/dev/null; }; then
    exit 0
  fi
  rm -f "${out}"
  if ((try < tries)); then
    pause="${pauses[$((try - 1))]}"
    echo "::warning::Download of ${url} failed (try ${try} of ${tries}). Trying again in ${pause} s."
    sleep "${pause}"
  fi
done

echo "::error::Download of ${url} failed after ${tries} tries."
exit 1
