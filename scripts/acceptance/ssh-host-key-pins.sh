#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# ssh-host-key-pins.sh — check that every SSH target has a host-key pin
# before an upgrade to v0.34.0 or later.
#
# Since v0.34.0 the SSH connectors refuse a target whose secret has no
# `known_hosts` entry and no `known_hosts_insecure` opt-out. This check
# runs against the release you run today, before the upgrade, and lists
# the SSH targets that would stop working after it.
#
# What it does:
#
#   1. Lists the targets of your tenant (GET /api/v1/targets, all pages).
#   2. Keeps the targets whose product uses an SSH connector.
#   3. Reads each target's secret from Vault (`vault kv get`), applies
#      the target's `secret_field_map` the way the SSH connector does, and
#      checks:
#        [ok]   the secret has a `known_hosts` entry
#        [WARN] the secret sets `known_hosts_insecure` (no host-key check)
#        [WARN] the target has no secret_ref, or its secret_ref names
#               another credential store (check it by hand)
#        [FAIL] the secret has neither, or Vault could not read it
#
# Exit codes:
#
#   0  no [FAIL] line: the upgrade does not break SSH on any target
#   1  at least one [FAIL] line: fix those targets before the upgrade
#   2  usage error (missing flag or token, curl/jq/vault not on PATH)
#
# The check only reads. It never prints a secret value or the bearer
# token. It reads Vault with your own Vault login (VAULT_ADDR and
# VAULT_TOKEN, or the token of `vault login`).

set -Eeuo pipefail

usage() {
    cat <<'HELP'
ssh-host-key-pins.sh — check that every SSH target has a host-key pin
before an upgrade to v0.34.0 or later.

Usage:
  MEHO_ACCESS_TOKEN=... bash scripts/acceptance/ssh-host-key-pins.sh \
    --backplane https://meho.example.com

Flags:
  --backplane <url>   Backplane base URL. Required.
  --mount <name>      Vault KV-v2 mount of the target secrets
                      (default: secret, the MEHO default).
  --product <name>    Also check targets of this product. Use it for an
                      SSH connector from a connector package. Repeat it
                      for more products.
  --cacert <path>     CA bundle for the backplane TLS check.
  -h | --help         Print this help and exit 0.

Environment:
  MEHO_ACCESS_TOKEN   Bearer token of an operator of the tenant. Required.
  VAULT_ADDR, VAULT_TOKEN
                      Read by the vault CLI. The Vault login must be
                      able to read the target secrets.

Exit codes: 0 no target fails, 1 at least one target fails, 2 usage error.
HELP
}

# The products of the built-in connectors that use SSH. A backend test
# (test_ssh_host_key_pins_script.py) keeps this list equal to the
# SshConnector subclasses.
SSH_PRODUCTS=(bind9 hyperv linux msad pfsense rke2 windns winsrv wsfc)

BACKPLANE=""
MOUNT="secret"
CACERT=""
FAILED=0

ok() { printf '[ok]   %s\n' "$*"; }
warn() { printf '[WARN] %s\n' "$*"; }
fail() {
    printf '[FAIL] %s\n' "$*"
    FAILED=1
}

die_usage() {
    printf 'ssh-host-key-pins.sh: %s\n' "$*" >&2
    exit 2
}

parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --backplane) [ $# -ge 2 ] || die_usage "--backplane needs a value"; BACKPLANE="${2%/}"; shift 2 ;;
            --mount) [ $# -ge 2 ] || die_usage "--mount needs a value"; MOUNT="$2"; shift 2 ;;
            --product) [ $# -ge 2 ] || die_usage "--product needs a value"; SSH_PRODUCTS+=("$2"); shift 2 ;;
            --cacert) [ $# -ge 2 ] || die_usage "--cacert needs a value"; CACERT="$2"; shift 2 ;;
            -h | --help) usage; exit 0 ;;
            *) die_usage "unknown flag: $1 (see --help)" ;;
        esac
    done
    [ -n "$BACKPLANE" ] || die_usage "--backplane is required"
    [ -n "${MEHO_ACCESS_TOKEN:-}" ] || die_usage "MEHO_ACCESS_TOKEN is not set"
    local tool
    for tool in curl jq vault; do
        command -v "$tool" >/dev/null 2>&1 || die_usage "$tool is not on PATH"
    done
}

# One page of GET /api/v1/targets. The token goes to curl on stdin, so
# it is never in the process list.
targets_page() {
    local cursor="$1"
    local -a args=(--silent --show-error --fail --retry 3 --get
        --data-urlencode "limit=500" --header @-)
    [ -n "$cursor" ] && args+=(--data-urlencode "cursor=$cursor")
    [ -n "$CACERT" ] && args+=(--cacert "$CACERT")
    printf 'Authorization: Bearer %s\n' "$MEHO_ACCESS_TOKEN" |
        curl "${args[@]}" "$BACKPLANE/api/v1/targets"
}

# Print one line per SSH target:
# name<TAB>product<TAB>secret_field_map (JSON, base64)<TAB>secret_ref.
# The secret_ref comes last: it may be empty, and `read` joins empty
# fields between tabs.
list_ssh_targets() {
    local products cursor="" page
    products="$(printf '%s\n' "${SSH_PRODUCTS[@]}" | jq -R . | jq -sc .)"
    while :; do
        page="$(targets_page "$cursor")" || {
            printf 'ssh-host-key-pins.sh: cannot list the targets at %s\n' "$BACKPLANE" >&2
            exit 1
        }
        jq -r --argjson p "$products" \
            '.items[] | select(.product as $x | $p | index($x)) |
             [.name, .product, (.secret_field_map // {} | tojson | @base64),
              (.secret_ref // "")] | @tsv' <<<"$page"
        cursor="$(jq -r '.next_cursor // ""' <<<"$page")"
        [ -n "$cursor" ] || break
    done
}

# Decide one target. The rules match the SSH connector. First the
# target's `secret_field_map` ({connector_field: source_field}) is applied:
# a mapped field takes the value of its source field, and is gone when the
# source field is not in the secret. Then a non-empty `known_hosts` wins;
# else a truthy `known_hosts_insecure` (true, or the strings
# 1/true/yes/on) is the opt-out; else the target fails.
check_target() {
    local name="$1" product="$2" ref="$3" field_map="$4" path secret verdict
    local label="$name ($product)"
    if [ -z "$ref" ]; then
        warn "$label: no secret_ref, so SSH does not work with or without a pin"
        return
    fi
    path="$ref"
    if [[ "$ref" =~ ^([a-zA-Z][a-zA-Z0-9+.-]*):(.+)$ ]]; then
        if [ "${BASH_REMATCH[1]}" != "vault" ]; then
            warn "$label: the secret is in the '${BASH_REMATCH[1]}' store; check it by hand"
            return
        fi
        path="${BASH_REMATCH[2]}"
    fi
    if ! secret="$(vault kv get -format=json -mount="$MOUNT" "$path" 2>/dev/null)"; then
        fail "$label: cannot read the secret from Vault; check it by hand"
        return
    fi
    verdict="$(jq -r --arg map "$field_map" '
        (.data.data // {}) as $raw
        | (if $map == "" then {} else ($map | @base64d | fromjson) end) as $m
        | (reduce ($m | to_entries[]) as $e ($raw;
            if $raw | has($e.value) then .[$e.key] = $raw[$e.value]
            else del(.[$e.key]) end)) as $s
        | ([$m | to_entries[]
            | select(.key == "known_hosts" or .key == "known_hosts_insecure")
            | select(.value as $v | $raw | has($v) | not) | .value] | unique | join(", ")) as $gone
        | if (($s.known_hosts // "") | tostring | gsub("^\\s+|\\s+$"; "")) != "" then "pinned"
          elif ($s.known_hosts_insecure == true)
            or (($s.known_hosts_insecure | type) == "string"
                and (($s.known_hosts_insecure | ascii_downcase | gsub("^\\s+|\\s+$"; ""))
                     | IN("1", "true", "yes", "on")))
          then "insecure"
          elif $gone != "" then "unmapped:" + $gone
          else "missing" end' <<<"$secret")" || verdict="missing"
    case "$verdict" in
        pinned) ok "$label: host key pinned" ;;
        insecure) warn "$label: known_hosts_insecure is set, so MEHO does not check the host key" ;;
        unmapped:*) fail "$label: the target's secret_field_map points to a field the secret does not have (${verdict#unmapped:}); SSH stops working after the upgrade" ;;
        *) fail "$label: no known_hosts entry and no known_hosts_insecure; SSH stops working after the upgrade" ;;
    esac
}

main() {
    parse_args "$@"
    local targets name product ref field_map count=0
    targets="$(list_ssh_targets)"
    while IFS=$'\t' read -r name product field_map ref; do
        [ -n "$name" ] || continue
        count=$((count + 1))
        check_target "$name" "$product" "$ref" "$field_map"
    done <<<"$targets"
    printf 'Checked %d SSH target(s).\n' "$count"
    if [ "$FAILED" -ne 0 ]; then
        printf 'Fix the [FAIL] targets before the upgrade: add a known_hosts entry to the secret, or set known_hosts_insecure: true.\n'
        exit 1
    fi
}

main "$@"
