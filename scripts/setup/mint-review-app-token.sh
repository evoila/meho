#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# mint-review-app-token.sh — mint a short-lived GitHub App installation
# token for the MEHO automation review identity (#2733).
#
# The autonomous review skills post their formal APPROVE /
# REQUEST_CHANGES verdict under a dedicated GitHub App so the verdict
# is a real second-party review (the PR author identity cannot approve
# its own PR). This script turns the App's credentials into the
# 1-hour installation token those skills export as GH_TOKEN for the
# review-posting call — and nothing else.
#
# Flow (see docs/codebase/automation-review-identity.md):
#   1. RS256-signed JWT: iss = client id, iat = now-60s, exp = now+9min
#      (inside GitHub's 10-minute cap).
#   2. GET /repos/<repo>/installation with the JWT -> installation id.
#   3. POST /app/installations/<id>/access_tokens -> token.
#
# Output contract (load-bearing for callers):
#   - stdout: the installation token, nothing else.
#   - stderr: all diagnostics.
#   - exit 0 only when a token was minted; any failure exits non-zero
#     with a specific reason (fail-loud — the review skills treat a
#     non-zero exit as "machine identity unavailable" and degrade
#     explicitly; they never guess).
#
# Usage:
#   mint-review-app-token.sh --client-id <id> --key-file <pem-path|->
#                            [--repo owner/name]
#
#   Environment fallbacks: MEHO_REVIEW_APP_CLIENT_ID,
#   MEHO_REVIEW_APP_KEY_FILE. --key-file - reads the PEM from stdin so
#   the key never touches the filesystem:
#
#   op read "op://<vault>/meho-review-app/private-key" | \
#     mint-review-app-token.sh --client-id "$CLIENT_ID" --key-file -

set -euo pipefail

API="https://api.github.com"
REPO="evoila/meho"
CLIENT_ID="${MEHO_REVIEW_APP_CLIENT_ID:-}"
KEY_FILE="${MEHO_REVIEW_APP_KEY_FILE:-}"

err() { printf 'mint-review-app-token: %s\n' "$*" >&2; }

usage() {
  printf 'usage: mint-review-app-token.sh --client-id <id> --key-file <pem-path|-> [--repo owner/name]\n'
}

usage_exit() {
  err "$1"
  usage >&2
  exit 2
}

while [ $# -gt 0 ]; do
  case "$1" in
    --client-id) [ $# -ge 2 ] || usage_exit "missing value for --client-id"; CLIENT_ID="$2"; shift 2 ;;
    --key-file)  [ $# -ge 2 ] || usage_exit "missing value for --key-file";  KEY_FILE="$2";  shift 2 ;;
    --repo)      [ $# -ge 2 ] || usage_exit "missing value for --repo";      REPO="$2";      shift 2 ;;
    -h|--help)   usage; exit 0 ;;
    *)           usage_exit "unknown argument: $1" ;;
  esac
done

for dep in openssl curl jq; do
  command -v "$dep" >/dev/null 2>&1 || { err "missing dependency: $dep"; exit 3; }
done

[ -n "$CLIENT_ID" ] || usage_exit "no client id (--client-id or MEHO_REVIEW_APP_CLIENT_ID)"
[ -n "$KEY_FILE" ]  || usage_exit "no key file (--key-file or MEHO_REVIEW_APP_KEY_FILE)"

# Materialize the key for openssl. stdin mode buffers into a 0600
# temp file that is removed on every exit path.
TMP_KEY=""
cleanup() { [ -n "$TMP_KEY" ] && rm -f "$TMP_KEY"; }
trap cleanup EXIT

if [ "$KEY_FILE" = "-" ]; then
  TMP_KEY="$(mktemp)"
  chmod 600 "$TMP_KEY"
  cat > "$TMP_KEY"
  KEY_FILE="$TMP_KEY"
fi

[ -s "$KEY_FILE" ] || { err "key file is missing or empty: $KEY_FILE"; exit 4; }

if ! openssl pkey -in "$KEY_FILE" -noout >/dev/null 2>&1; then
  err "key file is not a readable private key (PEM expected)"
  exit 4
fi

b64url() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }

now="$(date +%s)"
iat=$((now - 60))
exp=$((now + 540))

header="$(printf '{"typ":"JWT","alg":"RS256"}' | b64url)"
payload="$(printf '{"iat":%d,"exp":%d,"iss":"%s"}' "$iat" "$exp" "$CLIENT_ID" | b64url)"
signature="$(printf '%s.%s' "$header" "$payload" | \
  openssl dgst -sha256 -sign "$KEY_FILE" -binary | b64url)"
jwt="${header}.${payload}.${signature}"

gh_api() {
  # gh_api <method> <path> -> response body + '\n' + HTTP status on
  # stdout (curl -w appends the status as the last line; callers split
  # it back off — a function cannot export a variable across the
  # command-substitution subshell boundary).
  local method="$1" path="$2"
  # The Authorization header is fed through a process-substitution FD,
  # not argv — the JWT is a live credential and command lines are
  # world-readable via ps (op-cli rule 1: never put a secret value on
  # a command line). curl reads header lines from @file since 7.55.
  curl -sS -X "$method" \
    -H "Accept: application/vnd.github+json" \
    -H @<(printf 'Authorization: Bearer %s\n' "$jwt") \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    -w '\n%{http_code}' \
    "${API}${path}"
}

response="$(gh_api GET "/repos/${REPO}/installation")" || {
  err "network failure discovering the installation on ${REPO}"
  exit 5
}
status="${response##*$'\n'}"
installation="${response%$'\n'*}"
case "$status" in
  200) ;;
  401) err "GitHub rejected the App JWT (401) — wrong client id, wrong/rotated key, or clock skew"; exit 6 ;;
  404) err "no App installation found on ${REPO} (404) — the App is not installed there, or the client id is unknown; see the provisioning runbook in docs/codebase/automation-review-identity.md"; exit 7 ;;
  *)   err "unexpected HTTP ${status} discovering the installation on ${REPO}"; exit 8 ;;
esac

installation_id="$(printf '%s' "$installation" | jq -r '.id // empty')"
[ -n "$installation_id" ] || { err "installation response carried no id"; exit 8; }

response="$(gh_api POST "/app/installations/${installation_id}/access_tokens")" || {
  err "network failure minting the token on installation ${installation_id}"
  exit 5
}
status="${response##*$'\n'}"
token_response="${response%$'\n'*}"
[ "$status" = "201" ] || {
  err "token mint failed with HTTP ${status} on installation ${installation_id}"
  exit 9
}

token="$(printf '%s' "$token_response" | jq -r '.token // empty')"
[ -n "$token" ] || { err "token response carried no token field"; exit 9; }

expires_at="$(printf '%s' "$token_response" | jq -r '.expires_at // "unknown"')"
err "minted installation token for ${REPO} (installation ${installation_id}), expires ${expires_at}"
printf '%s\n' "$token"
