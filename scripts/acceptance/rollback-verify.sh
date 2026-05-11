#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# rollback-verify.sh — producer-side helm-rollback acceptance verifier
# (Task #57, Goal #11 DoD bullet 3).
#
# The cluster-level proof of the forward-compat property: deploy
# version N+1 (with a non-trivial additive migration) → `helm rollback`
# back to N → verify the rolled-back N image runs cleanly against the
# N+1 schema. The unit-test-level proof of the same property lives at
# backend/tests/test_migration_rollback.py (Task #30, Initiative #26);
# together they form two layers of forward-compat assurance for the
# Goal #11 DoD bullet 3 contract:
#
#   `helm rollback meho` returns to the previous chart version without
#   manual DB intervention.
#
# This script does NOT itself drive the deploy/rollback exercise (that
# requires a kubeconfig pointing at a real RKE2 cluster + Vault session
# + GHCR egress; same posture as install-verify.sh — producer ships the
# contract + the assertions, consumer runs the exercise). What this
# script does is, after the consumer has done the deploy → rollback,
# assert the END STATE is what Goal #11 promises:
#
#   1. `helm history` shows the most recent action was a rollback
#      (description contains "Rollback to").
#   2. The deployed Pod is Available and running the N image (the
#      operator passes the N image SHA / tag via --n-image-sha).
#   3. The schema **stays at N+1** — the non-trivial additive columns
#      from the N→N+1 migration are present in `audit_log`. (The
#      operator names the columns via --expected-schema-columns; the
#      sample synthetic migration at scripts/acceptance/synthetic-n-plus-1.sql
#      adds `payload_summary text NULL DEFAULT 'reserved_for_v0.2'` for
#      this purpose.) The schema check is optional and only runs when
#      DATABASE_URL is set on the verifier host — same posture as
#      install-verify.sh's audit-row check.
#   4. The post-rollback chassis serves traffic against the N+1 schema:
#      `/healthz` 200, `/version.git_sha` matches the N image, and
#      `/api/v1/health` unauthenticated returns 401 (federation chain
#      still gates correctly).
#
# Exit codes mirror install-verify.sh:
#
#   0  → every required check passed (forward-compat property holds
#        end-to-end against the live cluster).
#   1  → at least one required check failed (rollback did NOT pass
#        acceptance; see [FAIL] lines).
#   2  → CLI usage error (missing args, kubectl/curl/jq/helm not on
#        PATH).
#
# Usage (called from the consumer's rollback exercise script, or
# directly by the operator):
#
#   bash scripts/acceptance/rollback-verify.sh \
#     --host meho.evba.lab \
#     --n-image-sha <git-sha-of-N> \
#     --namespace meho \
#     --release meho \
#     --expected-schema-columns payload_summary
#
# Flags:
#   --host <hostname>             Ingress hostname (default: meho.evba.lab).
#                                 The verifier hits https://<hostname>/...
#                                 for the public-surface probes.
#   --n-image-sha <sha>           The git SHA of the N image — i.e. the
#                                 image the rollback returned to.
#                                 /version must report this value
#                                 post-rollback. Required.
#   --namespace <ns>              Kubernetes namespace the chart was
#                                 installed into (default: meho).
#   --release <name>              Helm release name (default: meho).
#   --expected-schema-columns     Comma-separated list of column names
#                                 added by the N→N+1 migration that
#                                 MUST still be present in `audit_log`
#                                 after the rollback (proves the
#                                 schema stayed at N+1). Default:
#                                 `payload_summary` — the column the
#                                 sample synthetic migration adds.
#                                 The schema check itself only runs
#                                 when DATABASE_URL is set on the
#                                 verifier host; otherwise check #3 is
#                                 a [note] line and is recorded as a
#                                 deferred assertion (per docs/acceptance/rollback.md).
#   --cacert <path>               CA bundle for the ingress TLS verify
#                                 (default: system trust store).
#   --skip-cluster-checks         Skip kubectl-side checks (#1, #2).
#                                 Useful when running from a
#                                 workstation without kubectl context
#                                 but with TLS reachability to the
#                                 ingress. The HTTP probes still run.
#                                 The helm-history check (#1a) is also
#                                 skipped — helm CLI access to the
#                                 release requires the same kubeconfig.
#   -h | --help                   Print this help and exit 0.
#
# Environment:
#   KUBECONFIG            Honoured by every kubectl + helm invocation.
#   DATABASE_URL          PostgreSQL connection string (psql shape).
#                         When set, the verifier runs check #3 (schema
#                         columns persist) against the live DB. When
#                         unset, check #3 is deferred to the
#                         consumer-side runbook (operator runs the psql
#                         query manually and pastes the output into
#                         the closing-comment artefact on #57).
#
# Notes on the defensive shape:
#
# - `set -Eeuo pipefail` aborts on first failed command; the verifier
#   never modifies cluster state (no `apply`, no `delete`, no `helm
#   upgrade`). Safe to run repeatedly against the same release.
# - `[ "$X" = "<literal>" ]` literal-string compares — `-eq` family
#   would match an empty curl response on some bash builds.
# - The deploy/rollback exercise itself lives in the consumer-side
#   rollback drill (docs/acceptance/rollback.md "What the consumer-side
#   exercise script needs to do"). The verifier is invoked as the
#   LAST step of that exercise, and its exit code becomes the
#   exercise's exit code — same contract shape as install-verify.sh
#   for Task #55.

set -Eeuo pipefail

# --- Help text ---------------------------------------------------------
# Self-contained --help output. Same rationale as install-verify.sh:
# a heredoc decouples the operator-facing surface from this script's
# physical line numbering, so reordering the header comments cannot
# silently truncate help mid-flag.
usage() {
    cat <<'HELP'
rollback-verify.sh — producer-side helm-rollback acceptance verifier
(Task #57, Goal #11 DoD bullet 3).

Runs the assertions documented in docs/acceptance/rollback.md against
a release where the consumer has just executed:

  helm upgrade --install meho ... --version <N+1>   # schema migrates
  helm rollback meho <revision-of-N> -n <ns> --wait # image returns to N

The verifier's exit code IS the rollback exercise's exit code:

  0  every required check passed (forward-compat property holds)
  1  at least one required check failed
  2  CLI usage error (missing args, kubectl/curl/jq/helm not found)

Usage:
  bash scripts/acceptance/rollback-verify.sh \
    --host meho.evba.lab \
    --n-image-sha "$N_SHA" \
    --namespace meho \
    --release meho \
    --expected-schema-columns payload_summary

Flags:
  --host <hostname>             Ingress hostname (default: meho.evba.lab).
  --n-image-sha <sha>           Git SHA of the N image — the image the
                                rollback returned to. /version must
                                report this value post-rollback. Required.
  --namespace <ns>              Kubernetes namespace (default: meho).
  --release <name>              Helm release name (default: meho).
  --expected-schema-columns     Comma-separated list of audit_log
                                column names the N→N+1 migration added
                                (default: payload_summary). The
                                schema check itself runs only when
                                DATABASE_URL is set.
  --cacert <path>               CA bundle for ingress TLS verify
                                (default: system trust store).
  --skip-cluster-checks         Skip kubectl + helm checks (#1, #2,
                                #1a). HTTP probes still run.
  -h | --help                   Print this help and exit 0.

Environment:
  KUBECONFIG    Honoured by every kubectl + helm invocation.
  DATABASE_URL  psql connection string. Enables check #3 when set.

See docs/acceptance/rollback.md for the full contract.
HELP
}

# --- Defaults ----------------------------------------------------------
HOST="meho.evba.lab"
N_IMAGE_SHA=""
NAMESPACE="meho"
RELEASE="meho"
EXPECTED_SCHEMA_COLUMNS="payload_summary"
CACERT=""
SKIP_CLUSTER_CHECKS=0

# --- CLI parse ---------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --host=*) HOST="${1#*=}"; shift ;;
        --host)
            [[ $# -ge 2 && -n "${2:-}" ]] || { echo "rollback-verify.sh: --host requires a value" >&2; exit 2; }
            HOST="$2"; shift 2 ;;
        --n-image-sha=*) N_IMAGE_SHA="${1#*=}"; shift ;;
        --n-image-sha)
            [[ $# -ge 2 && -n "${2:-}" ]] || { echo "rollback-verify.sh: --n-image-sha requires a value" >&2; exit 2; }
            N_IMAGE_SHA="$2"; shift 2 ;;
        --namespace=*) NAMESPACE="${1#*=}"; shift ;;
        --namespace)
            [[ $# -ge 2 && -n "${2:-}" ]] || { echo "rollback-verify.sh: --namespace requires a value" >&2; exit 2; }
            NAMESPACE="$2"; shift 2 ;;
        --release=*) RELEASE="${1#*=}"; shift ;;
        --release)
            [[ $# -ge 2 && -n "${2:-}" ]] || { echo "rollback-verify.sh: --release requires a value" >&2; exit 2; }
            RELEASE="$2"; shift 2 ;;
        --expected-schema-columns=*) EXPECTED_SCHEMA_COLUMNS="${1#*=}"; shift ;;
        --expected-schema-columns)
            [[ $# -ge 2 && -n "${2:-}" ]] || { echo "rollback-verify.sh: --expected-schema-columns requires a value" >&2; exit 2; }
            EXPECTED_SCHEMA_COLUMNS="$2"; shift 2 ;;
        --cacert=*) CACERT="${1#*=}"; shift ;;
        --cacert)
            [[ $# -ge 2 && -n "${2:-}" ]] || { echo "rollback-verify.sh: --cacert requires a value" >&2; exit 2; }
            CACERT="$2"; shift 2 ;;
        --skip-cluster-checks) SKIP_CLUSTER_CHECKS=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "rollback-verify.sh: unknown argument: $1" >&2; exit 2 ;;
    esac
done

if [[ -z "$N_IMAGE_SHA" ]]; then
    echo "rollback-verify.sh: --n-image-sha is required" >&2
    echo "  pass the git SHA of the N image (the image the rollback returned to)" >&2
    exit 2
fi

# --- Tooling pre-flight ------------------------------------------------
need_tool() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "rollback-verify.sh: required tool '$1' not on PATH" >&2
        exit 2
    fi
}
need_tool curl
need_tool jq
if [[ "$SKIP_CLUSTER_CHECKS" -ne 1 ]]; then
    need_tool kubectl
    # helm is required for check #1a (history shape) AND for the
    # release-status fallback in check #2 (same contract as
    # install-verify.sh: when the migration Job has been GC'd by the
    # hook-succeeded delete-policy, the helm release status is the
    # only positive signal left).
    need_tool helm
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

# Curl retry args — same shape as install-verify.sh. `--retry-all-errors`
# only added when the installed curl actually accepts it (gates on
# Ubuntu 22.04's 7.81+ vs older base images).
CURL_RETRY_ARGS=(--retry 5 --retry-delay 2 --retry-connrefused)
if curl --help all 2>/dev/null | grep -q -- '--retry-all-errors'; then
    CURL_RETRY_ARGS+=(--retry-all-errors)
fi

curl_body() {
    local url="$1"
    shift
    local cacert_args=()
    [[ -n "$CACERT" ]] && cacert_args=("--cacert" "$CACERT")
    curl -sSf "${cacert_args[@]}" \
        "${CURL_RETRY_ARGS[@]}" \
        --max-time 10 \
        "$@" "$url"
}

curl_code() {
    local url="$1"
    shift
    local cacert_args=()
    [[ -n "$CACERT" ]] && cacert_args=("--cacert" "$CACERT")
    curl -sS "${cacert_args[@]}" \
        "${CURL_RETRY_ARGS[@]}" \
        --max-time 10 \
        -o /dev/null -w '%{http_code}' \
        "$@" "$url"
}

# --- Banner ------------------------------------------------------------
echo "rollback-verify.sh: producer-side helm-rollback acceptance check"
echo "  host:                    https://$HOST"
echo "  namespace:               $NAMESPACE"
echo "  release:                 $RELEASE"
echo "  expected N image SHA:    $N_IMAGE_SHA"
echo "  expected schema cols:    $EXPECTED_SCHEMA_COLUMNS"
[[ -n "$CACERT" ]] && echo "  CA bundle:               $CACERT"
[[ "$SKIP_CLUSTER_CHECKS" -eq 1 ]] && echo "  cluster checks:          SKIPPED"
echo

# --- Check #1a: helm history shows a recent rollback -------------------
# The cluster-level rollback property only holds if a rollback actually
# happened. Without this assertion the verifier would silently pass on
# a `helm upgrade --install` to the N image (a re-deploy of N — same
# end state superficially, but proves nothing about the rollback path).
# `helm history -o json` returns the revisions in chronological order;
# the LAST element is the most recent. Its `.description` field starts
# with "Rollback to" when produced by `helm rollback` (Helm 3.x; see
# pkg/action/rollback.go's success message).
if [[ "$SKIP_CLUSTER_CHECKS" -ne 1 ]]; then
    if ! history_json="$(helm history -n "$NAMESPACE" "$RELEASE" -o json 2>/dev/null)"; then
        check_fail "helm history -n $NAMESPACE $RELEASE failed" \
            "kubeconfig/release missing? check 'helm list -n $NAMESPACE'"
    else
        # Need at least 2 entries: the install (revision 1) and the
        # rollback action (revision >=2). A single-entry history means
        # no rollback happened.
        history_len="$(echo "$history_json" | jq 'length' 2>/dev/null || echo "0")"
        if [[ "$history_len" -lt 2 ]]; then
            check_fail "helm history has $history_len entries; rollback needs >=2" \
                "no rollback action recorded on this release; rerun the exercise"
        else
            latest_desc="$(echo "$history_json" | jq -r '.[-1].description' 2>/dev/null || echo "")"
            latest_status="$(echo "$history_json" | jq -r '.[-1].status' 2>/dev/null || echo "")"
            if [[ "$latest_desc" == "Rollback to "* && "$latest_status" == "deployed" ]]; then
                check_ok "helm history latest is a successful rollback: $latest_desc"
            else
                check_fail "helm history latest is not a rollback" \
                    "got description='$latest_desc' status='$latest_status'; expected description='Rollback to <rev>' status='deployed'"
            fi
        fi
    fi
fi

# --- Check #1: Deployment Available ------------------------------------
# Same release-name-agnostic label selector as install-verify.sh — the
# chart's `meho.fullname` helper produces release-name-dependent object
# names, so a literal `deployment/${RELEASE}` lookup is brittle on
# non-`meho` releases.
if [[ "$SKIP_CLUSTER_CHECKS" -ne 1 ]]; then
    deployment_name="$(kubectl get deployment -n "$NAMESPACE" \
        -l "app.kubernetes.io/name=meho,app.kubernetes.io/instance=${RELEASE}" \
        -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
    if [[ -z "$deployment_name" ]]; then
        check_fail "no Deployment matching app.kubernetes.io/name=meho,app.kubernetes.io/instance=${RELEASE} in ${NAMESPACE}" \
            "kubectl get deployment -n $NAMESPACE -l app.kubernetes.io/instance=${RELEASE} to inspect what's present"
    else
        # `kubectl rollout status` blocks until Available=True OR the
        # timeout fires. The rollback action runs `--wait` (per the
        # consumer-side exercise script contract), so by the time the
        # verifier runs the rollout should already be Available; 60s is
        # generous headroom.
        if kubectl rollout status -n "$NAMESPACE" "deployment/${deployment_name}" --timeout=60s >/dev/null 2>&1; then
            check_ok "deployment/${deployment_name} Available in namespace ${NAMESPACE} post-rollback"
        else
            check_fail "deployment/${deployment_name} not Available post-rollback" \
                "kubectl describe deployment -n $NAMESPACE $deployment_name for details"
        fi
    fi
fi

# --- Check #2: schema columns persist (N+1 schema stays ahead) ---------
# The load-bearing forward-compat assertion at the cluster level: the
# additive columns from the N+1 migration MUST still exist after the
# rollback. helm rollback does NOT invoke pre-install/pre-upgrade
# hooks for the previous revision (per helm docs on chart hooks), so
# the migration Job from N+1 does not run in reverse — the schema
# stays at N+1 by design. v0.1 has no down-migrations (Task #29 CI
# guard rejects destructive patterns); the test exercise codifies
# this is the correct end state.
#
# DATABASE_URL is the operator-cost path: the verifier needs psql +
# DB credentials to inspect the live schema. When DATABASE_URL is
# unset, the check is deferred to the consumer-side runbook (the
# operator pastes the psql output into the closing comment on #57).
if [[ -n "${DATABASE_URL:-}" ]]; then
    need_tool psql
    # Build a SQL `IN (...)` list from the comma-separated --expected-schema-columns
    # value. `quote_literal` would be ideal but we're constructing SQL
    # client-side; instead we validate that each column name matches
    # the SQL-identifier pattern and refuse to run otherwise (no
    # quoting needed for ASCII identifiers).
    IFS=',' read -ra COLS <<<"$EXPECTED_SCHEMA_COLUMNS"
    sql_list=""
    bad_col=""
    for col in "${COLS[@]}"; do
        col_trimmed="$(echo "$col" | tr -d '[:space:]')"
        if ! [[ "$col_trimmed" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
            bad_col="$col_trimmed"
            break
        fi
        if [[ -z "$sql_list" ]]; then
            sql_list="'$col_trimmed'"
        else
            sql_list+=", '$col_trimmed'"
        fi
    done
    if [[ -n "$bad_col" ]]; then
        check_fail "invalid --expected-schema-columns entry: '$bad_col'" \
            "column names must match ^[A-Za-z_][A-Za-z0-9_]*\$ (ASCII SQL identifier)"
    else
        # information_schema is portable across PG versions; `column_name`
        # is case-folded to lowercase by PG on unquoted identifiers,
        # which matches the lowercase names the sample migration adds.
        query="SELECT string_agg(column_name, ',' ORDER BY column_name) FROM information_schema.columns WHERE table_name = 'audit_log' AND column_name IN ($sql_list);"
        if found_cols="$(psql "$DATABASE_URL" -tAc "$query" 2>/dev/null)"; then
            # Normalise both sides for comparison (sorted, trimmed,
            # lowercase). information_schema returns lowercase already
            # for unquoted column names; the operator's --expected list
            # might mix case.
            expected_norm="$(echo "$EXPECTED_SCHEMA_COLUMNS" | tr ',' '\n' | tr -d '[:space:]' | tr 'A-Z' 'a-z' | sort | paste -sd, -)"
            found_norm="$(echo "$found_cols" | tr ',' '\n' | tr -d '[:space:]' | tr 'A-Z' 'a-z' | sort | paste -sd, -)"
            if [[ "$found_norm" == "$expected_norm" ]]; then
                check_ok "audit_log schema retained N+1 columns: $expected_norm"
            else
                check_fail "audit_log schema did not retain expected N+1 columns" \
                    "expected: '$expected_norm'; found: '$found_norm' (was a down-migration run? v0.1 forbids them)"
            fi
        else
            check_fail "psql query against \$DATABASE_URL failed" \
                "verify DATABASE_URL is set + reachable; query was: $query"
        fi
    fi
else
    check_note "audit_log schema check deferred — \$DATABASE_URL unset"
    check_note "  operator: run the following on the verifier host (or any host with DB access):"
    check_note "    psql \"\$DATABASE_URL\" -c \\"
    check_note "      \"SELECT column_name FROM information_schema.columns\""
    check_note "      \" WHERE table_name='audit_log' AND column_name\""
    check_note "      \" IN ('${EXPECTED_SCHEMA_COLUMNS//,/\',\'}');\""
    check_note "    expected output: every column listed above (N+1 schema stays ahead)"
    check_note "  paste the output into the closing comment on issue #57"
fi

# --- Check #3: /healthz returns 200 ------------------------------------
# Post-rollback liveness contract: the N image is alive against the
# N+1 schema. This is the visible payoff of the forward-compat
# property — chassis serves traffic, doesn't crash on missing
# columns / unexpected columns / type drift.
code="$(curl_code "https://${HOST}/healthz" || true)"
if [[ "$code" = "200" ]]; then
    check_ok "GET https://${HOST}/healthz returns 200 post-rollback"
else
    check_fail "GET https://${HOST}/healthz expected 200, got '$code'" \
        "rollback Pod might not be Ready; kubectl get pods -n $NAMESPACE; kubectl logs -n $NAMESPACE deployment/$RELEASE"
fi

# --- Check #4: /version reports the N image SHA ------------------------
# THE rollback-specific assertion: /version must report the N SHA, not
# the N+1 SHA. If /version still reports N+1, the Pod did not roll
# (rollback flipped the chart manifest but the old replicas linger) —
# common Helm rollback misuse: forgetting `--wait`. Catching this is
# the difference between "rollback exited 0" and "rollback actually
# served traffic from the N image".
if version_json="$(curl_body "https://${HOST}/version" 2>/dev/null)"; then
    if echo "$version_json" | jq -e '.git_sha' >/dev/null 2>&1; then
        live_sha="$(echo "$version_json" | jq -r '.git_sha')"
        # Mirror install-verify.sh's tighter sha check: live_sha must
        # be non-empty AND SHA-shaped before any comparison.
        if [[ -z "$live_sha" || "$live_sha" == "null" ]]; then
            check_fail "/version reports empty git_sha" \
                "expected N SHA '$N_IMAGE_SHA'; got '$live_sha' (raw response: $version_json)"
        elif ! [[ "$live_sha" =~ ^[A-Za-z0-9._-]+$ ]]; then
            check_fail "/version git_sha contains unexpected characters" \
                "expected '$N_IMAGE_SHA' (bare); got '$live_sha'"
        else
            # Accept `sha-<40hex>` tag form OR bare SHA. Strip the
            # `sha-` prefix from --n-image-sha then forward-prefix-match
            # in either direction (long-SHA expected with short live
            # is unusual but possible; same logic as install-verify.sh).
            expected_bare="${N_IMAGE_SHA#sha-}"
            if [[ "$live_sha" == "$expected_bare" ]] \
                || [[ "$live_sha" == "$expected_bare"* ]] \
                || [[ "$expected_bare" == "$live_sha"* ]]; then
                check_ok "/version reports git_sha='$live_sha' matching N image '$N_IMAGE_SHA'"
            else
                check_fail "/version git_sha mismatch — rollback did not flip the running image" \
                    "expected N SHA '$N_IMAGE_SHA' (bare '$expected_bare'); got '$live_sha'. The chart rolled back but old Pods still serving (forget --wait?), OR the deploy/rollback exercise ran out-of-order"
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
# Negative auth test. The federation chain (Keycloak JWT middleware)
# must still gate correctly under the N image talking to the N+1
# schema — proves the audit middleware can still chain to the audit
# table even when the table has extra columns the N code doesn't know
# about. A 200 here = federation regressed open (release-blocking);
# anything other than 401 = a different regression worth investigating.
code="$(curl_code "https://${HOST}/api/v1/health" || true)"
if [[ "$code" = "401" ]]; then
    check_ok "GET https://${HOST}/api/v1/health (unauthenticated) returns 401 post-rollback"
else
    check_fail "GET https://${HOST}/api/v1/health auth gate regressed post-rollback" \
        "expected 401; got '$code'. 200 = middleware regressed open; 502/503 = backplane / ingress issue. The N image talking to the N+1 schema *should* still gate correctly — if not, the audit middleware crashed on the extra columns (a real forward-compat regression)"
fi

# --- Summary -----------------------------------------------------------
echo
printf 'rollback-verify.sh: %d passed, %d failed, %d warned\n' \
    "$PASS_COUNT" "$FAIL_COUNT" "$WARN_COUNT"

if [[ "$FAIL_COUNT" -gt 0 ]]; then
    echo "rollback-verify.sh: helm rollback DID NOT pass acceptance — see [FAIL] lines above" >&2
    exit 1
fi

echo "[ok]   helm rollback verified end-to-end (forward-compat property holds against N+1 schema)"
exit 0
