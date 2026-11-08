#!/usr/bin/env bash
# Checks one runner of the second pool. The result decides if CI may use the
# pool, so it fails on every doubt.
#
# Usage: extra_runner_smoke.sh <url>...
#   The URLs this runner must reach.
#
# The addresses this runner must NOT reach come from the private pod env
# MEHO_CI_DENY_PROBE_HOSTS, a comma-separated list of host or host:port. Each
# one must end in a curl timeout (exit code 28). Every other result fails the
# check: a success, a refused connection or a certificate error means the
# packets got through, and any other error means the check proved nothing.
#
# The logs of this public repository are public. So each address is masked
# with ::add-mask:: before it is used, and only its position and the exit code
# are printed, never the address.
set -uo pipefail

connect_seconds=5
deny_max_seconds=10
reach_max_seconds=30

fail() {
  echo "::error title=Second runner pool smoke::${1}"
  exit 1
}

if [[ "${MEHO_CI_SITE:-}" != "extra" ]]; then
  fail "MEHO_CI_SITE is not 'extra' on this runner, so the test jobs would use the private image names here."
fi
if [[ $# -eq 0 ]]; then
  fail "No URL to reach was given."
fi
if ! command -v curl >/dev/null 2>&1; then
  fail "curl is not installed on this runner."
fi

IFS=',' read -ra raw_entries <<<"${MEHO_CI_DENY_PROBE_HOSTS:-}"
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
  if curl --silent --head --connect-timeout "${connect_seconds}" --max-time "${reach_max_seconds}" \
    --output /dev/null "${url}" 2>/dev/null; then
    echo "reached ${url}"
  else
    fail "Cannot reach ${url} (curl exit code $?). Unit test shards need it, so this pool must not count as online."
  fi
done

bad=0
for (( i = 0; i < total; i++ )); do
  # No --insecure: a certificate error must count as "got through".
  # --noproxy: this checks the runner's own network path.
  # stderr is dropped because curl's error text can hold the address.
  connected="$(curl --silent --noproxy '*' --connect-timeout "${connect_seconds}" \
    --max-time "${deny_max_seconds}" --output /dev/null --write-out '%{time_connect}' \
    "${deny_urls[$i]}" 2>/dev/null)"
  code=$?
  # A timeout after the TCP connection was made still means the packets got
  # through: curl also ends with 28 when a host accepts and then says nothing.
  if [[ "${code}" -eq 28 && "${connected}" =~ ^0*(\.0*)?$ ]]; then
    echo "address $(( i + 1 )) of ${total}: exit code ${code}"
  else
    echo "::error title=Second runner pool smoke::Address $(( i + 1 )) of ${total}: exit code ${code}. Only a timeout (28) shows this pool cannot reach it."
    bad=1
  fi
done

if [[ "${bad}" -ne 0 ]]; then
  exit 1
fi
echo "Second runner pool smoke passed: ${#} URL(s) reached, ${total} address(es) timed out."
