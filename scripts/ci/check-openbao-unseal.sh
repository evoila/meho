#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# Cluster check of the built-in OpenBao's unseal helper (#4046).
#
# Run it against a release installed with `coreVault.enabled=true` whose
# OpenBao is set up: the Secret `<release>-core-vault-unseal` exists and the
# pod `<release>-vault-0` is ready. Run it once after the install and once
# more after a `helm upgrade`. It checks, in this order:
#
#   1. Self-unseal: a deleted pod is ready again within 2 minutes.
#   2. Keyholder mode: Secret deleted and `rollout restart`: the pod stays
#      sealed, and the helper says so in its log.
#   3. Keyholder to self: Secret back and `rollout restart`: ready again.
#   4. The helper keeps no key: Secret deleted, the server process stopped
#      inside its container (not the pod): the server comes back sealed and
#      stays sealed.
#   5. Secret back: the helper unseals the same pod, with no restart.
#   6. Before and after all of that: the key is in no pod log of the namespace
#      (Job output included), and not in `helm get notes`, `helm get values
#      --all` or `helm get manifest`.
#
# The Secret is put back before the script ends, also when a check fails.
# The backplane's `/ready` ("vault: sealed") is checked by the CI job that
# installs the full core (#4051); this script checks the OpenBao pod.
#
# The key stays in this script's memory: it is never printed, never passed
# on a command line and never written to a file.
#
# Usage: check-openbao-unseal.sh <namespace> <release>
# Needs kubectl, helm and jq (1.6+); KUBECONFIG as usual.
#
# Exit codes: 0 every check passed; 1 a check failed (the message names it).

set -euo pipefail

NS=${1:?usage: check-openbao-unseal.sh <namespace> <release>}
REL=${2:?usage: check-openbao-unseal.sh <namespace> <release>}
STS="$REL-vault"
POD="$STS-0"
SECRET="$REL-core-vault-unseal"

k() { kubectl -n "$NS" "$@"; }
fail() {
  echo "FAIL: $*" >&2
  exit 1
}

SAVED=$(k get secret "$SECRET" -o json) || fail "no Secret $SECRET: set OpenBao up first"
KEY_B64=$(jq -r '.data.key' <<<"$SAVED")
KEY=$(jq -r '.data.key | @base64d' <<<"$SAVED")
[ -n "$KEY" ] || fail "the Secret $SECRET has no key"
# The server container is named after the program (`bao` or `vault`).
BIN=$(k get pod "$POD" -o jsonpath='{.spec.containers[0].name}')

restore_secret() {
  k get secret "$SECRET" >/dev/null 2>&1 && return 0
  jq 'del(.metadata.resourceVersion, .metadata.uid, .metadata.creationTimestamp, .metadata.managedFields)' \
    <<<"$SAVED" | k create -f - >/dev/null
}
trap restore_secret EXIT

# wait_until <seconds> <command...>: retry every 3 s until the command succeeds.
wait_until() {
  local deadline=$((SECONDS + $1))
  shift
  until "$@"; do
    ((SECONDS < deadline)) || return 1
    sleep 3
  done
}
uid() { k get pod "$POD" -o jsonpath='{.metadata.uid}' 2>/dev/null || true; }
ready() { [ "$(k get pod "$POD" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)" = True ]; }
# `status` exits 0 when unsealed, 2 when sealed, 1 when the server does not answer.
seal_rc() {
  local rc=0
  k exec "$POD" -c unseal -- "$BIN" status >/dev/null 2>&1 || rc=$?
  echo "$rc"
}
answers() { [ "$(seal_rc)" != 1 ]; }
new_pod() { local u; u=$(uid); [ -n "$u" ] && [ "$u" != "$OLD_UID" ]; }
new_and_ready() { new_pod && ready; }
new_and_answers() { new_pod && answers; }
server_restarts() { k get pod "$POD" -o jsonpath="{.status.containerStatuses[?(@.name==\"$BIN\")].restartCount}"; }
restarted_and_answers() { [ "$(server_restarts)" -gt "$RESTARTS" ] && answers; }
key_file_gone() { ! k exec "$POD" -c unseal -- test -e /unseal/key 2>/dev/null; }
# stays_sealed <seconds> <what>: sealed and not ready on every look, every 5 s.
stays_sealed() {
  local end=$((SECONDS + $1))
  while ((SECONDS < end)); do
    [ "$(seal_rc)" = 2 ] || fail "$2: the server is not sealed"
    ! ready || fail "$2: the pod is ready"
    sleep 5
  done
}
# Pure bash, so the key never lands on a command line.
has_key() { [[ $1 == *"$KEY"* || $1 == *"$KEY_B64"* ]]; }

no_key_anywhere() {
  local pod c text what
  for pod in $(k get pods -o name); do
    for c in $(k get "$pod" -o jsonpath='{.spec.initContainers[*].name} {.spec.containers[*].name}'); do
      text="$(k logs "$pod" -c "$c" 2>/dev/null || true)$(k logs "$pod" -c "$c" --previous 2>/dev/null || true)"
      if has_key "$text"; then fail "$1: the key is in the log of $pod, container $c"; fi
    done
  done
  for what in notes "values --all" manifest; do
    # shellcheck disable=SC2086  # $what is a subcommand and maybe one flag
    text=$(helm -n "$NS" get $what "$REL")
    if has_key "$text"; then fail "$1: the key is in helm get $what"; fi
  done
  echo "ok: $1: the key is in no pod log and not in helm get notes, values or manifest"
}

ready || fail "$POD is not ready: set OpenBao up and unseal it first"
no_key_anywhere "before"

echo "1. self-unseal: delete the pod, expect it ready within 2 minutes"
OLD_UID=$(uid)
start=$SECONDS
k delete pod "$POD" --wait=true >/dev/null
wait_until $((120 - (SECONDS - start))) new_and_ready || fail "the new pod is not ready within 2 minutes"
echo "ok: ready $((SECONDS - start)) s after the delete"

echo "2. keyholder mode: delete the Secret, rollout restart, expect it to stay sealed"
k delete secret "$SECRET" >/dev/null
OLD_UID=$(uid)
k rollout restart statefulset "$STS" >/dev/null
wait_until 180 new_and_answers || fail "the restarted server does not answer within 3 minutes"
stays_sealed 30 "keyholder mode"
k logs "$POD" -c unseal | grep -q "keyholder mode" || fail "the helper's log does not say keyholder mode"
echo "ok: sealed for 30 s, the pod is not ready"

echo "3. keyholder to self: put the Secret back, rollout restart, expect it ready"
restore_secret
OLD_UID=$(uid)
k rollout restart statefulset "$STS" >/dev/null
wait_until 180 new_and_ready || fail "the restarted pod is not ready within 3 minutes"
echo "ok: ready"

echo "4. the helper keeps no key: delete the Secret, stop the server process, expect sealed"
k delete secret "$SECRET" >/dev/null
wait_until 180 key_file_gone || fail "the kubelet did not remove /unseal/key within 3 minutes"
RESTARTS=$(server_restarts)
# The server is PID 1 of its container (the image's start script ends with `exec`).
k exec "$POD" -c "$BIN" -- sh -c 'kill 1'
wait_until 120 restarted_and_answers || fail "the server did not come back within 2 minutes"
stays_sealed 30 "after the server process restart"
echo "ok: the server came back sealed and stayed sealed for 30 s"

echo "5. put the Secret back, expect the helper to unseal the same pod"
OLD_UID=$(uid)
restore_secret
wait_until 180 ready || fail "not ready within 3 minutes after the Secret came back"
[ "$(uid)" = "$OLD_UID" ] || fail "the pod was replaced"
echo "ok: ready, same pod"

no_key_anywhere "after"
echo "PASS"
