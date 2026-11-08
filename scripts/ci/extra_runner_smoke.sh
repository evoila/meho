#!/usr/bin/env bash
# Checks one runner of the second pool. The result decides if CI may use the
# pool, so it fails on every doubt.
#
# Usage: extra_runner_smoke.sh <host[:port]>...
#   The hosts this runner must reach (port 443 when no port is given).
#
# The addresses this runner must NOT reach come from the private pod env
# MEHO_CI_DENY_PROBE_HOSTS, as a comma-separated list of host or host:port.
# The logs of this public repository are public, so this script NEVER prints
# one of those addresses: it counts them, and bash's own error text, which
# holds the address, stays in a variable.
#
# Only bash and `timeout`: the runner image is small. The check that must
# work runs first with the same probe, so a broken probe cannot look like a
# blocked address.
set -uo pipefail
export LC_ALL=C

probe_seconds=5
default_ports=(443 80 22)

fail() {
  echo "::error title=Second runner pool smoke::${1}"
  exit 1
}

# Prints open, refused or no-answer.
probe() {
  local out
  # shellcheck disable=SC2016  # The quotes are right: the inner bash expands $0 and $1.
  if out="$(timeout "${probe_seconds}" bash -c 'exec 3<>"/dev/tcp/$0/$1"' "$1" "$2" 2>&1)"; then
    echo open
  elif [[ "${out}" == *"Connection refused"* ]]; then
    echo refused
  else
    echo no-answer
  fi
}

if [[ "${MEHO_CI_SITE:-}" != "extra" ]]; then
  fail "MEHO_CI_SITE is not 'extra' on this runner, so the test jobs would use the private image names here."
fi
if [[ $# -eq 0 ]]; then
  fail "No host to reach was given."
fi

IFS=',' read -ra raw_entries <<<"${MEHO_CI_DENY_PROBE_HOSTS:-}"
deny_hosts=()
deny_ports=()
position=0
for entry in ${raw_entries[@]+"${raw_entries[@]}"}; do
  entry="${entry//[[:space:]]/}"
  [[ -n "${entry}" ]] || continue
  position=$(( position + 1 ))
  # The shape check matters: a `/` would make bash open a plain file instead
  # of a network address, and that failure would look like "blocked".
  if [[ ! "${entry}" =~ ^[A-Za-z0-9._:-]+$ ]]; then
    fail "Entry ${position} of MEHO_CI_DENY_PROBE_HOSTS is not a host or host:port."
  fi
  colons="${entry//[^:]/}"
  if [[ "${#colons}" -eq 1 ]]; then
    if [[ ! "${entry##*:}" =~ ^[0-9]+$ ]]; then
      fail "Entry ${position} of MEHO_CI_DENY_PROBE_HOSTS has a port that is not a number."
    fi
    deny_hosts+=("${entry%:*}")
    deny_ports+=("${entry##*:}")
  else
    # No colon, or an IPv6 address: no port given.
    deny_hosts+=("${entry}")
    deny_ports+=("")
  fi
done
total="${#deny_hosts[@]}"
if [[ "${total}" -eq 0 ]]; then
  fail "MEHO_CI_DENY_PROBE_HOSTS is empty, so this check would prove nothing."
fi

for target in "$@"; do
  host="${target%:*}"
  port=443
  if [[ "${target}" == *:* ]]; then
    port="${target##*:}"
  fi
  result="$(probe "${host}" "${port}")"
  if [[ "${result}" != "open" ]]; then
    fail "Cannot reach ${host} on port ${port} (${result}). Unit test shards need it, so this pool must not count as online."
  fi
  echo "reached ${host} on port ${port}"
done

bad=0
for (( i = 0; i < total; i++ )); do
  ports=("${deny_ports[$i]}")
  if [[ -z "${deny_ports[$i]}" ]]; then
    ports=("${default_ports[@]}")
  fi
  for port in "${ports[@]}"; do
    result="$(probe "${deny_hosts[$i]}" "${port}")"
    if [[ "${result}" == "no-answer" ]]; then
      echo "address $(( i + 1 )) of ${total}, port ${port}: no answer, as it should be"
    else
      # `refused` means a reply came back, so a network path exists.
      echo "::error title=Second runner pool smoke::Address $(( i + 1 )) of ${total} answered on port ${port} (${result}). This pool must not reach it."
      bad=1
    fi
  done
done

if [[ "${bad}" -ne 0 ]]; then
  exit 1
fi
echo "Second runner pool smoke passed: ${#} host(s) reached, ${total} address(es) not reached."
