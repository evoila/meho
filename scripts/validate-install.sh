#!/usr/bin/env bash
# scripts/validate-install.sh
#
# Maintainer smoke test: runs preflight, then probes the running stack
# (backend /health, frontend, Keycloak, one real chat roundtrip). Use this
# before cutting a release.
#
# Usage: ./scripts/validate-install.sh
#
# Companion to scripts/preflight.sh (pre-up host/env checks). This script is
# NOT invoked from CI — the CI bootstrap smoke job is owned by Goal #294.
# See docs/codebase/first-run-experience.md for context and
# docs/development/dual-repo-workflow.md for the release-owner checklist.

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Per-run scratch file for capturing a failing step's output. mktemp + EXIT
# trap so the file is cleaned up even on SIGINT mid-step.
TMP_OUT="$(mktemp -t validate-install.XXXXXX)"
trap 'rm -f "$TMP_OUT"' EXIT

# --- Configuration (overridable via env) --------------------------------------

HEALTH_URL="${HEALTH_URL:-http://localhost:8000/health}"
FRONTEND_URL="${FRONTEND_URL:-http://localhost:5173}"
KEYCLOAK_URL="${KEYCLOAK_URL:-http://localhost:8080}"
CHAT_URL="${CHAT_URL:-http://localhost:8000/api/chat/stream}"
KEYCLOAK_REALM="${KEYCLOAK_REALM:-meho-tenant}"
KEYCLOAK_CLIENT="${KEYCLOAK_CLIENT:-meho-frontend}"
# Seeded dev-realm credentials, not a production secret. Match the user seeded
# by config/keycloak/meho-tenant-realm.json (same pattern as
# tests/support/auth.py). Override SMOKE_USERNAME/SMOKE_PASSWORD when running
# against a non-default realm. NOSONAR  # noqa: S107
SMOKE_USERNAME="${SMOKE_USERNAME:-user@meho.local}"
SMOKE_PASSWORD="${SMOKE_PASSWORD:-user123}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-180}"

TOTAL_STEPS=5
FAILED_STEP=""
FAILED_LABEL=""

# --- Color setup (respects NO_COLOR and non-TTY stdout) -----------------------

if [[ -n "${NO_COLOR:-}" ]] || [[ ! -t 1 ]]; then
  RED=''
  GREEN=''
  YELLOW=''
  BOLD=''
  NC=''
else
  RED=$'\033[0;31m'
  GREEN=$'\033[0;32m'
  YELLOW=$'\033[1;33m'
  BOLD=$'\033[1m'
  NC=$'\033[0m'
fi

# --- Step runner --------------------------------------------------------------

# run_step N LABEL FN — runs FN, prints aligned "[N/M] label ... ✓ (Xs)" or
# "✗ (Xs)". Records failure in FAILED_STEP / FAILED_LABEL and returns 1 so the
# main flow can short-circuit.
run_step() {
  local n=$1 label=$2 fn=$3
  local start end elapsed rc=0
  start=$(date +%s)
  printf '[%d/%d] %-32s' "$n" "$TOTAL_STEPS" "$label"
  "$fn" >"$TMP_OUT" 2>&1 || rc=$?
  end=$(date +%s)
  elapsed=$((end - start))
  if [[ $rc -eq 0 ]]; then
    printf ' %s✓%s (%ds)\n' "$GREEN" "$NC" "$elapsed"
    return 0
  fi
  printf ' %s✗%s (%ds)\n' "$RED" "$NC" "$elapsed"
  if [[ -s "$TMP_OUT" ]]; then
    printf '%s\n' "---"
    sed 's/^/    /' "$TMP_OUT"
    printf '%s\n' "---"
  fi
  FAILED_STEP=$n
  FAILED_LABEL=$label
  return 1
}

# --- Step implementations -----------------------------------------------------

step_preflight() {
  bash "${PROJECT_ROOT}/scripts/preflight.sh"
}

# Polls $url every 2s until it returns 2xx or timeout_sec elapses.
wait_for_url() {
  local url=$1 timeout_sec=$2
  local deadline=$(( $(date +%s) + timeout_sec ))
  while (( $(date +%s) < deadline )); do
    if curl -sf --max-time 2 -o /dev/null "$url"; then
      return 0
    fi
    sleep 2
  done
  echo "Timed out waiting for ${url} after ${timeout_sec}s" >&2
  return 1
}

step_health() {
  wait_for_url "$HEALTH_URL" "$HEALTH_TIMEOUT"
}

step_frontend() {
  # Task #279 step 3 requires asserting the response looks like the frontend
  # HTML, not just a 2xx — a proxy or stale service on :5173 returning 200
  # otherwise passes this check silently.
  local raw body code marker='__HTTP__'
  raw=$(curl -sS -L --max-time 10 -w "\n${marker}%{http_code}${marker}" "$FRONTEND_URL") || {
    echo "Frontend probe: curl failed to reach ${FRONTEND_URL}" >&2
    return 1
  }
  # Split raw into body + code around the single-line marker suffix curl added.
  # Strip everything up to and including the first \n__HTTP__ to get CODE__HTTP__.
  code=${raw##*$'\n'"${marker}"}
  # Then strip the trailing __HTTP__ to isolate the numeric status code.
  code=${code%%"${marker}"*}
  # Body is everything before the \n__HTTP__ suffix.
  body=${raw%$'\n'"${marker}"*}
  if [[ ! "$code" =~ ^[23][0-9][0-9]$ ]]; then
    echo "Frontend returned HTTP ${code} (expected 2xx/3xx)" >&2
    return 1
  fi
  # Vite-served index.html always has one of these markers.
  if [[ "$body" == *'<div id="root"'* ]] \
     || [[ "$body" == *'<!doctype html'* ]] \
     || [[ "$body" == *'<!DOCTYPE html'* ]]; then
    return 0
  fi
  echo "Frontend returned HTTP ${code} but body doesn't look like the MEHO frontend HTML:" >&2
  printf '%s\n' "$body" | head -c 200 >&2
  echo >&2
  return 1
}

step_keycloak() {
  curl -sf --max-time 10 -o /dev/null "${KEYCLOAK_URL}/health/ready"
}

# POSTs password grant to Keycloak, prints the access_token on stdout, or fails
# with exit 1 and a diagnostic on stderr.
get_keycloak_token() {
  local token_url="${KEYCLOAK_URL}/realms/${KEYCLOAK_REALM}/protocol/openid-connect/token"
  local response token
  response=$(curl -s --max-time 15 \
    -X POST \
    -H "Content-Type: application/x-www-form-urlencoded" \
    --data-urlencode "grant_type=password" \
    --data-urlencode "client_id=${KEYCLOAK_CLIENT}" \
    --data-urlencode "username=${SMOKE_USERNAME}" \
    --data-urlencode "password=${SMOKE_PASSWORD}" \
    "$token_url")
  if [[ -z "$response" ]]; then
    echo "Keycloak token endpoint returned no response" >&2
    return 1
  fi
  if command -v jq >/dev/null 2>&1; then
    token=$(printf '%s' "$response" | jq -r '.access_token // empty')
  else
    token=$(printf '%s' "$response" \
      | grep -o '"access_token"[[:space:]]*:[[:space:]]*"[^"]*"' \
      | head -n1 \
      | sed 's/.*"access_token"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/')
  fi
  if [[ -z "$token" ]]; then
    echo "Could not extract access_token from Keycloak response:" >&2
    echo "$response" >&2
    return 1
  fi
  printf '%s' "$token"
}

step_chat_roundtrip() {
  local token
  token=$(get_keycloak_token) || return 1
  # SSE endpoint — read enough bytes to observe the first typed event.
  # Pipefail is disabled inside the subshell so head's early close doesn't
  # kill the pipeline; the subshell scope keeps the setting local.
  # 2 KiB is comfortably larger than any single SSE frame the backend emits.
  local first_bytes
  first_bytes=$(
    set +o pipefail
    curl -sN --max-time 60 \
      -H "Authorization: Bearer ${token}" \
      -H "Content-Type: application/json" \
      -X POST \
      -d '{"message":"hello","session_mode":"ask"}' \
      "$CHAT_URL" 2>/dev/null | head -c 2048
  )
  if [[ -z "$first_bytes" ]]; then
    echo "Chat endpoint returned empty body (likely HTTP error before stream)" >&2
    return 1
  fi
  # The backend emits errors as SSE 'data: {"type":"error",...}' frames
  # (meho_app/api/routes_chat.py). Detecting those is the whole point of this
  # step per #279, so reject the error type explicitly before accepting any
  # SSE framing.
  if [[ "$first_bytes" == *'"type":"error"'* ]] \
     || [[ "$first_bytes" == *'"type": "error"'* ]]; then
    echo "Chat endpoint emitted an SSE error event:" >&2
    echo "$first_bytes" >&2
    return 1
  fi
  # Require at least one positive event type to avoid greening on bare SSE
  # framing. Accepted types from the endpoint docstring: thought, action,
  # observation, approval_required, final_answer, done, context_usage.
  local t
  for t in 'thought' 'action' 'observation' 'approval_required' \
           'final_answer' 'done' 'context_usage'; do
    if [[ "$first_bytes" == *"\"type\":\"${t}\""* ]] \
       || [[ "$first_bytes" == *"\"type\": \"${t}\""* ]]; then
      return 0
    fi
  done
  echo "Chat endpoint returned SSE framing but no recognized event type:" >&2
  echo "$first_bytes" >&2
  return 1
}

# --- Main ---------------------------------------------------------------------

main() {
  local start end total
  start=$(date +%s)

  printf '%sMEHO install validation%s\n' "$BOLD" "$NC"
  printf '%s\n' '───────────────────────'

  run_step 1 "Preflight"           step_preflight       || true
  if [[ -z "$FAILED_STEP" ]]; then
    run_step 2 "Backend /health"    step_health          || true
  fi
  if [[ -z "$FAILED_STEP" ]]; then
    run_step 3 "Frontend reachable" step_frontend        || true
  fi
  if [[ -z "$FAILED_STEP" ]]; then
    run_step 4 "Keycloak ready"     step_keycloak        || true
  fi
  if [[ -z "$FAILED_STEP" ]]; then
    run_step 5 "Chat roundtrip"     step_chat_roundtrip  || true
  fi

  end=$(date +%s)
  total=$((end - start))

  printf '\n'
  if [[ -z "$FAILED_STEP" ]]; then
    printf '%sSmoke OK%s (%d/%d passed in %ds)\n' \
      "$GREEN" "$NC" "$TOTAL_STEPS" "$TOTAL_STEPS" "$total"
    return 0
  fi
  printf '%sSmoke FAILED%s (step %d: %s) — %ds\n' \
    "$RED" "$NC" "$FAILED_STEP" "$FAILED_LABEL" "$total"
  printf '%sRerun after fixing the failing step.%s\n' "$YELLOW" "$NC"
  return 1
}

main "$@"
