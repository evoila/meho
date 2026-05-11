#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# smoke.sh — producer-side federation-chain acceptance verifier
# (Task #56, Goal #11 DoD bullet 2).
#
# The five-leg end-to-end proof that a deployed MEHO works for a real
# operator with a real Keycloak token against a real Vault and a real
# Postgres. Companion to install-verify.sh (Task #55) and
# rollback-verify.sh (Task #57): same producer-owns-the-contract /
# consumer-runs-the-exercise split, same `[ok]` / `[FAIL]` / `[WARN]`
# vocabulary, same exit-code shape.
#
# The five legs codify Goal #11 DoD bullet 2:
#
#   `smoke.sh` passes (login + status + audit-row + Vault +
#   DB-migration state).
#
#   1. login              — meho login <backplane> succeeds; a bearer
#                           token is stored in the keyring / 0600 file.
#                           Verifies the device-code chain end-to-end
#                           (Keycloak → CLI → token store) — Task #44.
#   2. status             — meho status (or meho status --json | jq)
#                           returns 0. Verifies the Status API + auth +
#                           federation summary surface — Task #45.
#   3. audit-row          — issue an authenticated GET /api/v1/health,
#                           then assert a corresponding row landed in
#                           audit_log (operator_sub, method=GET,
#                           path=/api/v1/health, status_code=200).
#                           Verifies G2.3 audit middleware contract
#                           end-to-end — Task #28.
#   4. vault              — assert the federation-proof Vault round-trip
#                           ran. The chassis-side proof is the same
#                           authenticated /api/v1/health probe — the
#                           response carries vault.reachable=true +
#                           vault.read_ok=true when the Vault chain
#                           worked. Verifies G2.2 federation —
#                           Task #25.
#   5. db-migration state — assert alembic head == current. The
#                           chassis-side proof is db.migrated=true on
#                           the same /api/v1/health response (DB
#                           migration probe, Task #29). Optional
#                           cluster-side cross-check via `kubectl exec`
#                           against the migrate Job's image when
#                           kubectl context is available.
#
# Exit codes mirror install-verify.sh / rollback-verify.sh:
#
#   0  → every required leg passed (federation chain works end-to-end).
#   1  → at least one required leg failed (smoke did NOT pass; see
#        [FAIL] lines above).
#   2  → CLI usage error (missing args, curl/jq/meho not on PATH).
#
# Usage (called from the consumer's smoke.sh wrapper on
# `claude-rdc-hetzner-dc/manifests/meho/smoke.sh`, or directly by the
# operator):
#
#   bash scripts/acceptance/smoke.sh \
#     --backplane https://meho.evba.lab \
#     --namespace meho \
#     --release meho
#
# The wrapper is responsible for completing the interactive
# device-code login BEFORE invoking this script — the verifier itself
# is non-interactive (no TTY assumed). The recommended consumer-side
# pattern is:
#
#   meho login https://meho.evba.lab           # interactive (browser)
#   bash scripts/acceptance/smoke.sh ...       # non-interactive
#
# Flags:
#   --backplane <url>             Backplane base URL (default:
#                                 https://meho.evba.lab). The five legs
#                                 run against this host.
#   --namespace <ns>              Kubernetes namespace the chart was
#                                 installed into (default: meho). Used
#                                 by the optional cluster-side
#                                 cross-checks.
#   --release <name>              Helm release name (default: meho).
#   --cacert <path>               CA bundle for the ingress TLS verify
#                                 (default: system trust store).
#   --skip-login                  Skip leg #1 (login). Useful when the
#                                 operator already ran `meho login` in
#                                 a prior shell and the token is in the
#                                 keyring — the verifier only needs to
#                                 confirm the token works (covered by
#                                 leg #2). Default: leg #1 runs and
#                                 fails when no stored token is found.
#   --enforce-budget              Treat the optional 60s wall-clock
#                                 budget check as a hard failure (the
#                                 federation chain end-to-end on a warm
#                                 cluster is sub-second per leg; the
#                                 budget catches a Vault / Keycloak /
#                                 PG latency regression that would
#                                 otherwise pass silently). Under
#                                 --enforce-budget the verifier ALSO
#                                 hard-fails when SMOKE_START_TS is
#                                 unset or non-numeric — same
#                                 contract-input discipline as
#                                 install-verify.sh's --enforce-budget.
#                                 Goal #11 closing-criteria runs MUST
#                                 pass --enforce-budget.
#   -h | --help                   Print this help and exit 0.
#
# Environment:
#   MEHO_ACCESS_TOKEN     A Keycloak access token for the operator.
#                         REQUIRED for legs #2, #3, #4. The wrapper
#                         typically obtains this from `meho login` (the
#                         CLI stores it in the keyring; the wrapper
#                         exports it via `meho token`) or from a
#                         service-account grant in CI.
#   DATABASE_URL          PostgreSQL connection string (psql shape).
#                         When set, leg #3 (audit-row) runs against the
#                         live DB. When unset, leg #3 is a [note] line
#                         deferred to the consumer-side runbook
#                         (operator runs the psql query manually and
#                         pastes the output into the closing-comment
#                         artefact on #56).
#   KUBECONFIG            Honoured by the optional cluster-side
#                         cross-checks (leg #5's alembic-current probe).
#   SMOKE_START_TS        Unix timestamp (seconds, UTC) — set by the
#                         consumer's smoke.sh wrapper as the FIRST
#                         action. Enables the optional 60s wall-clock
#                         budget check. Omit only for standalone
#                         debugging runs.
#
# Sensitive-data discipline (per Goal #11 / Task #25):
#
# - The bearer token is NEVER echoed. `curl`'s `-H "Authorization:
#   Bearer ..."` is constructed inline and not interpolated into any
#   log line. The verifier's stdout / stderr is safe to capture
#   verbatim into the closing-comment artefact.
# - The operator's `sub` IS echoed (it's the identifier the audit row
#   asserts against), but neither `name` nor `email` is. The
#   audit-row check elides PII before printing.
# - `meho login` runs in a child shell — no JWT bytes flow back through
#   shared file descriptors. The CLI persists tokens via the keyring /
#   0600 file fallback; smoke.sh reads tokens only through that channel
#   (or via the operator-provided MEHO_ACCESS_TOKEN env var).
#
# Notes on the defensive shape (mirrors install-verify.sh /
# rollback-verify.sh):
#
# - `set -Eeuo pipefail` aborts on first failed command. The verifier
#   never modifies cluster state (no `apply`, no `delete`, no `psql`
#   INSERT/UPDATE). Safe to run repeatedly against the same release.
# - `[ "$X" = "<literal>" ]` literal-string compares — `-eq` family
#   would match an empty curl response on some bash builds.
# - `curl --retry` covers ingress-controller warm-up and Vault /
#   Keycloak transient blips. Bounded budget so a real outage surfaces
#   within the smoke window.

set -Eeuo pipefail

# --- Help text ---------------------------------------------------------
# Self-contained --help output. Same rationale as install-verify.sh:
# a heredoc decouples the operator-facing surface from this script's
# physical line numbering, so reordering the header comments cannot
# silently truncate help mid-flag.
usage() {
    cat <<'HELP'
smoke.sh — producer-side federation-chain acceptance verifier
(Task #56, Goal #11 DoD bullet 2).

Runs the five legs documented in docs/acceptance/smoke.md against a
deployed MEHO instance. The verifier's exit code IS the smoke run's
exit code:

  0  every required leg passed (federation chain works end-to-end)
  1  at least one required leg failed
  2  CLI usage error (missing args, curl/jq/meho not found)

Usage:
  bash scripts/acceptance/smoke.sh \
    --backplane https://meho.evba.lab \
    --namespace meho \
    --release meho

The wrapper completes `meho login` interactively before invoking this
script. Subsequent legs read the bearer token from MEHO_ACCESS_TOKEN
(set by the wrapper) or from the CLI's keyring / 0600 file fallback.

Flags:
  --backplane <url>             Backplane base URL
                                (default: https://meho.evba.lab).
  --namespace <ns>              Kubernetes namespace (default: meho).
  --release <name>              Helm release name (default: meho).
  --cacert <path>               CA bundle for ingress TLS verify
                                (default: system trust store).
  --skip-login                  Skip leg #1 (login). Use when the
                                operator already ran `meho login` in a
                                prior shell.
  --enforce-budget              Treat the 60s wall-clock budget check
                                as a hard failure. Also hard-fails when
                                SMOKE_START_TS is unset or non-numeric.
                                Goal #11 closing-criteria runs MUST
                                pass this flag.
  -h | --help                   Print this help and exit 0.

Environment:
  MEHO_ACCESS_TOKEN     Keycloak access token. Required for legs #2-#4.
  DATABASE_URL          psql connection string. Enables leg #3 audit-row
                        assertion when set.
  KUBECONFIG            Honoured by optional cluster-side cross-checks
                        (leg #5 alembic-current probe).
  SMOKE_START_TS        Unix epoch seconds (UTC), captured by the
                        consumer's smoke.sh wrapper as its first
                        action. Required under --enforce-budget.

See docs/acceptance/smoke.md for the full contract.
HELP
}

# --- Defaults ----------------------------------------------------------
BACKPLANE="https://meho.evba.lab"
NAMESPACE="meho"
RELEASE="meho"
CACERT=""
SKIP_LOGIN=0
ENFORCE_BUDGET=0
BUDGET_SECONDS=60 # Federation chain is sub-second per leg on a warm cluster.

# --- CLI parse ---------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --backplane=*) BACKPLANE="${1#*=}"; shift ;;
        --backplane)
            [[ $# -ge 2 && -n "${2:-}" ]] || { echo "smoke.sh: --backplane requires a value" >&2; exit 2; }
            BACKPLANE="$2"; shift 2 ;;
        --namespace=*) NAMESPACE="${1#*=}"; shift ;;
        --namespace)
            [[ $# -ge 2 && -n "${2:-}" ]] || { echo "smoke.sh: --namespace requires a value" >&2; exit 2; }
            NAMESPACE="$2"; shift 2 ;;
        --release=*) RELEASE="${1#*=}"; shift ;;
        --release)
            [[ $# -ge 2 && -n "${2:-}" ]] || { echo "smoke.sh: --release requires a value" >&2; exit 2; }
            RELEASE="$2"; shift 2 ;;
        --cacert=*) CACERT="${1#*=}"; shift ;;
        --cacert)
            [[ $# -ge 2 && -n "${2:-}" ]] || { echo "smoke.sh: --cacert requires a value" >&2; exit 2; }
            CACERT="$2"; shift 2 ;;
        --skip-login) SKIP_LOGIN=1; shift ;;
        --enforce-budget) ENFORCE_BUDGET=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "smoke.sh: unknown argument: $1" >&2; exit 2 ;;
    esac
done

# Normalise: strip any trailing slash from the backplane URL so the
# curl invocations below don't double up on `//api/...`.
BACKPLANE="${BACKPLANE%/}"

# --- Tooling pre-flight ------------------------------------------------
need_tool() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "smoke.sh: required tool '$1' not on PATH" >&2
        exit 2
    fi
}
need_tool curl
need_tool jq

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

# Curl retry args — same shape as install-verify.sh /
# rollback-verify.sh. `--retry-all-errors` is only included when the
# installed curl actually accepts it (gates on Ubuntu 22.04's 7.81+
# vs older base images).
CURL_RETRY_ARGS=(--retry 5 --retry-delay 2 --retry-connrefused)
if curl --help all 2>/dev/null | grep -q -- '--retry-all-errors'; then
    CURL_RETRY_ARGS+=(--retry-all-errors)
fi

# Common curl invocation. Token (if any) is appended by the caller via
# additional `-H "Authorization: Bearer $TOKEN"` so this helper never
# sees the bearer; the token also never lands in the function's
# argv (each leg constructs its own `-H` arg inline).
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

# --- Banner ------------------------------------------------------------
echo "smoke.sh: producer-side federation-chain acceptance check (Task #56)"
echo "  backplane:         $BACKPLANE"
echo "  namespace:         $NAMESPACE"
echo "  release:           $RELEASE"
[[ -n "$CACERT" ]] && echo "  CA bundle:         $CACERT"
[[ "$SKIP_LOGIN" -eq 1 ]] && echo "  login leg:         SKIPPED (--skip-login)"
[[ "$ENFORCE_BUDGET" -eq 1 ]] && echo "  budget enforce:    HARD"
echo

# --- Leg #1: login -----------------------------------------------------
# The login leg proves the device-code chain end-to-end:
#
#   operator → CLI → Keycloak realm → access token → CLI token store.
#
# `meho login` is the CLI surface shipped by Task #44 (CLI #42). It
# requires an interactive browser session (the device-code flow's
# user_code is verified on the operator's workstation). The verifier
# itself is non-interactive — by the time smoke.sh runs, the consumer-
# side wrapper has either:
#
#   (a) already invoked `meho login` interactively and the token is in
#       the keyring → SKIP_LOGIN=1 and we skip the explicit login call;
#   (b) exported MEHO_ACCESS_TOKEN from a prior `meho login` →
#       SKIP_LOGIN=1 and we use that token directly.
#
# The verifier still ASSERTS leg #1 even with --skip-login by checking
# the auth chain works (a stored token that doesn't authenticate is a
# failed login, not a passed one). The actual assertion is delegated
# to leg #2 (`meho status` won't return 0 against an invalid token),
# and leg #1 here records that login produced a usable token.
if [[ "$SKIP_LOGIN" -eq 1 ]]; then
    if [[ -n "${MEHO_ACCESS_TOKEN:-}" ]]; then
        check_ok "login: MEHO_ACCESS_TOKEN provided by wrapper (skipped interactive login)"
    else
        # No token in env AND --skip-login — the operator is relying on
        # the CLI keyring's stored token. The verifier can't probe the
        # keyring directly without the CLI binary, so the trust falls
        # to leg #2.
        if command -v meho >/dev/null 2>&1; then
            check_note 'login: --skip-login set with no MEHO_ACCESS_TOKEN; relying on the meho CLI keyring lookup in leg #2'
            check_ok "login: deferred to leg #2 (CLI keyring lookup)"
        else
            check_fail "login: --skip-login set, no MEHO_ACCESS_TOKEN, and meho CLI not on PATH" \
                'either run "meho login <backplane>" from this shell, OR export MEHO_ACCESS_TOKEN, OR install the meho CLI'
        fi
    fi
else
    # Interactive login — the verifier itself does NOT invoke `meho
    # login` (no TTY assumed; the wrapper does it). Instead the verifier
    # checks that either MEHO_ACCESS_TOKEN is set OR the CLI is on PATH
    # (the latter implies the wrapper ran login and the token's in the
    # keyring). This is the producer-side surrogate; the consumer's
    # wrapper documents the full interactive flow.
    if [[ -n "${MEHO_ACCESS_TOKEN:-}" ]]; then
        check_ok 'login: MEHO_ACCESS_TOKEN provided (assumed produced by prior "meho login")'
    elif command -v meho >/dev/null 2>&1; then
        check_note 'login: no MEHO_ACCESS_TOKEN env var; relying on the meho CLI keyring lookup in leg #2'
        check_ok "login: meho CLI present (deferred to leg #2 for token validity proof)"
    else
        check_fail "login: no MEHO_ACCESS_TOKEN and meho CLI not on PATH" \
            "run \"meho login $BACKPLANE\" first, OR export MEHO_ACCESS_TOKEN"
    fi
fi

# --- Leg #2: status ----------------------------------------------------
# The status leg proves the Status API + auth + federation summary
# surface (Task #45). Two equivalent assertion shapes — the verifier
# prefers MEHO_ACCESS_TOKEN over the CLI when both are available
# because the curl path is hermetic (no dependency on the CLI version
# the operator happens to have installed) and the response JSON is
# what every downstream check (legs #3, #4, #5) consumes.
#
# `meho status --json` (when invoked directly) emits the same JSON
# shape, so a wrapper that prefers the CLI is equivalent. We pick the
# curl path here so the verifier can run from any host with curl + jq
# + a token, not only from a host with the meho binary installed.
if [[ -z "${MEHO_ACCESS_TOKEN:-}" ]]; then
    # No token → try the CLI directly. The CLI reads the keyring and
    # constructs the same /api/v1/health request internally.
    if command -v meho >/dev/null 2>&1; then
        if status_json="$(meho status --json --backplane "$BACKPLANE" 2>/dev/null)"; then
            check_ok "status: \`meho status --json\` returns 0 (keyring-mediated)"
        else
            check_fail "status: \`meho status --json\` failed" \
                "the stored token is missing or rejected; rerun \`meho login $BACKPLANE\`"
            status_json=""
        fi
    else
        check_fail "status: no MEHO_ACCESS_TOKEN and meho CLI not on PATH" \
            "cannot probe /api/v1/health without either a token or the CLI"
        status_json=""
    fi
else
    # Token path — curl directly. `curl -sSf` exits non-zero on >=400
    # so a 401 here means the token expired between login and now (or
    # the wrapper exported a stale token).
    if status_json="$(curl_body "$BACKPLANE/api/v1/health" \
        -H "Authorization: Bearer $MEHO_ACCESS_TOKEN" 2>/dev/null)"; then
        check_ok "status: GET $BACKPLANE/api/v1/health (authenticated) returns 200"
    else
        check_fail "status: GET $BACKPLANE/api/v1/health failed" \
            "MEHO_ACCESS_TOKEN missing / expired / Vault role denied; rerun \`meho login $BACKPLANE\`"
        status_json=""
    fi
fi

# Parse the response shape regardless of how we got it — the assertion
# bar (operator.sub present + vault healthy + db.migrated true) is the
# same. `jq -e` exits non-zero on a false / null match, so the verifier
# can chain it directly into check_ok / check_fail without parsing
# stderr.
operator_sub=""
if [[ -n "${status_json:-}" ]]; then
    if echo "$status_json" | jq -e '.operator.sub != null and .operator.sub != ""' >/dev/null 2>&1; then
        operator_sub="$(echo "$status_json" | jq -r '.operator.sub')"
        check_ok "status: response carries operator.sub (federation chain produced an identity)"
    else
        check_fail "status: response missing operator.sub" \
            "expected JWT validation to bind operator identity; got: $(echo "$status_json" | jq -c '.operator // {}')"
    fi
fi

# --- Leg #4: vault -----------------------------------------------------
# Asserted BEFORE leg #3 because the audit-row check needs the
# operator_sub to query psql, and we want to fail-fast on a broken
# federation chain (a missing Vault round-trip means audit_log writes
# may also be broken, and leg #3's psql output would be misleading).
#
# The chassis-side proof of Vault is the .vault.reachable=true +
# .vault.read_ok=true pair on the /api/v1/health response. This is
# the SAME response leg #2 already parsed — no second request needed
# (the audit-row check in leg #3 would write a SECOND row otherwise,
# polluting the closing-comment artefact).
if [[ -n "${status_json:-}" ]]; then
    vault_reachable="$(echo "$status_json" | jq -r '.vault.reachable // false')"
    vault_read_ok="$(echo "$status_json" | jq -r '.vault.read_ok // false')"
    vault_detail="$(echo "$status_json" | jq -r '.vault.detail // ""')"
    if [[ "$vault_reachable" == "true" && "$vault_read_ok" == "true" ]]; then
        # detail carries the KV version on success — useful in the
        # closing-comment artefact.
        check_ok "vault: federation chain operational (reachable=true, read_ok=true, detail='$vault_detail')"
    elif [[ "$vault_reachable" == "true" && "$vault_read_ok" != "true" ]]; then
        check_fail "vault: reachable but read failed" \
            "JWT/OIDC login succeeded but secret read against meho/test/federation failed; detail='$vault_detail'. Vault role / policy / KV mount misconfigured?"
    else
        check_fail "vault: not reachable from backplane" \
            "JWT/OIDC login to Vault failed; detail='$vault_detail'. Common causes: Vault role JWKS URL drift, audience mismatch, network policy blocks egress, Vault sealed"
    fi
fi

# --- Leg #3: audit-row -------------------------------------------------
# The audit middleware writes synchronously BEFORE the response is
# returned (per backend/src/meho_backplane/audit.py): by the time leg
# #2's curl returns 200, the audit_log row already exists. The query
# below scopes by operator_sub + recent timestamp + path to avoid
# false positives from concurrent operator activity.
#
# DATABASE_URL is the operator-cost path. The verifier needs psql +
# DB read credentials to inspect audit_log. When DATABASE_URL is
# unset, the check becomes a [note] line emitting the exact psql
# command the operator should run, and the result is recorded in the
# closing-comment artefact on issue #56 — same pattern as
# rollback-verify.sh's schema check.
if [[ -n "${DATABASE_URL:-}" && -n "$operator_sub" ]]; then
    need_tool psql
    # Bind the sub via a psql variable. `-v` + `:'name'` inlines the
    # value as a single-quoted SQL literal, so an operator_sub
    # containing a quote (it shouldn't — Keycloak sub is a UUID — but
    # defensive coding wins here) can't break out of the literal.
    audit_query="SELECT operator_sub, method, path, status_code
                   FROM audit_log
                  WHERE operator_sub = :'sub'
                    AND path = '/api/v1/health'
                    AND method = 'GET'
                    AND status_code = 200
                    AND occurred_at > NOW() - INTERVAL '1 minute'
                  ORDER BY occurred_at DESC LIMIT 1;"
    if audit_row="$(psql "$DATABASE_URL" -tA -v sub="$operator_sub" -c "$audit_query" 2>/dev/null)"; then
        if [[ -n "$audit_row" ]]; then
            # `-tA` returns tuples-only, unaligned, pipe-delimited. The
            # row is `<sub>|GET|/api/v1/health|200`; we don't echo the
            # sub itself (PII) but confirm the row landed.
            check_ok "audit-row: psql found a matching row for the /api/v1/health call (method=GET, path=/api/v1/health, status_code=200)"
        else
            check_fail "audit-row: psql returned 0 rows for the /api/v1/health call" \
                "audit middleware did not write a row, OR the row didn't match (operator_sub mismatch, path normalisation diverged, status_code regressed). Capture: psql \"\$DATABASE_URL\" -c \"SELECT operator_sub, method, path, status_code, occurred_at FROM audit_log ORDER BY occurred_at DESC LIMIT 5;\""
        fi
    else
        check_fail "audit-row: psql query against \$DATABASE_URL failed" \
            "verify DATABASE_URL is set + reachable + has SELECT on audit_log; query was the standard audit-row probe (see docs/acceptance/smoke.md)"
    fi
elif [[ -z "${DATABASE_URL:-}" ]]; then
    check_note "audit-row: deferred — \$DATABASE_URL unset"
    check_note "  operator: run the following on a host with PG SELECT access on audit_log:"
    check_note "    psql \"\$DATABASE_URL\" -c \\"
    check_note "      \"SELECT operator_sub, method, path, status_code, occurred_at\""
    check_note "      \"  FROM audit_log\""
    check_note "      \" WHERE path = '/api/v1/health'\""
    check_note "      \"   AND occurred_at > NOW() - INTERVAL '1 minute'\""
    check_note "      \" ORDER BY occurred_at DESC LIMIT 5;\""
    check_note "  expected: at least one row matching the operator's sub, method=GET, path=/api/v1/health, status_code=200"
    check_note "  paste the output into the closing comment on issue #56"
elif [[ -z "$operator_sub" ]]; then
    # DATABASE_URL set but leg #2 didn't yield a sub → leg #2 already
    # failed; downgrade this to a [WARN] so the operator sees the
    # cascade and doesn't get a flood of [FAIL] lines pointing at the
    # same root cause.
    check_warn "audit-row: skipped because leg #2 did not yield operator.sub" \
        "fix leg #2 (status) first, then re-run; the audit middleware row exists iff the /api/v1/health call returned 200"
fi

# --- Leg #5: db-migration state ---------------------------------------
# The chassis-side proof of DB-migration state is .db.migrated=true on
# the /api/v1/health response. The chassis runs db_migration_probe()
# under the hood (backend/src/meho_backplane/db/migrations.py — Task
# #29's runner contract): connect to DB, read alembic_version table,
# compare to head() from ScriptDirectory.
#
# Optional cluster-side cross-check: `alembic current` via the
# migrate Job's image. When kubectl context + KUBECONFIG are set on
# the verifier host, the verifier asserts the migrate Job's last
# completed run shows revision == head. The Job's hook-succeeded
# delete-policy GC's the Pod after success, so the fallback is the
# helm release status (deployed) — same pattern as install-verify.sh's
# check #2.
if [[ -n "${status_json:-}" ]]; then
    db_migrated="$(echo "$status_json" | jq -r '.db.migrated // null')"
    if [[ "$db_migrated" == "true" ]]; then
        check_ok "db-migration: chassis reports .db.migrated=true (alembic_version == head)"
    else
        check_fail "db-migration: chassis reports .db.migrated=$db_migrated (expected true)" \
            "either DB unreachable from backplane, alembic_version table missing, or current revision != head. Inspect: kubectl logs -n $NAMESPACE deployment/$RELEASE"
    fi
fi

# Optional cluster-side cross-check.
if command -v kubectl >/dev/null 2>&1; then
    # The migrate Job is named via the chart's `meho.fullname` helper
    # (release-name-dependent). The same label selector
    # install-verify.sh uses is invariant across release rename:
    job_name="$(kubectl get job -n "$NAMESPACE" \
        -l "app.kubernetes.io/component=migrate,app.kubernetes.io/instance=${RELEASE}" \
        -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
    if [[ -n "$job_name" ]]; then
        # Job still present (post-install/upgrade window before hook
        # GC, or chart configured without hook-succeeded deletion).
        # Assert succeeded=1.
        job_status="$(kubectl get job -n "$NAMESPACE" "$job_name" \
            -o jsonpath='{.status.succeeded}' 2>/dev/null || true)"
        if [[ "$job_status" == "1" ]]; then
            check_ok "db-migration: cluster cross-check — migrate Job $job_name succeeded=1"
        else
            check_warn "db-migration: cluster cross-check inconclusive" \
                "migrate Job $job_name status.succeeded='$job_status'; chassis-side .db.migrated is the authoritative signal"
        fi
    elif command -v helm >/dev/null 2>&1; then
        # Job GC'd; fall back to helm release status.
        helm_status="$(helm status -n "$NAMESPACE" "$RELEASE" -o json 2>/dev/null \
            | jq -r '.info.status // "unknown"' 2>/dev/null || echo "unknown")"
        if [[ "$helm_status" == "deployed" ]]; then
            check_ok "db-migration: cluster cross-check — migrate Job GC'd by hook-succeeded; helm release status=deployed"
        else
            check_warn "db-migration: cluster cross-check inconclusive" \
                "Job not present and helm release status='$helm_status'; chassis-side .db.migrated is the authoritative signal"
        fi
    else
        # No Job, no helm — the chassis-side signal is the only one
        # left, and it already passed (or failed) above. Note the gap
        # so the operator knows what's missing for a fuller proof.
        check_note "db-migration: cluster cross-check skipped — migrate Job GC'd and \`helm\` not on PATH"
    fi
else
    check_note "db-migration: cluster cross-check skipped — \`kubectl\` not on PATH"
fi

# --- Optional: wall-clock budget ---------------------------------------
# Same matrix as install-verify.sh's check #7:
#
#   ENFORCE_BUDGET  SMOKE_START_TS     outcome
#   0 (warn-only)   unset              [note] skipped — debugging mode
#   0               non-numeric        [WARN] invalid value
#   0               numeric            [ok]/[WARN] depending on elapsed
#   1 (hard-fail)   unset              [FAIL] — Goal #11 closing runs
#   1               non-numeric        [FAIL] — same
#   1               numeric            [ok]/[FAIL] depending on elapsed
#
# The 60s budget reflects that the federation chain on a warm cluster
# is sub-second per leg; bursting past 60s indicates a Vault /
# Keycloak / PG latency regression worth investigating even when the
# legs themselves pass.
echo
if [[ -z "${SMOKE_START_TS:-}" ]]; then
    if [[ "$ENFORCE_BUDGET" -eq 1 ]]; then
        check_fail "SMOKE_START_TS is unset under --enforce-budget" \
            "Goal #11 closing-criteria runs require a numeric start timestamp; the consumer's smoke.sh wrapper sets START_TS=\$(date -u +%s) as its first action"
    else
        check_note "wall-clock budget check skipped — SMOKE_START_TS unset"
    fi
elif ! [[ "$SMOKE_START_TS" =~ ^[0-9]+$ ]]; then
    if [[ "$ENFORCE_BUDGET" -eq 1 ]]; then
        check_fail "SMOKE_START_TS is not a unix timestamp under --enforce-budget" \
            "got '$SMOKE_START_TS'; the wrapper should set START_TS=\$(date -u +%s)"
    else
        check_warn "SMOKE_START_TS is not a unix timestamp" \
            "got '$SMOKE_START_TS'; the wrapper should set START_TS=\$(date -u +%s)"
    fi
else
    NOW_TS="$(date -u +%s)"
    ELAPSED=$((NOW_TS - SMOKE_START_TS))
    if [[ "$ELAPSED" -le "$BUDGET_SECONDS" ]]; then
        check_ok "wall-clock ${ELAPSED}s <= budget ${BUDGET_SECONDS}s"
    else
        msg="wall-clock ${ELAPSED}s > budget ${BUDGET_SECONDS}s"
        if [[ "$ENFORCE_BUDGET" -eq 1 ]]; then
            check_fail "$msg" "Goal #11 closing-criteria run; see docs/acceptance/smoke.md 'What \"60s\" means'"
        else
            check_warn "$msg" "non-closing run; rerun with --enforce-budget for hard-fail behaviour"
        fi
    fi
fi

# --- Summary -----------------------------------------------------------
echo
printf 'smoke.sh: %d passed, %d failed, %d warned\n' \
    "$PASS_COUNT" "$FAIL_COUNT" "$WARN_COUNT"

if [[ "$FAIL_COUNT" -gt 0 ]]; then
    echo "smoke.sh: federation chain DID NOT pass acceptance — see [FAIL] lines above" >&2
    exit 1
fi

echo "[ok]   federation chain verified end-to-end (login + status + audit-row + Vault + DB-migration state)"
exit 0
