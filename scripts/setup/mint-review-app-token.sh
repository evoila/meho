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
#   mint-review-app-token.sh --credential-source governed-vault \
#     --vault-target <target> --vault-path <path> [--repo owner/name]
#
#   Environment fallbacks: MEHO_REVIEW_APP_CLIENT_ID,
#   MEHO_REVIEW_APP_KEY_FILE. --key-file - reads the PEM from stdin so
#   the key never touches the filesystem:
#
# Credential-source modes resolve both credentials without exposing the PEM in
# argv, a terminal, or a regular file. `governed-vault` uses the audited MEHO
# operator path; `1password` remains an explicit fallback. The original
# --client-id / --key-file mode stays available for callers that already own
# their source plumbing.

set -euo pipefail

API="https://api.github.com"
REPO="evoila/meho"
CLIENT_ID="${MEHO_REVIEW_APP_CLIENT_ID:-}"
KEY_FILE="${MEHO_REVIEW_APP_KEY_FILE:-}"
CREDENTIAL_SOURCE="${MEHO_REVIEW_APP_CREDENTIAL_SOURCE:-manual}"
VAULT_TARGET="${MEHO_REVIEW_APP_VAULT_TARGET:-}"
VAULT_PATH="${MEHO_REVIEW_APP_VAULT_PATH:-}"
VAULT_CLIENT_ID_FIELD="${MEHO_REVIEW_APP_VAULT_CLIENT_ID_FIELD:-client-id}"
VAULT_PRIVATE_KEY_FIELD="${MEHO_REVIEW_APP_VAULT_PRIVATE_KEY_FIELD:-private-key}"
OP_VAULT="${MEHO_REVIEW_APP_OP_VAULT:-}"
OP_ITEM="${MEHO_REVIEW_APP_OP_ITEM:-meho-review-app}"
OP_CLIENT_ID_FIELD="${MEHO_REVIEW_APP_OP_CLIENT_ID_FIELD:-client-id}"
OP_PRIVATE_KEY_FIELD="${MEHO_REVIEW_APP_OP_PRIVATE_KEY_FIELD:-private-key}"

err() { printf 'mint-review-app-token: %s\n' "$*" >&2; }

usage() {
  printf '%s\n' \
    'usage: mint-review-app-token.sh --client-id <id> --key-file <pem-path|-> [--repo owner/name]' \
    '       mint-review-app-token.sh --credential-source governed-vault --vault-target <target> --vault-path <path> [--repo owner/name]' \
    '       mint-review-app-token.sh --credential-source 1password --op-vault <vault> [--op-item <item>] [--repo owner/name]'
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
    --credential-source) [ $# -ge 2 ] || usage_exit "missing value for --credential-source"; CREDENTIAL_SOURCE="$2"; shift 2 ;;
    --vault-target) [ $# -ge 2 ] || usage_exit "missing value for --vault-target"; VAULT_TARGET="$2"; shift 2 ;;
    --vault-path) [ $# -ge 2 ] || usage_exit "missing value for --vault-path"; VAULT_PATH="$2"; shift 2 ;;
    --vault-client-id-field) [ $# -ge 2 ] || usage_exit "missing value for --vault-client-id-field"; VAULT_CLIENT_ID_FIELD="$2"; shift 2 ;;
    --vault-private-key-field) [ $# -ge 2 ] || usage_exit "missing value for --vault-private-key-field"; VAULT_PRIVATE_KEY_FIELD="$2"; shift 2 ;;
    --op-vault) [ $# -ge 2 ] || usage_exit "missing value for --op-vault"; OP_VAULT="$2"; shift 2 ;;
    --op-item) [ $# -ge 2 ] || usage_exit "missing value for --op-item"; OP_ITEM="$2"; shift 2 ;;
    --op-client-id-field) [ $# -ge 2 ] || usage_exit "missing value for --op-client-id-field"; OP_CLIENT_ID_FIELD="$2"; shift 2 ;;
    --op-private-key-field) [ $# -ge 2 ] || usage_exit "missing value for --op-private-key-field"; OP_PRIVATE_KEY_FIELD="$2"; shift 2 ;;
    --repo)      [ $# -ge 2 ] || usage_exit "missing value for --repo";      REPO="$2";      shift 2 ;;
    -h|--help)   usage; exit 0 ;;
    *)           usage_exit "unknown argument: $1" ;;
  esac
done

for dep in openssl curl jq; do
  command -v "$dep" >/dev/null 2>&1 || { err "missing dependency: $dep"; exit 3; }
done

read_governed_field() {
  meho secret read --target "$VAULT_TARGET" secret "$VAULT_PATH" --field "$1"
}

read_1password_field() {
  op read --no-newline "op://${OP_VAULT}/${OP_ITEM}/$1"
}

read_key() {
  case "$CREDENTIAL_SOURCE" in
    manual)
      if [ "$KEY_FILE" = "-" ]; then
        cat <&3
      else
        [ -s "$KEY_FILE" ] || { err "key file is missing or empty: $KEY_FILE"; return 1; }
        cat -- "$KEY_FILE"
      fi
      ;;
    governed-vault) read_governed_field "$VAULT_PRIVATE_KEY_FIELD" ;;
    1password) read_1password_field "$OP_PRIVATE_KEY_FIELD" ;;
  esac
}

case "$CREDENTIAL_SOURCE" in
  manual)
    [ -n "$CLIENT_ID" ] || usage_exit "no client id (--client-id or MEHO_REVIEW_APP_CLIENT_ID)"
    [ -n "$KEY_FILE" ] || usage_exit "no key file (--key-file or MEHO_REVIEW_APP_KEY_FILE)"
    # The signing pipeline gets its own stdin for JWT bytes. Preserve the
    # caller's streamed PEM on a separate inherited descriptor first.
    [ "$KEY_FILE" != "-" ] || exec 3<&0
    ;;
  governed-vault)
    command -v meho >/dev/null 2>&1 || { err "missing dependency: meho (required for governed-vault)"; exit 3; }
    [ -n "$VAULT_TARGET" ] || usage_exit "no Vault target (--vault-target or MEHO_REVIEW_APP_VAULT_TARGET)"
    [ -n "$VAULT_PATH" ] || usage_exit "no Vault path (--vault-path or MEHO_REVIEW_APP_VAULT_PATH)"
    CLIENT_ID="$(read_governed_field "$VAULT_CLIENT_ID_FIELD")" || { err "governed Vault read failed for client id"; exit 4; }
    [ -n "$CLIENT_ID" ] || { err "governed Vault client-id field is empty"; exit 4; }
    ;;
  1password)
    command -v op >/dev/null 2>&1 || { err "missing dependency: op (required for 1password)"; exit 3; }
    [ -n "$OP_VAULT" ] || usage_exit "no 1Password vault (--op-vault or MEHO_REVIEW_APP_OP_VAULT)"
    CLIENT_ID="$(read_1password_field "$OP_CLIENT_ID_FIELD")" || { err "1Password read failed for client id"; exit 4; }
    [ -n "$CLIENT_ID" ] || { err "1Password client-id field is empty"; exit 4; }
    ;;
  *) usage_exit "unsupported credential source: $CREDENTIAL_SOURCE (choose manual, governed-vault, or 1password)" ;;
esac

b64url() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }

now="$(date +%s)"
iat=$((now - 60))
exp=$((now + 540))

header="$(printf '{"typ":"JWT","alg":"RS256"}' | b64url)"
payload="$(printf '{"iat":%d,"exp":%d,"iss":"%s"}' "$iat" "$exp" "$CLIENT_ID" | b64url)"
# Feed a streamed PEM through file descriptor 3 while OpenSSL reads the JWT
# signing bytes from stdin. The outer pipeline retains `read_key`'s exit
# status (because pipefail is set), so a source denial after partial output
# cannot mint a token. OpenSSL consumes the key exactly once; do not
# pre-validate with `openssl pkey`, which would consume a stream twice.
sign_jwt() {
  if [ "$CREDENTIAL_SOURCE" = "manual" ] && [ "$KEY_FILE" != "-" ]; then
    printf '%s.%s' "$header" "$payload" | openssl dgst -sha256 -sign "$KEY_FILE" -binary
  else
    read_key | {
      exec 3<&0
      printf '%s.%s' "$header" "$payload" | \
        openssl dgst -sha256 -sign /dev/fd/3 -binary
    }
  fi
}

if ! signature="$(sign_jwt | b64url)"; then
  [ "$CREDENTIAL_SOURCE" != "manual" ] || [ "$KEY_FILE" != "-" ] || exec 3<&-
  err "private key is not a readable private key (PEM expected), or credential read failed"
  exit 4
fi
[ "$CREDENTIAL_SOURCE" != "manual" ] || [ "$KEY_FILE" != "-" ] || exec 3<&-
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
