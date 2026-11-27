#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# Checks one runner of the second runner site. The result decides if CI may
# use the site, so it fails on every doubt.
#
# Usage: second_site_smoke.sh --image <image> <url>...
#   --image  a public test image; `docker pull` must get it (on the site it
#            comes through the site's own Docker Hub cache)
#   <url>    the URLs this runner must reach. Name a file that exists: the
#            check needs a 2xx answer, after redirects. An error answer such
#            as a 502 from a proxy does not count as reached.
#
# The runner must have MEHO_CI_SITE=site2 in its pod env. It proves the job
# reached the second site.
#
# The addresses this runner must NOT reach come from the private pod env
# MEHO_CI_DENY_PROBE_HOSTS, a list of host or host:port. A comma or a line
# break ends an entry, so a list that wraps onto a new line is read whole. Each
# one must end in a curl timeout (exit code 28). Every other result fails the
# check: a success, a refused connection or a certificate error means the
# packets got through, and any other error means the check proved nothing.
# curl also ends with 28 when a name lookup times out. No connection was tried
# then, so a timeout counts only after the name was resolved.
#
# The logs of this public repository are public. So each address is masked
# with ::add-mask:: before it is used, and only its position and the exit code
# are printed, never the address. The output of `docker pull` is dropped for
# the same reason: an error text can name the cache.
set -uo pipefail

connect_seconds=5
deny_max_seconds=10
reach_max_seconds=30

fail() {
  echo "::error title=Second runner site smoke::${1}"
  exit 1
}

# True for a curl time of zero, which means that step never finished. Also
# true for an empty value.
is_zero() {
  [[ "${1}" =~ ^0*(\.0*)?$ ]]
}

if [[ "${MEHO_CI_SITE:-}" != "site2" ]]; then
  fail "MEHO_CI_SITE is not 'site2' on this runner, so this job did not reach the second site."
fi
image=""
if [[ "${1:-}" == "--image" ]]; then
  image="${2:-}"
  shift 2 || shift $#
fi
if [[ ! "${image}" =~ ^docker\.io/[A-Za-z0-9._/-]+(:[A-Za-z0-9._-]+|@sha256:[0-9a-f]{64})$ ]]; then
  fail "No public docker.io test image to pull was given (--image)."
fi
if [[ $# -eq 0 ]]; then
  fail "No URL to reach was given."
fi
if ! command -v curl >/dev/null 2>&1; then
  fail "curl is not installed on this runner."
fi
if ! command -v docker >/dev/null 2>&1; then
  fail "docker is not installed on this runner."
fi

# `read` stops at the first line break, so every line break becomes a comma
# first. Otherwise the entries after it would never be checked.
deny_list="${MEHO_CI_DENY_PROBE_HOSTS:-}"
deny_list="${deny_list//$'\r'/,}"
deny_list="${deny_list//$'\n'/,}"
IFS=',' read -ra raw_entries <<<"${deny_list}"
deny_urls=()
position=0
for entry in ${raw_entries[@]+"${raw_entries[@]}"}; do
  entry="${entry//[[:space:]]/}"
  [[ -n "${entry}" ]] || continue
  position=$(( position + 1 ))
  colons="${entry//[^:]/}"
  echo "::add-mask::${entry}"
  if [[ "${#colons}" -eq 1 ]]; then
    echo "::add-mask::${entry%:*}"
  fi
  # The shape check matters: the entry becomes part of a URL.
  if [[ ! "${entry}" =~ ^[A-Za-z0-9._:-]+$ ]]; then
    fail "Entry ${position} of MEHO_CI_DENY_PROBE_HOSTS is not a host or host:port."
  fi
  if [[ "${#colons}" -eq 1 && ! "${entry##*:}" =~ ^[0-9]+$ ]]; then
    fail "Entry ${position} of MEHO_CI_DENY_PROBE_HOSTS has a port that is not a number."
  fi
  if [[ "${#colons}" -gt 1 ]]; then
    # An IPv6 address needs brackets in a URL.
    deny_urls+=("https://[${entry}]/")
  else
    deny_urls+=("https://${entry}/")
  fi
done
total="${#deny_urls[@]}"
if [[ "${total}" -eq 0 ]]; then
  fail "MEHO_CI_DENY_PROBE_HOSTS is empty, so this check would prove nothing."
fi

for url in "$@"; do
  # Only a 2xx answer counts. A proxy that cannot reach the server answers
  # with an error status (for example 502), and curl still ends with 0.
  status="$(curl --silent --head --location --connect-timeout "${connect_seconds}" \
    --max-time "${reach_max_seconds}" --output /dev/null --write-out '%{http_code}' \
    "${url}" 2>/dev/null)"
  code=$?
  if [[ "${code}" -ne 0 ]]; then
    fail "Cannot reach ${url} (curl exit code ${code}). Unit test shards need it, so this site must not count as online."
  fi
  if [[ ! "${status}" =~ ^2[0-9][0-9]$ ]]; then
    fail "Cannot reach ${url} (HTTP status ${status:-none}). Unit test shards need it, so this site must not count as online."
  fi
  echo "reached ${url} (HTTP status ${status})"
done

if docker pull --quiet "${image}" >/dev/null 2>&1; then
  echo "pulled ${image}"
else
  fail "docker pull ${image} failed (exit code $?). Unit test shards pull test images, so this site must not count as online."
fi

bad=0
for (( i = 0; i < total; i++ )); do
  # No --insecure: a certificate error must count as "got through".
  # --noproxy: this checks the runner's own network path.
  # stderr is dropped because curl's error text can hold the address.
  timings="$(curl --silent --noproxy '*' --connect-timeout "${connect_seconds}" \
    --max-time "${deny_max_seconds}" --output /dev/null \
    --write-out '%{time_namelookup} %{time_connect}' "${deny_urls[$i]}" 2>/dev/null)"
  code=$?
  read -r resolved connected <<<"${timings}"
  # An IP address also shows a lookup time above zero. Measured on curl 7.81
  # and 8.18: only a lookup that never finished shows zero.
  if [[ "${code}" -ne 28 ]]; then
    why="Only a timeout (28) shows this site cannot reach it."
  elif is_zero "${resolved}"; then
    why="The name was never resolved, so no connection was tried."
  elif ! is_zero "${connected}"; then
    # curl also ends with 28 when a host accepts and then says nothing.
    why="The connection was made, so the packets got through."
  else
    echo "address $(( i + 1 )) of ${total}: exit code ${code}"
    continue
  fi
  echo "::error title=Second runner site smoke::Address $(( i + 1 )) of ${total}: exit code ${code}. ${why}"
  bad=1
done

if [[ "${bad}" -ne 0 ]]; then
  exit 1
fi
echo "Second runner site smoke passed: ${#} URL(s) reached, 1 test image pulled, ${total} address(es) timed out."
