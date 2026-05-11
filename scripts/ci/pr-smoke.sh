#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# Per-PR ephemeral cluster smoke script (Task #50, G2.7-T2).
#
# Invoked by `.github/workflows/pr-smoke.yml` after `helm upgrade --install`
# completes against the ephemeral `meho-ci-<pr-number>` namespace on
# rke2-infra. Asserts the backplane's public operator surfaces respond
# correctly without authentication, then exits non-zero on any failure so
# the workflow's teardown step (which runs `if: always()`) cleans up and
# the PR's smoke status flips to red.
#
# This is the v0.1 PR-smoke contract — deliberately scoped to the
# unauthenticated surface (`/healthz`, `/version`, `/api/v1/health` 401
# negative test). The authenticated federation-chain smoke lives in
# claude-rdc-hetzner-dc/manifests/meho/smoke.sh (operator-facing,
# real-credentials, against the persistent install) and is exercised in
# Goal #11 G2.8 against the production-style instance, not per PR.
#
# Arguments:
#   $1  ephemeral namespace name (required) — typically meho-ci-<pr-number>.
#
# Environment:
#   KUBECONFIG  optional; defaults to $HOME/.kube/config (set by the
#               workflow's "Build kubeconfig" step). Honoured implicitly
#               by every kubectl invocation.
#
# Exit codes:
#   0  every smoke assertion passed.
#   non-zero  one or more assertions failed; the workflow teardown step
#             still runs because of `if: always()`.
#
# Hard fail-loud rules: `set -euo pipefail` aborts on the first failure
# instead of trying to continue past a half-ready backplane. Inline
# `[ "$X" = "200" ]` comparisons fail-loud on mismatch because the test
# `-eq` family would also fire if curl emitted an empty string. The
# port-forward background process is captured into PF_PID and an EXIT
# trap kills it even on bash abort.

set -euo pipefail

NS="${1:?usage: pr-smoke.sh <namespace>}"

# Wait for the backplane Deployment to reach Available before any HTTP
# probe — the helm install --wait gate already blocks on this, but a
# defence-in-depth re-check here surfaces a deceptively-fast helm exit
# (e.g. if a future templating change drops --wait by accident) as a
# rollout-status failure rather than a confusing 502 from port-forward.
echo ">> waiting for deployment/meho rollout in $NS"
kubectl rollout status -n "$NS" deployment/meho --timeout=2m

# Port-forward into the Service's `http` port (8000 per chart values).
# Bound to localhost only — the runner is shared infra, so binding 0.0.0.0
# would expose the backplane briefly to anything else co-resident on the
# host network. `&` puts kubectl into the background; PF_PID captures the
# PID for the EXIT trap. `sleep 3` gives port-forward time to establish
# the tunnel (the alternative — polling /healthz with retries — is
# implemented later in this script via the curl `--retry` flag, so the
# fixed sleep covers just the initial socket bind).
echo ">> port-forward svc/meho 8000:8000"
kubectl port-forward -n "$NS" svc/meho 8000:8000 --address 127.0.0.1 >/dev/null 2>&1 &
PF_PID=$!
trap 'kill "$PF_PID" 2>/dev/null || true' EXIT
sleep 3

# Tiny curl wrapper for the assertions below. `--retry 5 --retry-delay 1
# --retry-connrefused` covers the port-forward warm-up gap (the socket
# is bound before the kubectl-proxy handshake fully settles).
# `-o /dev/null -w '%{http_code}'` strips the body and emits just the
# HTTP status as the command's stdout, ready to compare to a literal.
http_code() {
  curl -sS \
    --retry 5 --retry-delay 1 --retry-connrefused \
    -o /dev/null -w '%{http_code}' \
    "$1"
}

echo ">> assert /healthz returns 200"
code="$(http_code 'http://127.0.0.1:8000/healthz')"
if [ "$code" != "200" ]; then
  echo "::error title=/healthz failed::expected 200, got $code" >&2
  exit 1
fi

echo ">> assert /version exposes a non-empty git_sha"
# /version is the operator-surface metadata endpoint (backplane main.py
# registers it alongside /healthz and /ready). jq -e returns non-zero
# when the JSON predicate evaluates to false or null, so a missing
# git_sha key OR the placeholder "unknown" value both fail the assertion.
# Without -e jq would emit "false" on stdout and exit 0, which is the
# documented jq gotcha for assertions.
version_json="$(curl -sSf \
  --retry 5 --retry-delay 1 --retry-connrefused \
  'http://127.0.0.1:8000/version')"
echo "$version_json" \
  | jq -e '.git_sha and .git_sha != "unknown" and (.git_sha | length) > 0' >/dev/null

echo ">> assert /api/v1/health unauthenticated returns 401"
# Negative authentication test: hitting the federation-proof endpoint
# without a Keycloak access token MUST be rejected as 401. A 200 here
# would indicate either (a) auth middleware regressed open, or (b) the
# backplane is wired to the wrong Keycloak realm and accepting anonymous
# requests — both are PR-blocking regressions Goal #11 considers
# non-negotiable.
code="$(http_code 'http://127.0.0.1:8000/api/v1/health')"
if [ "$code" != "401" ]; then
  echo "::error title=/api/v1/health auth gate regressed::expected 401, got $code" >&2
  exit 1
fi

echo ">> PR smoke passed"
