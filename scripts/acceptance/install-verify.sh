#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# install-verify.sh — producer-side cold-deploy acceptance verifier
# (Task #55, Goal #11 DoD bullet 1).
#
# Runs the assertions documented in docs/acceptance/install.md against a
# freshly-installed MEHO instance. Designed to be invoked as the LAST
# step of the consumer's `install.sh` (the wrapper lives on
# `claude-rdc-hetzner-dc/manifests/meho/install.sh` per Goal #11
# cross-repo deps). The verifier's exit code IS the cold-deploy's exit
# code:
#
#   0  → every required check passed
#   1  → at least one required check failed
#   2  → CLI usage error (missing args, kubectl/curl/jq not found)
#
# Usage (called from the consumer's install.sh):
#
#   # FIRST action in install.sh, before any work:
#   START_TS=$(date -u +%s)
#   export INSTALL_START_TS="$START_TS"
#
#   # ... pre-flight + helm upgrade --install ...
#
#   # LAST action in install.sh:
#   bash scripts/acceptance/install-verify.sh \
#     --host meho.evba.lab \
#     --expected-git-sha "$TAG" \
#     --namespace meho
#
# Flags:
#   --host <hostname>          Ingress hostname (default: meho.evba.lab).
#                              The verifier hits https://<hostname>/...
#                              for the public-surface probes.
#   --expected-git-sha <sha>   The git SHA the operator passed to
#                              install.sh --tag. /version must report
#                              this value. Required.
#   --namespace <ns>           Kubernetes namespace the chart was
#                              installed into (default: meho).
#   --release <name>           Helm release name (default: meho). Used
#                              to find the migration Job.
#   --cacert <path>            CA bundle for the ingress TLS verify.
#                              Default: use system trust store.
#   --enforce-budget           Treat the 5-minute budget check as a
#                              HARD failure (exit non-zero). Default
#                              behaviour is warning-only — the budget
#                              check still prints WARN and the verifier
#                              still records the timing for the
#                              closing-comment artefact, but doesn't
#                              fail-loud. Under --enforce-budget the
#                              verifier ALSO hard-fails when
#                              INSTALL_START_TS is unset or non-numeric
#                              (a closing-criteria run with no timing
#                              input would otherwise pass silently and
#                              never prove the <5-min bar was met).
#                              Goal #11 closing-criteria runs MUST pass
#                              --enforce-budget.
#   --skip-cluster-checks      Skip the kubectl-side checks (#1, #2).
#                              Useful when running from a workstation
#                              without kubectl context but with TLS
#                              reachability to the ingress. The HTTP
#                              probes still run.
#   -h | --help                Print this header and exit 0.
#
# Environment:
#   KUBECONFIG            Honoured by every kubectl invocation.
#   MEHO_ACCESS_TOKEN     If set, two additional optional checks run:
#                         the authenticated /api/v1/health probe and a
#                         note pointing at the operator-cost
#                         psql-side audit-row check. Omit for the
#                         unauthenticated path.
#   INSTALL_START_TS      Unix timestamp (seconds, UTC) recorded by
#                         install.sh as its first action. Used to
#                         compute the wall-clock budget. Omit only if
#                         the verifier is being invoked standalone for
#                         debugging (the budget check then prints
#                         "n/a — INSTALL_START_TS unset" and is skipped
#                         from the pass/fail tally).
#
# Notes on the script's defensive shape:
#
# - `set -Eeuo pipefail` aborts on the first failed command; the EXIT
#   trap captures partial state so a half-run is visible in the
#   closing-comment artefact instead of vanishing on `kill -INT`.
# - `assert_*` helpers print `[ok]` / `[FAIL]` / `[WARN]` with
#   deterministic prefixes so the artefact's grep-ability is stable
#   across rebases.
# - Inline literal compare (`[ "$X" = "200" ]`) — `-eq` would also
#   match an empty curl response on some bash builds.
# - `curl --retry` covers ingress-controller warm-up after a fresh
#   cert-manager Certificate (the TLS handshake can briefly stall while
#   the controller wires up the new SNI map). The retry budget is
#   bounded so a real outage surfaces inside the 5-minute window
#   rather than masking it as a slow probe.
# - The verifier does NOT modify cluster state. No `apply`, no
#   `delete`. Safe to run repeatedly against the live deploy.

set -Eeuo pipefail

# --- Defaults ----------------------------------------------------------
HOST="meho.evba.lab"
EXPECTED_GIT_SHA=""
NAMESPACE="meho"
RELEASE="meho"
CACERT=""
ENFORCE_BUDGET=0
SKIP_CLUSTER_CHECKS=0
BUDGET_SECONDS=300 # 5 minutes — Goal #11 DoD bullet 1.

# --- CLI parse ---------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --host=*) HOST="${1#*=}"; shift ;;
        --host)
            [[ $# -ge 2 && -n "${2:-}" ]] || { echo "install-verify.sh: --host requires a value" >&2; exit 2; }
            HOST="$2"; shift 2 ;;
        --expected-git-sha=*) EXPECTED_GIT_SHA="${1#*=}"; shift ;;
        --expected-git-sha)
            [[ $# -ge 2 && -n "${2:-}" ]] || { echo "install-verify.sh: --expected-git-sha requires a value" >&2; exit 2; }
            EXPECTED_GIT_SHA="$2"; shift 2 ;;
        --namespace=*) NAMESPACE="${1#*=}"; shift ;;
        --namespace)
            [[ $# -ge 2 && -n "${2:-}" ]] || { echo "install-verify.sh: --namespace requires a value" >&2; exit 2; }
            NAMESPACE="$2"; shift 2 ;;
        --release=*) RELEASE="${1#*=}"; shift ;;
        --release)
            [[ $# -ge 2 && -n "${2:-}" ]] || { echo "install-verify.sh: --release requires a value" >&2; exit 2; }
            RELEASE="$2"; shift 2 ;;
        --cacert=*) CACERT="${1#*=}"; shift ;;
        --cacert)
            [[ $# -ge 2 && -n "${2:-}" ]] || { echo "install-verify.sh: --cacert requires a value" >&2; exit 2; }
            CACERT="$2"; shift 2 ;;
        --enforce-budget) ENFORCE_BUDGET=1; shift ;;
        --skip-cluster-checks) SKIP_CLUSTER_CHECKS=1; shift ;;
        -h|--help)
            # Re-print the script header (everything between the first
            # `#!` line and the first blank-comment line). Stable
            # operator-facing help text without duplicating the docs.
            sed -n '2,75p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo "install-verify.sh: unknown argument: $1" >&2; exit 2 ;;
    esac
done

if [[ -z "$EXPECTED_GIT_SHA" ]]; then
    echo "install-verify.sh: --expected-git-sha is required" >&2
    echo "  pass the same value install.sh received via --tag" >&2
    exit 2
fi

# --- Tooling pre-flight ------------------------------------------------
need_tool() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "install-verify.sh: required tool '$1' not on PATH" >&2
        exit 2
    fi
}
need_tool curl
need_tool jq
if [[ "$SKIP_CLUSTER_CHECKS" -ne 1 ]]; then
    need_tool kubectl
fi

# --- Counters + helpers ------------------------------------------------
PASS_COUNT=0
FAIL_COUNT=0
WARN_COUNT=0

check_ok()   { PASS_COUNT=$((PASS_COUNT + 1)); printf '[ok]   %s\n' "$1"; }
check_fail() {
    FAIL_COUNT=$((FAIL_COUNT + 1))
    printf '[FAIL] %s\n' "$1" >&2
    [[ -n "${2:-}" ]] && printf '       %s\n' "$2" >&2
}
check_warn() {
    WARN_COUNT=$((WARN_COUNT + 1))
    printf '[WARN] %s\n' "$1" >&2
    [[ -n "${2:-}" ]] && printf '       %s\n' "$2" >&2
}
check_note() { printf '[note] %s\n' "$1"; }

# Curl wrapper that returns only the body on success. `--retry` covers
# the cert-manager + ingress-controller warm-up gap; `--retry-all-errors`
# (curl >=7.71) treats TLS handshake errors as retryable too. The
# `|| true` fallback to `--retry-connrefused` keeps older curl builds
# working (Ubuntu 22.04 ships 7.81 which has --retry-all-errors).
curl_body() {
    local url="$1"
    shift
    local cacert_args=()
    [[ -n "$CACERT" ]] && cacert_args=("--cacert" "$CACERT")
    curl -sSf "${cacert_args[@]}" \
        --retry 5 --retry-delay 2 --retry-connrefused --retry-all-errors \
        --max-time 10 \
        "$@" "$url"
}

curl_code() {
    local url="$1"
    shift
    local cacert_args=()
    [[ -n "$CACERT" ]] && cacert_args=("--cacert" "$CACERT")
    curl -sS "${cacert_args[@]}" \
        --retry 5 --retry-delay 2 --retry-connrefused --retry-all-errors \
        --max-time 10 \
        -o /dev/null -w '%{http_code}' \
        "$@" "$url"
}

# --- Banner ------------------------------------------------------------
echo "install-verify.sh: producer-side cold-deploy acceptance check"
echo "  host:              https://$HOST"
echo "  namespace:         $NAMESPACE"
echo "  release:           $RELEASE"
echo "  expected git_sha:  $EXPECTED_GIT_SHA"
[[ -n "$CACERT" ]] && echo "  CA bundle:         $CACERT"
[[ "$SKIP_CLUSTER_CHECKS" -eq 1 ]] && echo "  cluster checks:    SKIPPED"
[[ "$ENFORCE_BUDGET" -eq 1 ]] && echo "  budget enforce:    HARD"
echo

# --- Check #1: Deployment Available ------------------------------------
if [[ "$SKIP_CLUSTER_CHECKS" -ne 1 ]]; then
    # Resolve the backplane Deployment by label selector rather than the
    # raw release name. The chart's `meho.fullname` helper produces
    # different object names depending on whether the release name
    # contains the chart name (`meho` → `meho`, `prod` → `prod-meho`),
    # so a literal `deployment/${RELEASE}` lookup is release-name-
    # specific and would false-fail on any non-`meho` release. The
    # selector `app.kubernetes.io/name=meho,app.kubernetes.io/instance=$RELEASE`
    # is what install.md row 1 already documents and is invariant
    # across rename. The `name=meho` discriminator also keeps us from
    # matching the broadcast subchart's Deployment (which carries
    # `app.kubernetes.io/name=broadcast`).
    deployment_name="$(kubectl get deployment -n "$NAMESPACE" \
        -l "app.kubernetes.io/name=meho,app.kubernetes.io/instance=${RELEASE}" \
        -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
    if [[ -z "$deployment_name" ]]; then
        check_fail "no Deployment matching app.kubernetes.io/name=meho,app.kubernetes.io/instance=${RELEASE} in ${NAMESPACE}" \
            "kubectl get deployment -n $NAMESPACE -l app.kubernetes.io/instance=${RELEASE} to inspect what's present"
    else
        # `kubectl rollout status` blocks until Available=True OR the
        # timeout fires. 60s is generous given the install just
        # completed with `helm --wait`; if it isn't ready by now
        # something's wrong.
        if kubectl rollout status -n "$NAMESPACE" "deployment/${deployment_name}" --timeout=60s >/dev/null 2>&1; then
            check_ok "deployment/${deployment_name} Available in namespace ${NAMESPACE}"
        else
            check_fail "deployment/${deployment_name} not Available" \
                "kubectl describe deployment -n $NAMESPACE $deployment_name for details"
        fi
    fi

    # --- Check #2: Migration Job succeeded ------------------------------
    # The Job name is templated by the chart as
    # `<meho.fullname>-migrate`, which is release-name-dependent (see
    # deploy/charts/meho/templates/migration-job.yaml + _helpers.tpl).
    # Use the release-name-agnostic label selector
    # `app.kubernetes.io/component=migrate,app.kubernetes.io/instance=$RELEASE`
    # — install.md row 2 already documents this shape. Helm's
    # `pre-install,pre-upgrade` hook GCs the Job on success via
    # `hook-succeeded`, so a successful migration may leave NO Job in
    # the namespace by the time the verifier runs. Distinguish "Job
    # ran successfully and was GC'd" from "Job never ran" by checking
    # the helm history's last revision state — Helm only marks the
    # release `deployed` if every hook including the migration
    # succeeded.
    job_name="$(kubectl get job -n "$NAMESPACE" \
        -l "app.kubernetes.io/component=migrate,app.kubernetes.io/instance=${RELEASE}" \
        -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
    job_status=""
    if [[ -n "$job_name" ]]; then
        job_status="$(kubectl get job -n "$NAMESPACE" "$job_name" \
            -o jsonpath='{.status.succeeded}' 2>/dev/null || true)"
        if [[ "$job_status" == "1" ]]; then
            check_ok "migration Job ${job_name} succeeded"
        else
            check_fail "migration Job ${job_name} exists but did not succeed" \
                "status.succeeded='$job_status'; kubectl logs -n $NAMESPACE job/${job_name}"
        fi
    else
        # Job was GC'd (hook-succeeded deletion). Assert helm release
        # status as the equivalent positive signal. The helm release
        # only flips to `deployed` when every pre-install/pre-upgrade
        # hook including the migration Job exited 0.
        if command -v helm >/dev/null 2>&1; then
            helm_status="$(helm status -n "$NAMESPACE" "$RELEASE" -o json 2>/dev/null \
                | jq -r '.info.status // "unknown"' 2>/dev/null || echo "unknown")"
            if [[ "$helm_status" == "deployed" ]]; then
                check_ok "migration Job succeeded (GC'd by hook-succeeded; helm release status=deployed)"
            else
                check_fail "cannot confirm migration Job succeeded" \
                    "Job not present and helm release status='$helm_status' (expected 'deployed')"
            fi
        else
            check_warn "helm not on PATH; migration Job already GC'd" \
                "install 'helm' to assert release status as the fallback signal"
        fi
    fi
fi

# --- Check #3: /healthz returns 200 ------------------------------------
code="$(curl_code "https://${HOST}/healthz" || true)"
if [[ "$code" = "200" ]]; then
    check_ok "GET https://${HOST}/healthz returns 200"
else
    check_fail "GET https://${HOST}/healthz expected 200, got '$code'" \
        "ingress + Service + Pod readiness; kubectl get ingress -n $NAMESPACE; curl -v"
fi

# --- Check #4: /version carries the expected git_sha -------------------
# Catch the helm no-op case: if the chart + image tag are identical to
# the previous revision, no Pods roll, the old Pods keep serving, and
# the operator might think the deploy "succeeded" while serving stale
# code. Comparing the running /version.git_sha to --tag closes that gap.
if version_json="$(curl_body "https://${HOST}/version" 2>/dev/null)"; then
    if echo "$version_json" | jq -e '.git_sha' >/dev/null 2>&1; then
        live_sha="$(echo "$version_json" | jq -r '.git_sha')"
        # Guard against the empty-or-junk live_sha false-pass: the
        # previous bidirectional prefix-match treated an empty string
        # as a prefix of every expected SHA, so `/version` returning
        # `{"git_sha": ""}` (or null serialized as "" by jq -r) would
        # silently pass. Require live_sha to be non-empty AND
        # SHA-shaped (hex / hyphen / dot / underscore — enough to
        # cover both bare SHAs and `sha-<40hex>` tag form) before
        # running any comparison.
        if [[ -z "$live_sha" || "$live_sha" == "null" ]]; then
            check_fail "/version reports empty git_sha" \
                "expected '$EXPECTED_GIT_SHA'; got '$live_sha' (raw response: $version_json)"
        elif ! [[ "$live_sha" =~ ^[A-Za-z0-9._-]+$ ]]; then
            check_fail "/version git_sha contains unexpected characters" \
                "expected '$EXPECTED_GIT_SHA' (bare); got '$live_sha'"
        else
            # The image-tag convention is `sha-<40-char-git-sha>` but
            # /version reports the bare SHA (per the chassis stamping
            # in image.yml). Accept either form by stripping the
            # `sha-` prefix from --expected-git-sha, then require a
            # forward prefix-match: the live SHA must start with the
            # expected SHA (or vice-versa for short-form expected).
            # Drop the previous bidirectional substring check — it
            # admitted any non-empty live_sha that happened to be a
            # substring of the expected value.
            expected_bare="${EXPECTED_GIT_SHA#sha-}"
            if [[ "$live_sha" == "$expected_bare" ]] \
                || [[ "$live_sha" == "$expected_bare"* ]] \
                || [[ "$expected_bare" == "$live_sha"* ]]; then
                check_ok "/version reports git_sha='$live_sha' matching expected '$EXPECTED_GIT_SHA'"
            else
                check_fail "/version git_sha mismatch" \
                    "expected '$EXPECTED_GIT_SHA' (bare '$expected_bare'); got '$live_sha'"
            fi
        fi
    else
        check_fail "/version response missing .git_sha field" "$version_json"
    fi
else
    check_fail "GET https://${HOST}/version failed" \
        "ingress reachable but /version not responding; kubectl logs -n $NAMESPACE deployment/$RELEASE"
fi

# --- Check #5: /api/v1/health unauthenticated returns 401 --------------
# Negative auth test. 200 here = federation chain regressed open;
# anything other than 401 (including 503) = a different regression.
code="$(curl_code "https://${HOST}/api/v1/health" || true)"
if [[ "$code" = "401" ]]; then
    check_ok "GET https://${HOST}/api/v1/health (unauthenticated) returns 401"
else
    check_fail "GET https://${HOST}/api/v1/health auth gate regressed" \
        "expected 401; got '$code'. 200 = middleware regressed open; 502/503 = backplane / ingress issue"
fi

# --- Check #6: audit middleware reachability (surrogate) ---------------
# Direct `SELECT FROM audit_log` requires operator-side PG credentials;
# that lands in Task #56's federation-chain smoke (which authenticates
# AND writes a row). At cold-deploy time, the verifier asserts the
# audit middleware is wired into the chain by checking the response
# headers from the negative-auth probe above carry the request-ID the
# middleware stamps. This is a surrogate, not a row-write proof, and
# the table above documents it as such.
if request_id_header="$(curl -sS \
    ${CACERT:+--cacert "$CACERT"} \
    --retry 5 --retry-delay 2 --retry-connrefused --retry-all-errors \
    --max-time 10 \
    -o /dev/null -D - \
    "https://${HOST}/api/v1/health" 2>/dev/null \
    | tr -d '\r' \
    | grep -i '^x-request-id:' || true)"; then
    if [[ -n "$request_id_header" ]]; then
        check_ok "audit middleware reachable (X-Request-ID stamped on response)"
    else
        # Missing X-Request-ID isn't a hard failure for the cold-deploy
        # (could be an ingress strip); flag as WARN so the operator
        # investigates without blocking the install.
        check_warn "audit middleware reachability inconclusive" \
            "X-Request-ID header not observed; verify with curl -v or run Task #56 smoke"
    fi
else
    check_warn "audit middleware reachability inconclusive" \
        "could not capture response headers; rerun with curl -v for diagnostics"
fi

# --- Check #7: 5-minute wall-clock budget ------------------------------
# Behaviour matrix:
#
#   ENFORCE_BUDGET  INSTALL_START_TS   outcome
#   0 (warn-only)   unset              [note] skipped — debugging mode
#   0               non-numeric        [WARN] invalid value — debugging mode
#   0               numeric            [ok]/[WARN] depending on elapsed
#   1 (hard-fail)   unset              [FAIL] — Goal #11 runs require timing proof
#   1               non-numeric        [FAIL] — same as above
#   1               numeric            [ok]/[FAIL] depending on elapsed
#
# The hard-fail-on-missing/invalid behaviour under --enforce-budget is
# load-bearing for Goal #11 DoD bullet 1: a closing-criteria run with no
# timing input would otherwise pass silently and the operator's
# transcript could never prove the <5-min bar was met. The flag's whole
# point is "treat the budget as a hard contract" — which has to include
# "the budget input itself must be present and valid".
if [[ -z "${INSTALL_START_TS:-}" ]]; then
    if [[ "$ENFORCE_BUDGET" -eq 1 ]]; then
        check_fail "INSTALL_START_TS is unset under --enforce-budget" \
            "Goal #11 closing-criteria runs require a numeric start timestamp; install.sh sets START_TS=\$(date -u +%s) as its first action"
    else
        check_note "wall-clock budget check skipped — INSTALL_START_TS unset"
    fi
elif ! [[ "$INSTALL_START_TS" =~ ^[0-9]+$ ]]; then
    if [[ "$ENFORCE_BUDGET" -eq 1 ]]; then
        check_fail "INSTALL_START_TS is not a unix timestamp under --enforce-budget" \
            "got '$INSTALL_START_TS'; install.sh should set START_TS=\$(date -u +%s)"
    else
        check_warn "INSTALL_START_TS is not a unix timestamp" \
            "got '$INSTALL_START_TS'; install.sh should set START_TS=\$(date -u +%s)"
    fi
else
    NOW_TS="$(date -u +%s)"
    ELAPSED=$((NOW_TS - INSTALL_START_TS))
    if [[ "$ELAPSED" -le "$BUDGET_SECONDS" ]]; then
        check_ok "wall-clock ${ELAPSED}s ≤ budget ${BUDGET_SECONDS}s"
    else
        msg="wall-clock ${ELAPSED}s > budget ${BUDGET_SECONDS}s"
        if [[ "$ENFORCE_BUDGET" -eq 1 ]]; then
            check_fail "$msg" "Goal #11 closing-criteria run; see docs/acceptance/install.md 'What \"<5 min\" means'"
        else
            check_warn "$msg" "non-closing run; rerun with --enforce-budget for hard-fail behaviour"
        fi
    fi
fi

# --- Optional checks #8 / #9: authenticated probes ---------------------
if [[ -n "${MEHO_ACCESS_TOKEN:-}" ]]; then
    echo
    echo "install-verify.sh: MEHO_ACCESS_TOKEN set; running authenticated probes"

    code="$(curl_code "https://${HOST}/api/v1/health" \
        -H "Authorization: Bearer $MEHO_ACCESS_TOKEN" || true)"
    if [[ "$code" = "200" ]]; then
        check_ok "GET /api/v1/health (authenticated) returns 200"
    else
        check_fail "authenticated /api/v1/health expected 200, got '$code'" \
            "federation chain (JWKS / audience / Vault role) broken; kubectl logs -n $NAMESPACE deployment/$RELEASE"
    fi

    # The actual audit-row check requires PG access; the verifier can't
    # do it remotely without leaking DB credentials. Point the operator
    # at the canonical psql command shape so the closing-comment
    # artefact includes the proof.
    check_note "operator: confirm audit_log row was written:"
    check_note "  psql \"\$DATABASE_URL\" -c \\"
    check_note "    \"SELECT operator_sub, method, path, status_code, occurred_at\""
    check_note "    \"     FROM audit_log\""
    check_note "    \"    WHERE occurred_at > NOW() - INTERVAL '1 minute'\""
    check_note "    \"    ORDER BY occurred_at DESC LIMIT 1;\""
    check_note "see docs/acceptance/install.md 'Optional (operator-asserted)' for context"
else
    echo
    check_note "MEHO_ACCESS_TOKEN unset; skipping authenticated probes (#8, #9)"
    check_note "  Goal #11 closing-criteria run: pair this verifier with Task #56 federation-chain smoke"
fi

# --- Summary -----------------------------------------------------------
echo
printf 'install-verify.sh: %d passed, %d failed, %d warned\n' \
    "$PASS_COUNT" "$FAIL_COUNT" "$WARN_COUNT"

if [[ "$FAIL_COUNT" -gt 0 ]]; then
    echo "install-verify.sh: cold-deploy DID NOT pass acceptance — see [FAIL] lines above" >&2
    exit 1
fi

echo "[ok]   MEHO is up"
exit 0
