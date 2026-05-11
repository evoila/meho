#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# verify-rke2-access.sh — verify the rke2-infra <-> evoila/meho handshake.
#
# Runs the kubectl half of the verification checks documented in
# docs/cross-repo/rke2-infra-coordination.md, with one assertion per
# acceptance bullet. Either side of the handshake can run it before
# declaring the contract closed.
#
# Usage:
#   KUBECONFIG=/path/to/kubeconfig ./verify-rke2-access.sh
#   ./verify-rke2-access.sh --namespace-prefix=meho-ci
#
# Exits 0 only when every check matches the expected outcome; non-zero
# on any deviation. Each check prints one line: `[ok] ...` / `[FAIL] ...`.

set -Eeuo pipefail

NAMESPACE_PREFIX="meho-ci"
KEEP_NAMESPACE="${KEEP_NAMESPACE:-0}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --namespace-prefix=*)
            NAMESPACE_PREFIX="${1#*=}"
            shift
            ;;
        --namespace-prefix)
            NAMESPACE_PREFIX="$2"
            shift 2
            ;;
        --keep-namespace)
            KEEP_NAMESPACE=1
            shift
            ;;
        -h | --help)
            sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "verify-rke2-access.sh: unknown argument: $1" >&2
            exit 2
            ;;
    esac
done

if ! command -v kubectl >/dev/null 2>&1; then
    echo "verify-rke2-access.sh: kubectl not on PATH" >&2
    exit 2
fi

# Deterministic per-run namespace name. Random suffix instead of $$ keeps
# the script safe to run from CI matrices where multiple shells share PID
# numbers across runners.
SUFFIX="$(LC_ALL=C tr -dc 'a-z0-9' </dev/urandom | head -c 8)"
VERIFY_NS="${NAMESPACE_PREFIX}-verify-${SUFFIX}"

FAIL_COUNT=0
PASS_COUNT=0

# Track whether we created the verification namespace so the trap only
# deletes when we own it (avoid clobbering a pre-existing namespace if
# the random suffix collides with one operators already created).
NS_CREATED=0

cleanup() {
    local rc=$?
    if [[ "$NS_CREATED" -eq 1 && "$KEEP_NAMESPACE" -ne 1 ]]; then
        kubectl delete namespace "$VERIFY_NS" --ignore-not-found --wait=false \
            >/dev/null 2>&1 || true
    fi
    exit "$rc"
}
trap cleanup EXIT INT TERM

check_ok() {
    local label="$1"
    PASS_COUNT=$((PASS_COUNT + 1))
    printf '[ok]   %s\n' "$label"
}

check_fail() {
    local label="$1"
    local detail="${2:-}"
    FAIL_COUNT=$((FAIL_COUNT + 1))
    printf '[FAIL] %s\n' "$label" >&2
    if [[ -n "$detail" ]]; then
        printf '       %s\n' "$detail" >&2
    fi
}

# `kubectl auth can-i` exit codes:
#   0  → allowed
#   1  → denied (or transport / parse error — distinguish via stdout)
#   2+ → CLI usage / transport error
# Stdout is the human-readable verdict (`yes` / `no`). Pin the answer to
# the exit code AND the verdict string to defend against the
# can-i-as-an-anon-user "no" -> stderr / exit 1 case.
assert_can() {
    local label="$1"
    local expected="$2" # yes | no
    shift 2

    local verdict
    set +e
    verdict="$(kubectl auth can-i "$@" 2>/dev/null)"
    local rc=$?
    set -e

    if [[ "$expected" == "yes" ]]; then
        if [[ "$rc" -eq 0 && "$verdict" == "yes" ]]; then
            check_ok "$label"
        else
            check_fail "$label" "expected yes; got verdict='$verdict' rc=$rc"
        fi
    else
        if [[ "$rc" -ne 0 && "$verdict" != "yes" ]]; then
            check_ok "$label"
        else
            check_fail "$label" "expected no; got verdict='$verdict' rc=$rc"
        fi
    fi
}

echo "verify-rke2-access.sh: checks running against:"
kubectl config current-context | sed 's/^/  context: /'
echo "  verify namespace: $VERIFY_NS"
echo

# --- 1. Identity round-trip ----------------------------------------------
# `kubectl auth whoami` is the cleanest signal that the apiserver accepts
# the current credentials. It returns the resolved identity (the OIDC sub
# in Option A; the ServiceAccount in Option B).
if kubectl auth whoami >/dev/null 2>&1; then
    check_ok "identity round-trip (kubectl auth whoami)"
    kubectl auth whoami 2>/dev/null | sed 's/^/  /'
else
    check_fail "identity round-trip (kubectl auth whoami)" \
        "kubectl could not resolve the current user against the apiserver"
fi
echo

# --- 2. The identity CAN create + use meho-ci-* namespaces --------------
if kubectl create namespace "$VERIFY_NS" >/dev/null 2>&1; then
    NS_CREATED=1
    check_ok "create namespace $VERIFY_NS"
else
    check_fail "create namespace $VERIFY_NS" \
        "expected the meho-ci-* RBAC to allow namespace create on this name"
fi

assert_can "  can list pods in $VERIFY_NS" yes \
    --namespace "$VERIFY_NS" list pods
assert_can "  can create deployments in $VERIFY_NS" yes \
    --namespace "$VERIFY_NS" create deployments.apps
assert_can "  can create services in $VERIFY_NS" yes \
    --namespace "$VERIFY_NS" create services
assert_can "  can create configmaps in $VERIFY_NS" yes \
    --namespace "$VERIFY_NS" create configmaps
assert_can "  can create secrets in $VERIFY_NS" yes \
    --namespace "$VERIFY_NS" create secrets
assert_can "  can create serviceaccounts in $VERIFY_NS" yes \
    --namespace "$VERIFY_NS" create serviceaccounts
assert_can "  can create networkpolicies in $VERIFY_NS" yes \
    --namespace "$VERIFY_NS" create networkpolicies.networking.k8s.io
assert_can "  can create ingresses in $VERIFY_NS" yes \
    --namespace "$VERIFY_NS" create ingresses.networking.k8s.io
assert_can "  can create jobs in $VERIFY_NS" yes \
    --namespace "$VERIFY_NS" create jobs.batch
assert_can "  can read pod logs in $VERIFY_NS" yes \
    --namespace "$VERIFY_NS" get pods --subresource=log
echo

# --- 3. The identity CANNOT touch non-meho-ci-* namespaces ---------------
# Pick a representative set: a regular namespace (default), the system
# namespace (kube-system), and the production-deploy target namespace
# (meho). The chart deploys MEHO into a `meho` namespace by convention
# in the consumer's prod overlay; the CI identity must never reach it.
for ns in default kube-system meho; do
    assert_can "cannot delete pods in $ns" no \
        --namespace "$ns" delete pods
    assert_can "cannot create deployments in $ns" no \
        --namespace "$ns" create deployments.apps
done
echo

# --- 4. Teardown is permitted -------------------------------------------
# The smoke workflow needs `delete namespace` on its own ephemeral ns.
assert_can "can delete the verification namespace" yes \
    delete namespace "$VERIFY_NS"
echo

# --- Summary -------------------------------------------------------------
printf 'verify-rke2-access.sh: %d passed, %d failed\n' "$PASS_COUNT" "$FAIL_COUNT"
if [[ "$FAIL_COUNT" -gt 0 ]]; then
    exit 1
fi
