#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# custom-policy-check.sh: run the checks of the custom policy sandbox test.
#
# One run checks that, with the switch CUSTOM_POLICY on, the custom policy
# model and every fixed rule work on a real install, through MEHO's own
# paths: the web console, the `meho` command line, MCP, and REST with a
# login's token. For each check the script prints [ok], [FAIL] or [skip],
# the caller, the path, and the audit row ids.
#
# The script runs only against an install in sandbox mode
# (docs/decisions/sandbox-mode.md). Every step goes to the one install it
# checked at its start. It reads every token, target and operation from
# the environment. It never approves a request: a person does that in the
# web console when the script asks.
#
# The expected rule ids sit next to each check. Each one names its row in
# the rule table of docs/codebase/policy-evaluator.md.
#
# How to run it, and what each check proves: docs/acceptance/custom-policy-check.md.

# Tracing would print every token. Stop before any line reads one.
case "$-" in
    *x*)
        echo "custom-policy-check.sh: refused: tracing (bash -x) is on, and it would print the tokens. Run the script without -x." >&2
        exit 2 ;;
esac

set -Eeuo pipefail

usage() {
    cat <<'HELP'
custom-policy-check.sh: run the checks of the custom policy sandbox test.

Usage:
  bash scripts/acceptance/custom-policy-check.sh [--backplane URL]
  bash scripts/acceptance/custom-policy-check.sh --record-baseline FILE [--backplane URL]
  bash scripts/acceptance/custom-policy-check.sh --dry-run DIR

  --backplane URL         The install. Default: $MEHO_BACKPLANE.
  --record-baseline FILE  With the switch CUSTOM_POLICY off, save the answers
                          that check 7 compares with, then stop. Run this
                          once before the switch is turned on.
  --dry-run DIR           Read every answer from the recorded answers in
                          DIR (one file <step>.json each). For the script's
                          own test.
  -h, --help              Print this text.

Before any call, it stops with exit code 2 when:
  - tracing (bash -x) is on;
  - a setting is missing, or a CPC_PARAMS_* value is not a JSON object;
  - the install that the meho command line stored at its last `meho login`
    is not the install of --backplane;
  - /ready does not answer HTTP 200 with "status": "ready" and
    "sandbox_mode": "on" (a 503 answer stops it too);
  - the switch CUSTOM_POLICY (features.policy.custom_policy of /ready) is
    not on, or cannot be read. --record-baseline needs it off.
It prints the install's address and version (GET /version).

What it does:
  It runs the checks 1 to 6 and 8 to 11 with the switch on. Then it
  withdraws every request it made that still waits, asks the person to
  remove what they added for the checks, and reads back that it is gone.
  Last, a person turns the switch off, and check 7 compares the answers
  with the baseline. The install ends with the switch off.

  The script never approves or decides a request. Its one approve call is
  the agent's attempt in check 6, which MEHO must refuse.

  A step that needs a person in the web console prints what to do and asks
  "Is it done?". With no terminal, or when the person answers no, it prints
  "[skip] check N (...): needs a person", and every later step that needs
  that person step prints "not run: waits for the person step N". It never
  runs a step that a person step guards, for example the delete of check 6.
  A [skip] line never counts as passed.

Exit codes:
  0  every check passed
  1  at least one check failed
  2  wrong use, a missing setting, or an install the script may not use
  3  not complete: no check failed, but at least one step did not run
     (a [skip] line), or something the run made could not be taken back

Checks:
   1  a lowered level: a person in the console runs the operation at once
   2  a re-levelled operation: an Operator cannot approve it, an Admin can
   3  a carried-over "always wait": needs a preview, then waits
   4  an agent policy: a personal agent runs, a service login waits
   5  an approver policy: only the custom role approves
   6  each fixed rule waits; an agent's change of the policy is refused;
      an agent's approve attempt is refused
   8  a write is never lowered below caution (console and token)
   9  every policy change in the console needs a fresh sign-in
  10  a person approves their own agent's request in the console; the
      command line approve is a person step ([skip], needs a person)
  11  each login of MEHO's own code writes its caller kind
   7  the switch off again: every answer is as before

Check 6, a change of the policy: an agent login asks for policy.apply over
MCP, and MEHO refuses it before anything waits ("Only a person can ask for
a policy import"). The same call with the Operator's token waits.

Environment (live run). Each CPC_OP_* value is "<connector_id> <op_id> [<target>]";
without a target the call has no target. A CPC_PARAMS_<name> value (for
example CPC_PARAMS_DELETE) is the JSON object of the call's params; the
default is {}.

  MEHO_BACKPLANE          The install's address.
  CPC_ADMIN_TOKEN         A tenant Admin's token. Reads audit rows, requests and overrides.
  CPC_PERSON_TOKEN        An Operator's own token (a personal agent over REST and MCP).
                          The meho command line must be signed in as the same Operator.
  CPC_SERVICE_TOKEN       A service login with no standing grant.
  CPC_DELETE_GRANT_TOKEN  A service login with a delete grant for CPC_OP_DELETE.
  CPC_AGENT_TOKEN         A login of kind agent.
  CPC_METRICS_TOKEN       Optional: the install's METRICS_AUTH_TOKEN, when it is set,
                          so /ready shows the switch.
  CPC_BASELINE            The file of --record-baseline.
  CPC_CUSTOM_ROLE         check 5: the name of the custom role the person makes.
  CPC_OP_LOWERED          check 1: a dangerous operation an Admin lowers to caution.
  CPC_OP_RELEVELLED       check 2: a former caution operation with the plain mark.
  CPC_OP_ALWAYS_WAIT      check 3: an operation with a saved "always wait".
  CPC_OP_AGENT_RULE       check 4: a caution operation for the personal agent rule.
  CPC_OP_APPROVER         check 5: a dangerous operation on the approver policy's connector.
  CPC_OP_CREDENTIAL_READ  check 6: an operation that reads a credential.
  CPC_OP_CREDENTIAL_WRITE check 6: an operation that writes a credential.
  CPC_OP_RIGHTS_CHANGE    check 6: an operation that changes rights.
  CPC_OP_POLICY_CHANGE    check 6: an operation that changes the policy (policy.apply).
  CPC_OP_DESTRUCTIVE      check 6: a destructive operation.
  CPC_OP_DELETE           check 6: a delete of a disposable system.
  CPC_OP_HARMLESS         check 6: an operation that is harmless if it ran and that
                          waits for a token call; the agent tries to approve its request.
  CPC_OP_CALLER_ADDRESS   check 6: an operation that calls an address the caller gives.
  CPC_OP_WRITE            check 8 and 9: a caution write.
  CPC_OP_OWN_AGENT        check 10: an operation that waits for the person's own agent.
  CPC_SENSOR_ID           check 11: a sensor whose check the runner runs.
HELP
}

BACKPLANE="${MEHO_BACKPLANE:-}"
DRY_DIR=""
BASELINE_OUT=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --backplane)
            [[ $# -ge 2 ]] || { echo "custom-policy-check.sh: --backplane needs a value" >&2; exit 2; }
            BACKPLANE="$2"; shift 2 ;;
        --record-baseline)
            [[ $# -ge 2 ]] || { echo "custom-policy-check.sh: --record-baseline needs a file" >&2; exit 2; }
            BASELINE_OUT="$2"; shift 2 ;;
        --dry-run)
            [[ $# -ge 2 && -d "$2" ]] || { echo "custom-policy-check.sh: --dry-run needs a folder" >&2; exit 2; }
            DRY_DIR="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "custom-policy-check.sh: unknown argument: $1" >&2; exit 2 ;;
    esac
done
BACKPLANE="${BACKPLANE%/}"

stop() { echo "custom-policy-check.sh: $1" >&2; exit 2; }

command -v jq >/dev/null || stop "jq is needed"

OP_VARS=(CPC_OP_LOWERED CPC_OP_RELEVELLED CPC_OP_ALWAYS_WAIT CPC_OP_AGENT_RULE
    CPC_OP_APPROVER CPC_OP_CREDENTIAL_READ CPC_OP_CREDENTIAL_WRITE CPC_OP_RIGHTS_CHANGE
    CPC_OP_POLICY_CHANGE CPC_OP_DESTRUCTIVE CPC_OP_DELETE CPC_OP_HARMLESS
    CPC_OP_CALLER_ADDRESS CPC_OP_WRITE CPC_OP_OWN_AGENT)

# The install that `meho login` stored (the CLI's config.json).
stored_install() {
    local file
    if [[ -n "${XDG_CONFIG_HOME:-}" ]]; then
        file="$XDG_CONFIG_HOME/meho/config.json"
    else
        file="${HOME:-}/.config/meho/config.json"
    fi
    [[ -f "$file" ]] || return 0
    jq -r '.backplane_url // ""' "$file" 2>/dev/null | sed 's:/*$::'
}

if [[ -z "$DRY_DIR" ]]; then
    command -v curl >/dev/null || stop "curl is needed"
    [[ -n "$BACKPLANE" ]] || stop "set MEHO_BACKPLANE or --backplane"
    missing=()
    for name in CPC_ADMIN_TOKEN CPC_PERSON_TOKEN CPC_SERVICE_TOKEN CPC_DELETE_GRANT_TOKEN \
        CPC_AGENT_TOKEN CPC_CUSTOM_ROLE CPC_SENSOR_ID "${OP_VARS[@]}"; do
        [[ -n "${!name:-}" ]] || missing+=("$name")
    done
    if [[ -z "$BASELINE_OUT" && -z "${CPC_BASELINE:-}" ]]; then
        missing+=("CPC_BASELINE")
    fi
    [[ ${#missing[@]} -eq 0 ]] || stop "these are not set: ${missing[*]}"
    for name in "${OP_VARS[@]}"; do
        params_name="CPC_PARAMS_${name#CPC_OP_}"
        if [[ -n "${!params_name:-}" ]] && ! printf '%s' "${!params_name}" \
            | jq -e 'type == "object"' >/dev/null 2>&1; then
            stop "$params_name is not a JSON object"
        fi
    done
    if [[ -z "$BASELINE_OUT" ]]; then
        command -v meho >/dev/null || stop "the meho command line is needed"
        stored="$(stored_install)"
        [[ "$stored" == "$BACKPLANE" ]] || stop "refused: the meho command line is signed in to ${stored:-no install}, not to $BACKPLANE. Run \`meho login\` against $BACKPLANE first."
    fi
fi

PASS_COUNT=0
FAIL_COUNT=0
SKIP_COUNT=0

check_ok() { PASS_COUNT=$((PASS_COUNT + 1)); printf '[ok]   %s\n' "$1"; }
check_fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); printf '[FAIL] %s\n' "$1"; }
check_skip() { SKIP_COUNT=$((SKIP_COUNT + 1)); printf '[skip] %s\n' "$1"; }
note() { printf '[note] %s\n' "$1"; }
needs_person() { check_skip "$1: needs a person"; }
waits_for() { check_skip "$1: not run: waits for the person step $2"; }

WORK="$(mktemp -d)"
REQUESTS=()

# At every end of the run, also on an error or a stop signal: withdraw the
# requests of this run that still wait, and remove the private files.
# shellcheck disable=SC2317,SC2329  # called by the trap
on_exit() {
    local rc=$?
    trap - EXIT INT TERM
    if [[ ${#REQUESTS[@]} -gt 0 ]]; then
        withdraw_requests || true
    fi
    rm -rf "$WORK"
    exit "$rc"
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# --- Answers: from the install, or recorded -------------------------------
#
# Every request has a step name. In a dry run, the answer of step <name> is
# the file <name>.json in the dry-run folder: {"code": <HTTP code or exit
# code>, "body": <JSON>}. A missing file is the answer code 0 with body null.
# A live run puts the step name in CPC_STEP for the programs it starts.

REPLY_CODE=""
REPLY_BODY=""
REST_HEADER=""

recorded() {
    local file="$DRY_DIR/$1.json"
    if [[ -f "$file" ]]; then
        REPLY_CODE="$(jq -r '.code // 0' "$file")"
        REPLY_BODY="$(jq -c '.body' "$file")"
    else
        REPLY_CODE=0
        REPLY_BODY=null
    fi
}

# The token goes to curl on a file descriptor, never on a command line.
auth_header() { printf 'Authorization: Bearer %s\n' "${!1}"; }

read_out() { REPLY_BODY="$(jq -c . "$1" 2>/dev/null || true)"; REPLY_BODY="${REPLY_BODY:-null}"; }

# rest STEP METHOD PATH TOKEN_NAME [BODY] [CONTENT_TYPE]
# The body goes to curl in a private file, never on a command line.
rest() {
    local step="$1" method="$2" path="$3" token_name="$4" body="${5:-}" ctype="${6:-application/json}"
    export CPC_STEP="$step"
    if [[ -n "$DRY_DIR" ]]; then recorded "$step"; return 0; fi
    local out="$WORK/out" data="$WORK/data" args
    rm -f "$out"
    args=(-sS -o "$out" -w '%{http_code}' -X "$method" -H "Content-Type: $ctype")
    [[ -n "$REST_HEADER" ]] && args+=(-H "$REST_HEADER")
    if [[ -n "$body" ]]; then
        printf '%s' "$body" >"$data"
        args+=(--data-binary "@$data")
    fi
    REPLY_CODE="$(curl "${args[@]}" -H @<(auth_header "$token_name") "$BACKPLANE$path" 2>/dev/null)" \
        || REPLY_CODE=000
    read_out "$out"
}

# get_open STEP PATH [TOKEN_NAME]: a GET of a route that needs no login.
get_open() {
    local step="$1" path="$2" token_name="${3:-}"
    export CPC_STEP="$step"
    if [[ -n "$DRY_DIR" ]]; then recorded "$step"; return 0; fi
    local out="$WORK/out"
    rm -f "$out"
    if [[ -n "$token_name" && -n "${!token_name:-}" ]]; then
        REPLY_CODE="$(curl -sS -o "$out" -w '%{http_code}' -H @<(auth_header "$token_name") \
            "$BACKPLANE$path" 2>/dev/null)" || REPLY_CODE=000
    else
        REPLY_CODE="$(curl -sS -o "$out" -w '%{http_code}' "$BACKPLANE$path" 2>/dev/null)" \
            || REPLY_CODE=000
    fi
    read_out "$out"
}

MCP_INIT='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"custom-policy-check","version":"1"}}}'

# mcp STEP TOKEN_NAME TOOL ARGUMENTS_JSON: one tools/call; the reply body is the tool result.
mcp() {
    local step="$1" token_name="$2" tool="$3" arguments="$4"
    export CPC_STEP="$step"
    if [[ -n "$DRY_DIR" ]]; then recorded "$step"; return 0; fi
    local headers="$WORK/headers" out="$WORK/out" data="$WORK/data" session
    rm -f "$headers" "$out"
    printf '%s' "$MCP_INIT" >"$data"
    curl -sS -o /dev/null -D "$headers" -X POST -H 'Content-Type: application/json' \
        -H @<(auth_header "$token_name") --data-binary "@$data" "$BACKPLANE/mcp" 2>/dev/null || true
    session="$(awk 'tolower($1) == "mcp-session-id:" {print $2}' "$headers" 2>/dev/null | tr -d '\r')"
    printf '%s' "$arguments" | jq -c --arg t "$tool" \
        '{jsonrpc:"2.0",id:2,method:"tools/call",params:{name:$t,arguments:.}}' >"$data"
    REPLY_CODE="$(curl -sS -o "$out" -w '%{http_code}' -X POST \
        -H 'Content-Type: application/json' -H 'MCP-Protocol-Version: 2025-06-18' \
        ${session:+-H "Mcp-Session-Id: $session"} \
        -H @<(auth_header "$token_name") \
        --data-binary "@$data" "$BACKPLANE/mcp" 2>/dev/null)" || REPLY_CODE=000
    REPLY_BODY="$(jq -c '.result.structuredContent // (.result.content[0].text | fromjson? ) // .' \
        "$out" 2>/dev/null || true)"
    REPLY_BODY="${REPLY_BODY:-null}"
}

now_iso() { # now, less 30 seconds for a server clock that is a little behind
    if [[ -n "$DRY_DIR" ]]; then echo "2026-01-01T00:00:00Z"; return; fi
    jq -nr 'now - 30 | strftime("%Y-%m-%dT%H:%M:%SZ")'
}

# --- Person steps -----------------------------------------------------------
#
# person STEP TEXT: a person does TEXT in the web console. Returns 0 only when
# the person answers yes; no terminal, no answer or "no" return 1.
person_reply() {
    local step="$1" reply=""
    if [[ -n "$DRY_DIR" ]]; then
        recorded "$step"
        reply="$(jq -r 'if type == "string" then . else "" end' <<<"$REPLY_BODY")"
    else
        { read -r reply </dev/tty; } 2>/dev/null || reply=""
    fi
    printf '%s\n' "${reply:-<no answer>}"
    PERSON_REPLY="$reply"
}

PERSON_REPLY=""
person() {
    printf '[person] %s\n' "$2"
    printf '         Is it done? [y/n] '
    person_reply "$1"
    case "$PERSON_REPLY" in y|Y|yes) return 0 ;; esac
    return 1
}

# ask STEP QUESTION: sets ANSWER to yes, no, or none (no terminal or no answer).
ANSWER=""
ask() {
    printf '[person] %s [y/n] ' "$2"
    person_reply "$1"
    case "$PERSON_REPLY" in
        y|Y|yes) ANSWER=yes ;;
        n|N|no) ANSWER=no ;;
        *) ANSWER=none ;;
    esac
}

# --- Operations ------------------------------------------------------------

op_part() { # op_part VAR N: word N (1-3) of "<connector_id> <op_id> [<target>]"
    local value="${!1:-}"
    awk -v n="$2" '{print $n}' <<<"$value"
}

op_params() { # op_params VAR: the JSON of CPC_PARAMS_<name>, default {}
    local name="CPC_PARAMS_${1#CPC_OP_}" value
    value="${!name:-}"
    [[ -n "$value" ]] || value='{}'
    printf '%s' "$value"
}

op_label() { # op_label VAR: "<op_id>" for the output, or the variable name in a dry run
    local id
    id="$(op_part "$1" 2)"
    printf '%s' "${id:-$1}"
}

# op_body OP_VAR [PREVIEW_HASH]: the call body. The params reach jq on its
# input, never on its command line. No target: no "target" key.
op_body() {
    op_params "$1" | jq -c --arg c "$(op_part "$1" 1)" --arg o "$(op_part "$1" 2)" \
        --arg t "$(op_part "$1" 3)" --arg h "${2:-}" \
        '{connector_id:$c, op_id:$o, params:.}
         + (if $t == "" then {} else {target:$t} end)
         + (if $h == "" then {} else {preview_hash:$h} end)'
}

# meho_cli STEP ARGS...: the meho command line, always at the checked install.
meho_cli() {
    local step="$1"; shift
    export CPC_STEP="$step"
    if [[ -n "$DRY_DIR" ]]; then recorded "$step"; return 0; fi
    local out="$WORK/cli"
    REPLY_CODE=0
    meho "$@" --backplane "$BACKPLANE" >"$out" 2>/dev/null || REPLY_CODE=$?
    read_out "$out"
}

# REQUESTS (set at the start): every request this run made that may still
# wait, with the login that made it ("<request id> <token name>").

# call_op STEP PATH TOKEN_NAME OP_VAR [PREVIEW_HASH]
# PATH is rest, mcp or cli. Sets CALL_STATUS, CALL_CODE, CALL_REASON, CALL_AUDIT, CALL_REQUEST.
CALL_STATUS=""; CALL_CODE=""; CALL_REASON=""; CALL_AUDIT=""; CALL_REQUEST=""
call_op() {
    local step="$1" path="$2" token_name="$3" op_var="$4" preview_hash="${5:-}"
    case "$path" in
        rest) rest "$step" POST /api/v1/operations/call "$token_name" "$(op_body "$op_var" "$preview_hash")" ;;
        mcp) mcp "$step" "$token_name" call_operation "$(op_body "$op_var" "$preview_hash")" ;;
        cli)
            local params_file="$WORK/params.json" target args
            op_params "$op_var" >"$params_file"
            target="$(op_part "$op_var" 3)"
            args=(operation call "$(op_part "$op_var" 1)" "$(op_part "$op_var" 2)"
                --params "@$params_file" --json)
            [[ -n "$target" ]] && args+=(--target "$target")
            [[ -n "$preview_hash" ]] && args+=(--preview-hash "$preview_hash")
            meho_cli "$step" "${args[@]}"
            rm -f "$params_file" ;;
    esac
    CALL_STATUS="$(jq -r '.status // "no answer"' <<<"$REPLY_BODY" 2>/dev/null || echo "no answer")"
    CALL_CODE="$(jq -r '.extras.error_code // ""' <<<"$REPLY_BODY" 2>/dev/null || true)"
    CALL_REASON="$(jq -r '.extras.reason // ""' <<<"$REPLY_BODY" 2>/dev/null || true)"
    CALL_AUDIT="$(jq -r '.audit_id // ""' <<<"$REPLY_BODY" 2>/dev/null || true)"
    CALL_REQUEST="$(jq -r '.extras.approval_request_id // ""' <<<"$REPLY_BODY" 2>/dev/null || true)"
    [[ -n "$CALL_REQUEST" ]] && REQUESTS+=("$CALL_REQUEST $token_name")
    return 0
}

# preview STEP PATH TOKEN_NAME OP_VAR: sets PREVIEW_HASH and PREVIEW_LEVEL.
# PATH rest or mcp: the same login and the same way in as the call after it.
PREVIEW_HASH=""; PREVIEW_LEVEL=""
preview() {
    local step="$1" path="$2" token_name="$3" op_var="$4"
    case "$path" in
        mcp) mcp "$step" "$token_name" preview_operation "$(op_body "$op_var")" ;;
        *) rest "$step" POST /api/v1/operations/preview "$token_name" "$(op_body "$op_var")" ;;
    esac
    PREVIEW_HASH="$(jq -r '.preview_hash // ""' <<<"$REPLY_BODY" 2>/dev/null || true)"
    PREVIEW_LEVEL="$(jq -r '.safety_level // ""' <<<"$REPLY_BODY" 2>/dev/null || true)"
}

# call_previewed STEP PATH TOKEN_NAME OP_VAR: preview, then the call with its hash.
call_previewed() {
    preview "$1-preview" "$2" "$3" "$4"
    call_op "$1" "$2" "$3" "$4" "$PREVIEW_HASH"
}

verdict_of() { # the call's answer in plain words
    case "$1" in
        ok) echo runs ;;
        awaiting_approval) echo waits ;;
        denied) echo refused ;;
        *) echo "$1" ;;
    esac
}

# --- Audit rows -------------------------------------------------------------

ROW_ID=""; ROW_RULE=""; ROW_KIND=""; ROW_HAS_KIND=""; ROW_STATUS=""

read_row_body() {
    ROW_ID="$(jq -r '.id // ""' <<<"$1")"
    ROW_RULE="$(jq -r '.policy_rule // ""' <<<"$1")"
    ROW_KIND="$(jq -r '.caller_kind // ""' <<<"$1")"
    ROW_HAS_KIND="$(jq -r 'if type == "object" and has("caller_kind") then "yes" else "" end' <<<"$1")"
    ROW_STATUS="$(jq -r '.result_status // ""' <<<"$1")"
}

clear_row() { ROW_ID=""; ROW_RULE=""; ROW_KIND=""; ROW_HAS_KIND=""; ROW_STATUS=""; }

# row_by_id STEP AUDIT_ID: read one audit row.
row_by_id() {
    clear_row
    [[ -n "$2" || -n "$DRY_DIR" ]] || return 0
    rest "$1" GET "/api/v1/audit/show/$2" CPC_ADMIN_TOKEN
    [[ "$REPLY_CODE" == 200 ]] && read_row_body "$REPLY_BODY"
    return 0
}

# audit_rows STEP FILTER_JSON: POST /api/v1/audit/query; sets ROWS (a JSON list, [] on any error).
ROWS="[]"
audit_rows() {
    rest "$1" POST /api/v1/audit/query CPC_ADMIN_TOKEN "$2"
    ROWS="[]"
    if [[ "$REPLY_CODE" == 200 ]]; then
        ROWS="$(jq -c '[.rows[]?]' <<<"$REPLY_BODY" 2>/dev/null || echo '[]')"
    fi
}

since_filter() { jq -nc --arg s "$1" '{since:$s, limit:1000}'; }

# post_rows STEP SINCE PATH_PREFIX: the ids of the POST rows since SINCE whose path starts with PATH_PREFIX.
ROW_IDS=""
post_rows() {
    audit_rows "$1" "$(since_filter "$2")"
    ROW_IDS="$(jq -r --arg p "$3" \
        '[.[] | select((.method // "") == "POST" and ((.path // "") | startswith($p))) | .id] | join(",")' \
        <<<"$ROWS")"
}

# request_status STEP REQUEST_ID: the status of a request and who sent it.
REQUEST_STATUS=""; REQUEST_SUB=""
request_status() {
    REQUEST_STATUS="unknown"; REQUEST_SUB=""
    [[ -n "$2" ]] || return 0
    rest "$1" GET "/api/v1/approvals/$2" CPC_ADMIN_TOKEN
    [[ "$REPLY_CODE" == 200 ]] || return 0
    REQUEST_STATUS="$(jq -r '.status // "unknown"' <<<"$REPLY_BODY")"
    REQUEST_SUB="$(jq -r '.principal_sub // ""' <<<"$REPLY_BODY")"
}

# rule_ok RULE WANT: WANT ending in ":" matches a source; else the rule id itself.
# A rule marked "(shadow)" did not decide, so it never matches. An empty rule never matches.
rule_ok() {
    local rule="$1" want="$2"
    [[ -n "$rule" ]] || return 1
    [[ "$rule" == *"(shadow)"* ]] && return 1
    if [[ "$want" == *: ]]; then [[ "$rule" == "$want"* ]]; else [[ "$rule" == "$want" || "$rule" == "$want "* ]]; fi
}

# expect STEP CHECK WHO PATH WANT_VERDICT WANT_RULE WANT_KIND [WANT_REASON]
# Reads the audit row of the last call_op and prints one line. A waiting
# or running call needs WANT_RULE; a refused call needs WANT_REASON.
expect() {
    local step="$1" check="$2" who="$3" path="$4" want="$5" want_rule="$6" want_kind="$7" want_reason="${8:-}"
    local got problems=()
    got="$(verdict_of "$CALL_STATUS")"
    row_by_id "$step" "$CALL_AUDIT"
    [[ "$got" == "$want" ]] || problems+=("expected $want")
    [[ -n "$ROW_ID" ]] || problems+=("no audit row")
    if [[ -n "$want_rule" ]] && ! rule_ok "$ROW_RULE" "$want_rule"; then
        problems+=("expected rule $want_rule")
    fi
    if [[ -n "$want_reason" && "$CALL_REASON" != *"$want_reason"* ]]; then
        problems+=("expected the reason \"$want_reason\"")
    fi
    if [[ -n "$want_kind" && "$ROW_KIND" != "$want_kind" ]]; then
        problems+=("expected caller kind $want_kind")
    fi
    local line
    line="check $check ($who, $path): $got${CALL_CODE:+ ($CALL_CODE)}, rule ${ROW_RULE:-none}, caller kind ${ROW_KIND:-none}, audit row ${ROW_ID:-none}"
    report "$line" ${problems[@]+"${problems[@]}"}
}

# report LINE [PROBLEM...]: [ok] without problems, else [FAIL] with them.
report() {
    local line="$1"; shift
    if [[ $# -eq 0 ]]; then check_ok "$line"; return; fi
    local problem
    for problem in "$@"; do line+="; $problem"; done
    check_fail "$line"
}

# expect_click CHECK WHO REQUEST WANT SINCE STEP: a person clicked Approve in the
# web console. WANT is refused (the request still waits, and the click row is
# not the sender's), approved, or own (approved by its own sender). The proof
# is the audit row of the click: POST /ui/approvals/<id>/approve.
expect_click() {
    local check="$1" who="$2" request="$3" want="$4" since="$5" step="$6"
    request_status "$step-request" "$request"
    audit_rows "$step-rows" "$(since_filter "$since")"
    local clicks others own
    clicks="$(jq -r --arg p "/ui/approvals/$request/approve" \
        '[.[] | select((.method // "") == "POST" and .path == $p) | .id] | join(",")' <<<"$ROWS")"
    others="$(jq -r --arg p "/ui/approvals/$request/approve" --arg s "$REQUEST_SUB" \
        '[.[] | select((.method // "") == "POST" and .path == $p and .principal_sub != $s) | .id] | join(",")' <<<"$ROWS")"
    own="$(jq -r --arg p "/ui/approvals/$request/approve" --arg s "$REQUEST_SUB" \
        '[.[] | select((.method // "") == "POST" and .path == $p and .principal_sub == $s) | .id] | join(",")' <<<"$ROWS")"
    local line="check $check ($who, web console): request ${request:-none} is $REQUEST_STATUS, click rows ${clicks:-none}"
    local problems=()
    [[ -n "$request" ]] || problems+=("no request")
    case "$want" in
        refused)
            [[ "$REQUEST_STATUS" == pending ]] || problems+=("expected a request that still waits")
            [[ -n "$others" ]] || problems+=("expected the audit row of a click by another person than the sender") ;;
        approved)
            [[ "$REQUEST_STATUS" == approved ]] || problems+=("expected approved")
            [[ -n "$clicks" ]] || problems+=("expected the audit row of the click") ;;
        own)
            [[ "$REQUEST_STATUS" == approved ]] || problems+=("expected approved")
            [[ -n "$own" ]] || problems+=("expected the audit row of the sender's own click") ;;
    esac
    report "$line" ${problems[@]+"${problems[@]}"}
}

# --- Check 7 probes: the answers that must be the same with the switch off --
#
# Each probe asks POST /api/v1/policy/explain as the caller of the check it
# repeats, by the same way in (web: the web console; token: REST, MCP or the
# command line). It runs nothing; its own request row is its audit row.
# Fields: the check it repeats, the token, the operation, the way in.

PROBES=(
    "1 CPC_PERSON_TOKEN CPC_OP_LOWERED web"
    "2 CPC_PERSON_TOKEN CPC_OP_RELEVELLED token"
    "3 CPC_PERSON_TOKEN CPC_OP_ALWAYS_WAIT token"
    "4 CPC_PERSON_TOKEN CPC_OP_AGENT_RULE token"
    "4 CPC_SERVICE_TOKEN CPC_OP_AGENT_RULE token"
    "5 CPC_SERVICE_TOKEN CPC_OP_APPROVER token"
    "6 CPC_AGENT_TOKEN CPC_OP_CREDENTIAL_READ token"
    "6 CPC_PERSON_TOKEN CPC_OP_CREDENTIAL_WRITE token"
    "6 CPC_PERSON_TOKEN CPC_OP_RIGHTS_CHANGE token"
    "6 CPC_AGENT_TOKEN CPC_OP_POLICY_CHANGE token"
    "6 CPC_PERSON_TOKEN CPC_OP_POLICY_CHANGE token"
    "6 CPC_SERVICE_TOKEN CPC_OP_DESTRUCTIVE token"
    "6 CPC_PERSON_TOKEN CPC_OP_DELETE token"
    "6 CPC_AGENT_TOKEN CPC_OP_DELETE token"
    "6 CPC_SERVICE_TOKEN CPC_OP_DELETE token"
    "6 CPC_DELETE_GRANT_TOKEN CPC_OP_DELETE token"
    "6 CPC_PERSON_TOKEN CPC_OP_HARMLESS token"
    "6 CPC_PERSON_TOKEN CPC_OP_CALLER_ADDRESS token"
    "8 CPC_ADMIN_TOKEN CPC_OP_WRITE web"
    "8 CPC_ADMIN_TOKEN CPC_OP_WRITE token"
    "10 CPC_PERSON_TOKEN CPC_OP_OWN_AGENT token"
)

# explain STEP TOKEN_NAME OP_VAR VIA: one explain request; sets EXPLAIN_REQUEST_ID.
EXPLAIN_REQUEST_ID=""
explain() {
    local step="$1" token_name="$2" op_var="$3" via="$4" body
    EXPLAIN_REQUEST_ID="$(od -An -N16 -tx1 /dev/urandom 2>/dev/null | tr -d ' \n')"
    body="$(jq -nc --arg c "$(op_part "$op_var" 1)" --arg o "$(op_part "$op_var" 2)" \
        --arg t "$(op_part "$op_var" 3)" --arg v "$via" \
        '{connector_id:$c, op_id:$o, me:true}
         + (if $t == "" then {} else {target:$t} end)
         + (if $v == "web" then {via:"web"} else {} end)')"
    REST_HEADER="X-Request-Id: $EXPLAIN_REQUEST_ID"
    rest "$step" POST /api/v1/policy/explain "$token_name" "$body"
    REST_HEADER=""
}

# probe STEP TOKEN_NAME OP_VAR VIA: sets PROBE_ANSWER, PROBE_ROW and PROBE_PROBLEM.
# A probe counts only with HTTP 200, an answer run, wait or deny, and its audit row.
PROBE_ANSWER=""; PROBE_ROW=""; PROBE_PROBLEM=""; PROBE_SINCE=""
probe() {
    local step="$1" code request_id
    explain "$step" "$2" "$3" "$4"
    code="$REPLY_CODE"
    PROBE_ANSWER="$(jq -r '.answer // "no answer"' <<<"$REPLY_BODY" 2>/dev/null || echo "no answer")"
    # The probe's own request row: the one with the request id it sent.
    # Recorded answers have no request id to match, so a dry run takes the first row.
    request_id="$EXPLAIN_REQUEST_ID"
    [[ -n "$DRY_DIR" ]] && request_id=""
    audit_rows "$step-row" "$(jq -nc --arg s "$PROBE_SINCE" '{since:$s, op_id:"policy.explain", limit:1000}')"
    PROBE_ROW="$(jq -r --arg r "$request_id" \
        '[.[] | select($r == "" or ((.request_id // "") | gsub("-"; "")) == $r) | .id][0] // ""' \
        <<<"$ROWS")"
    PROBE_PROBLEM=""
    if [[ "$code" != 200 ]]; then
        PROBE_PROBLEM="HTTP $code"
    elif [[ "$PROBE_ANSWER" != run && "$PROBE_ANSWER" != wait && "$PROBE_ANSWER" != deny ]]; then
        PROBE_PROBLEM="no answer run, wait or deny"
    elif [[ -z "$PROBE_ROW" ]]; then
        PROBE_PROBLEM="no audit row"
    fi
}

probe_key() { printf '%s %s %s %s' "$1" "${2#CPC_}" "${3#CPC_OP_}" "$4"; }

probe_who() { # probe_who CHECK TOKEN OP VIA: the text of a check 7 line
    printf 'as check %s: %s %s, %s' "$1" "${2#CPC_}" "${3#CPC_OP_}" \
        "$([[ "$4" == web ]] && echo "web console" || echo "token")"
}

# --- The switch CUSTOM_POLICY -------------------------------------------------

# read_switch STEP: reads /ready; sets SWITCH to on, off, or unknown.
SWITCH=""; READY_CODE=""; READY_BODY=""
read_switch() {
    get_open "$1" /ready CPC_METRICS_TOKEN
    READY_CODE="$REPLY_CODE"
    READY_BODY="$REPLY_BODY"
    SWITCH="$(jq -r '.features.policy.custom_policy
        | if . == true then "on" elif . == false then "off" else "unknown" end' \
        <<<"$READY_BODY" 2>/dev/null || echo unknown)"
}

record_baseline() {
    local tmp="$BASELINE_OUT.new.$$" entry check token_name op_var via key bad=0
    PROBE_SINCE="$(now_iso)"
    : >"$tmp"
    local i=0
    for entry in "${PROBES[@]}"; do
        i=$((i + 1))
        read -r check token_name op_var via <<<"$entry"
        key="$(probe_key "$check" "$token_name" "$op_var" "$via")"
        probe "baseline-$i" "$token_name" "$op_var" "$via"
        if [[ -n "$PROBE_PROBLEM" ]]; then
            bad=1
            printf '[FAIL] baseline (%s): %s, audit row %s; %s\n' \
                "$(probe_who "$check" "$token_name" "$op_var" "$via")" "$PROBE_ANSWER" "${PROBE_ROW:-none}" "$PROBE_PROBLEM"
            continue
        fi
        printf '%s\t%s\n' "$key" "$PROBE_ANSWER" >>"$tmp"
        note "baseline ($(probe_who "$check" "$token_name" "$op_var" "$via")): $PROBE_ANSWER, audit row $PROBE_ROW"
    done
    if [[ $bad -ne 0 ]]; then
        rm -f "$tmp"
        echo "custom-policy-check.sh: no baseline saved: a probe got no valid answer. Any earlier file $BASELINE_OUT stays as it was." >&2
        exit 1
    fi
    mv "$tmp" "$BASELINE_OUT"
}

# --- The checks --------------------------------------------------------------

PERSON_STEP_1_DONE=""
C1_ROW=""

check_1() {
    local since op connector
    since="$(now_iso)"
    op="$(op_part CPC_OP_LOWERED 2)"; connector="$(op_part CPC_OP_LOWERED 1)"
    if ! person p1 "Check 1. As an Admin, in the web console, lower $(op_label CPC_OP_LOWERED) from dangerous to caution. Give a reason and sign in again when MEHO asks. Then, as the Operator of CPC_PERSON_TOKEN, in the web console, run $(op_label CPC_OP_LOWERED)."; then
        needs_person "check 1 (an Admin and an Operator, persons, web console)"
        return
    fi
    PERSON_STEP_1_DONE=yes
    # The override made in this run: its row of the override list.
    rest c1-overrides GET /api/v1/connectors/operation-overrides CPC_ADMIN_TOKEN
    local override_ref
    override_ref="$(jq -r --arg o "$op" --arg c "$connector" --arg s "$since" \
        '[.overrides[]? | select(.op_id == $o and .connector_id == $c and .safety_level == "caution"
          and ((.set_at // "") >= $s))] | .[0].rule_ref // ""' <<<"$REPLY_BODY" 2>/dev/null || true)"
    # The console run of the operation: the newest row of the op since then.
    audit_rows c1-row "$(jq -nc --arg o "$op" --arg s "$since" '{op_id:$o, since:$s, limit:1}')"
    clear_row
    read_row_body "$(jq -c '.[0] // {}' <<<"$ROWS")"
    C1_ROW="$(jq -c '.[0] // {}' <<<"$ROWS")"
    local problems=()
    [[ "$ROW_STATUS" == ok ]] || problems+=("expected runs")
    [[ -n "$override_ref" ]] || problems+=("no override of this run lowers it to caution")
    # Table row: an Admin's edit of the operation; the row names that edit.
    if [[ -n "$override_ref" ]] && { [[ "$ROW_RULE" != *"$override_ref"* ]] || [[ "$ROW_RULE" == *"(shadow)"* ]]; }; then
        problems+=("expected the rule of the override $override_ref")
    fi
    [[ "$ROW_KIND" == person ]] || problems+=("expected caller kind person")
    report "check 1 (the Operator, a person, web console): $(verdict_of "${ROW_STATUS:-none}"), rule ${ROW_RULE:-none}, override ${override_ref:-none}, caller kind ${ROW_KIND:-none}, audit row ${ROW_ID:-none}" ${problems[@]+"${problems[@]}"}
}

check_2() {
    local since request
    preview c2-level rest CPC_PERSON_TOKEN CPC_OP_RELEVELLED
    if [[ "$PREVIEW_LEVEL" == dangerous ]]; then
        check_ok "check 2 (an Operator's token, REST preview): risk level dangerous"
    else
        check_fail "check 2 (an Operator's token, REST preview): risk level ${PREVIEW_LEVEL:-none}; expected dangerous"
    fi
    call_op c2-call rest CPC_PERSON_TOKEN CPC_OP_RELEVELLED
    request="$CALL_REQUEST"
    # Table row: "floor", a token call runs only safe operations.
    expect c2-call-row 2 "an Operator's token, a personal agent" REST waits "floor:token_runs_only_safe" personal_agent
    since="$(now_iso)"
    if person p2-operator "Check 2. As a second Operator (not the one of CPC_PERSON_TOKEN), in the web console, click Approve on request ${request:-<none>}. MEHO must refuse it."; then
        expect_click 2 "a second Operator, a person" "$request" refused "$since" c2-operator
    else
        needs_person "check 2 (a second Operator, a person, web console)"
    fi
    since="$(now_iso)"
    if person p2-admin "Check 2. As an Admin, in the web console, approve request ${request:-<none>}."; then
        expect_click 2 "an Admin, a person" "$request" approved "$since" c2-admin
    else
        needs_person "check 2 (an Admin, a person, web console)"
    fi
}

check_3() {
    call_op c3-nopreview rest CPC_PERSON_TOKEN CPC_OP_ALWAYS_WAIT
    row_by_id c3-nopreview-row "$CALL_AUDIT"
    local line problems=()
    line="check 3 (an Operator's token, a personal agent, REST): without a preview: $(verdict_of "$CALL_STATUS")${CALL_CODE:+ ($CALL_CODE)}, audit row ${ROW_ID:-none}"
    [[ "$CALL_STATUS" != ok && "$CALL_CODE" == preview_binding_required ]] \
        || problems+=("expected preview_binding_required")
    [[ -n "$ROW_ID" ]] || problems+=("no audit row")
    report "$line" ${problems[@]+"${problems[@]}"}
    call_previewed c3-call rest CPC_PERSON_TOKEN CPC_OP_ALWAYS_WAIT
    # Table row: "floor", a destructive operation waits.
    expect c3-call-row 3 "an Operator's token, a personal agent" REST waits "floor:destructive_waits" personal_agent
}

check_4() {
    if ! person p4 "Check 4. As an Admin, in the web console, add a personal agent rule that lets the AI tool of the Operator of CPC_PERSON_TOKEN run $(op_label CPC_OP_AGENT_RULE) at once. Add no grant for the service login."; then
        needs_person "check 4 (an Admin, a person, web console)"
        waits_for "check 4 (an Operator's AI tool, a personal agent, MCP)" 4
        waits_for "check 4 (a service login without a grant, a service agent, REST)" 4
        return
    fi
    call_op c4-agent mcp CPC_PERSON_TOKEN CPC_OP_AGENT_RULE
    # Table row: "tenant_rule", a rule of the tenant's agent policy.
    expect c4-agent-row 4 "an Operator's AI tool, a personal agent" MCP runs "tenant_rule:" personal_agent
    call_op c4-service rest CPC_SERVICE_TOKEN CPC_OP_AGENT_RULE
    # Table row: "default", the default for a service agent.
    expect c4-service-row 4 "a service login without a grant, a service agent" REST waits "default:" service_agent
}

check_5() {
    local since request
    if ! person p5-role "Check 5. As an Admin, in the web console, make a custom role named ${CPC_CUSTOM_ROLE:-<CPC_CUSTOM_ROLE>} that approves requests on connector $(op_part CPC_OP_APPROVER 1), and give it to one person. Make sure a second person does not have the role."; then
        needs_person "check 5 (an Admin, a person, web console)"
        waits_for "check 5 (a service login, a service agent, REST)" 5
        waits_for "check 5 (a person without the role, web console)" 5
        waits_for "check 5 (a person with the custom role, web console)" 5
        return
    fi
    call_op c5-call rest CPC_SERVICE_TOKEN CPC_OP_APPROVER
    request="$CALL_REQUEST"
    # Table row: "default", a service login's dangerous call waits.
    expect c5-call-row 5 "a service login, a service agent" REST waits "default:service.dangerous" service_agent
    since="$(now_iso)"
    if person p5-without "Check 5. As the person WITHOUT the custom role, in the web console, click Approve on request ${request:-<none>}. MEHO must refuse it at the click."; then
        expect_click 5 "a person without the role" "$request" refused "$since" c5-without
    else
        needs_person "check 5 (a person without the role, web console)"
    fi
    since="$(now_iso)"
    if person p5-with "Check 5. As the person WITH the custom role, in the web console, approve request ${request:-<none>}."; then
        expect_click 5 "a person with the custom role" "$request" approved "$since" c5-with
    else
        needs_person "check 5 (a person with the custom role, web console)"
    fi
}

# The deletes of check 6. A person step guards them: one of them runs.
check_6_deletes() {
    local who_lowered="check 6 delete after a lowering (an Operator's token, a personal agent, REST)"
    if ! person p6-delete "Check 6. The next steps call $(op_label CPC_OP_DELETE) on target $(op_part CPC_OP_DELETE 3) as four logins. The service login with the delete grant runs it at once. Make sure the target is a disposable system and the delete grant is in place."; then
        needs_person "check 6 delete (a person, web console)"
        waits_for "check 6 delete (an Operator's token, a personal agent, REST)" 6
        waits_for "check 6 delete (an agent login, a service agent, MCP)" 6
        waits_for "check 6 delete (a service login without a grant, a service agent, REST)" 6
        waits_for "check 6 delete (a service login with a delete grant, a service agent, REST)" 6
        waits_for "$who_lowered" 6
        return
    fi
    # The delete line names each caller (decision 5 of the record): in
    # sandbox mode a service login with a delete grant does not wait.
    # Each delete is previewed first with the same login, target and params.
    # Table row: "floor", a delete waits.
    call_previewed c6-delete-person rest CPC_PERSON_TOKEN CPC_OP_DELETE
    expect c6-delete-person-row "6 delete" "an Operator's token, a personal agent" REST waits "floor:delete_waits" personal_agent
    local level_before="$PREVIEW_LEVEL"
    call_previewed c6-delete-agent mcp CPC_AGENT_TOKEN CPC_OP_DELETE
    expect c6-delete-agent-row "6 delete" "an agent login, a service agent" MCP waits "floor:delete_waits" service_agent
    call_previewed c6-delete-service rest CPC_SERVICE_TOKEN CPC_OP_DELETE
    expect c6-delete-service-row "6 delete" "a service login without a grant, a service agent" REST waits "floor:delete_waits" service_agent
    call_previewed c6-delete-grant rest CPC_DELETE_GRANT_TOKEN CPC_OP_DELETE
    # Table row: "standing_grant", the sandbox delete grant.
    expect c6-delete-grant-row "6 delete" "a service login with a delete grant, a service agent" REST runs "standing_grant:sandbox_delete_grant" service_agent

    if ! person p6-lower "Check 6. As an Admin, in the web console, lower $(op_label CPC_OP_DELETE) by one risk level, with a reason and a fresh sign-in."; then
        needs_person "check 6 delete lowering (an Admin, a person, web console)"
        waits_for "$who_lowered" 6
        return
    fi
    call_previewed c6-delete-lowered rest CPC_PERSON_TOKEN CPC_OP_DELETE
    if [[ -z "$PREVIEW_LEVEL" || "$PREVIEW_LEVEL" == "$level_before" ]]; then
        check_fail "$who_lowered: risk level ${PREVIEW_LEVEL:-none}, before ${level_before:-none}; expected a lower risk level"
    else
        check_ok "check 6 delete after a lowering (an Operator's token, REST preview): risk level $PREVIEW_LEVEL, before $level_before"
    fi
    expect c6-delete-lowered-row "6 delete after a lowering" "an Operator's token, a personal agent" REST waits "floor:delete_waits" personal_agent
}

# The agent's approve attempt: on the request of the harmless operation only.
check_6_agent_approve() {
    local since request params code detail
    call_op c6-harmless rest CPC_PERSON_TOKEN CPC_OP_HARMLESS
    request="$CALL_REQUEST"
    note "check 6 agent approve: the Operator's token asked for $(op_label CPC_OP_HARMLESS): $(verdict_of "$CALL_STATUS"), request ${request:-none}"
    local line="check 6 agent approve (an agent login, a service agent, REST)"
    if [[ -z "$request" ]]; then
        check_fail "$line: no request of $(op_label CPC_OP_HARMLESS) to try; expected it to wait"
        return
    fi
    since="$(now_iso)"
    params="$(op_params CPC_OP_HARMLESS | jq -c '{params:.}')"
    rest c6-agent-approve POST "/api/v1/approvals/$request/approve" CPC_AGENT_TOKEN "$params"
    code="$REPLY_CODE"
    detail="$(jq -r '.detail // "" | if type == "string" then . else tostring end' <<<"$REPLY_BODY" 2>/dev/null || true)"
    request_status c6-agent-approve-request "$request"
    post_rows c6-agent-approve-rows "$since" "/api/v1/approvals/$request/approve"
    local problems=()
    # The route's person-only check (require_human_principal): 403 human_principal_required.
    [[ "$code" == 403 && "$detail" == human_principal_required* ]] \
        || problems+=("expected HTTP 403 human_principal_required")
    [[ "$REQUEST_STATUS" == pending ]] || problems+=("expected a request that still waits")
    [[ -n "$ROW_IDS" ]] || problems+=("expected the audit row of the attempt")
    report "$line: HTTP $code ${detail%%:*}, request $request is $REQUEST_STATUS, audit rows ${ROW_IDS:-none}" ${problems[@]+"${problems[@]}"}
}

check_6() {
    # Each line: step, caller, token, path, operation, expected verdict,
    # expected rule (its table row), expected caller kind.
    call_op c6-cred-read mcp CPC_AGENT_TOKEN CPC_OP_CREDENTIAL_READ
    # Table row: "floor", a credential read or write waits.
    expect c6-cred-read-row "6 credential read" "an agent login, a service agent" MCP waits "floor:credential_waits" service_agent

    call_op c6-cred-write cli CPC_PERSON_TOKEN CPC_OP_CREDENTIAL_WRITE
    # Table row: "floor", a credential read or write waits.
    expect c6-cred-write-row "6 credential write" "the Operator's meho login, a personal agent" "command line" waits "floor:credential_waits" personal_agent

    call_op c6-rights rest CPC_PERSON_TOKEN CPC_OP_RIGHTS_CHANGE
    # Table row: "floor", a change of rights waits.
    expect c6-rights-row "6 change of rights" "an Operator's token, a personal agent" REST waits "floor:rights_change_waits" personal_agent

    # An agent never asks for a policy change: MEHO refuses it before anything
    # waits (policy_apply_requester_refusal). A person's token waits.
    call_op c6-policy-agent mcp CPC_AGENT_TOKEN CPC_OP_POLICY_CHANGE
    expect c6-policy-agent-row "6 change of the policy" "an agent login, a service agent" MCP refused "" service_agent "Only a person can ask for a policy import"
    call_op c6-policy mcp CPC_PERSON_TOKEN CPC_OP_POLICY_CHANGE
    # Table row: "floor", a change of the policy waits.
    expect c6-policy-row "6 change of the policy" "an Operator's AI tool, a personal agent" MCP waits "floor:policy_change_waits" personal_agent

    call_previewed c6-destructive rest CPC_SERVICE_TOKEN CPC_OP_DESTRUCTIVE
    # Table row: "floor", a destructive operation waits.
    expect c6-destructive-row "6 destructive" "a service login without a grant, a service agent" REST waits "floor:destructive_waits" service_agent

    check_6_deletes

    call_op c6-address mcp CPC_PERSON_TOKEN CPC_OP_CALLER_ADDRESS
    # Table row: "floor", a call to an address the caller gives waits.
    expect c6-address-row "6 caller address" "an Operator's AI tool, a personal agent" MCP waits "floor:caller_address_waits" personal_agent

    # The root command on a host: its fixed rule id comes with #7051.
    check_skip "check 6 root command (a service login, a service agent, REST): not run: waits for #7051"

    check_6_agent_approve
}

check_8() {
    local since
    since="$(now_iso)"
    if person p8 "Check 8. As the Admin of CPC_ADMIN_TOKEN, in the web console, try to lower $(op_label CPC_OP_WRITE) (a write) to safe, with a reason and a fresh sign-in. MEHO must refuse it."; then
        post_rows c8-console-rows "$since" "/ui/"
        preview c8-level rest CPC_ADMIN_TOKEN CPC_OP_WRITE
        local problems=()
        [[ -n "$ROW_IDS" ]] || problems+=("expected the audit row of the try")
        [[ -n "$PREVIEW_LEVEL" && "$PREVIEW_LEVEL" != safe ]] || problems+=("expected the risk level to stay at least caution")
        report "check 8 (an Admin, a person, web console): risk level after the try ${PREVIEW_LEVEL:-none}, audit rows ${ROW_IDS:-none}" ${problems[@]+"${problems[@]}"}
    else
        needs_person "check 8 (an Admin, a person, web console)"
    fi

    local file op
    op="$(op_part CPC_OP_WRITE 2)"
    file="$(printf 'policy: v1\nclassifications:\n  - match: {connector: "%s", op: "%s"}\n    risk: safe\n' \
        "$(op_part CPC_OP_WRITE 1)" "$op")"
    since="$(now_iso)"
    rest c8-token POST "/api/v1/policy/import?preview=true" CPC_ADMIN_TOKEN "$file" application/yaml
    local code="$REPLY_CODE" ok mistake
    ok="$(jq -r 'if type == "object" and has("ok") then (.ok | tostring) else "none" end' \
        <<<"$REPLY_BODY" 2>/dev/null || echo none)"
    # The write floor names the operation and says it stays at least caution,
    # in the form of MEHO's other floors (policy/validate.py).
    mistake="$(jq -r --arg o "$op" \
        '[.mistakes[]? | select(contains($o) and contains("at least caution"))][0] // ""' \
        <<<"$REPLY_BODY" 2>/dev/null || true)"
    post_rows c8-token-rows "$since" "/api/v1/policy/import"
    local problems=()
    [[ "$code" == 200 && "$ok" == false ]] || problems+=("expected HTTP 200 with ok false")
    [[ -n "$mistake" ]] || problems+=("expected the mistake that a write stays at least caution")
    [[ -n "$ROW_IDS" ]] || problems+=("no audit row")
    report "check 8 (an Admin's token, REST, a policy file preview): HTTP $code, ok $ok, mistake ${mistake:-none}, audit rows ${ROW_IDS:-none}" ${problems[@]+"${problems[@]}"}
}

check_9() {
    local since
    since="$(now_iso)"
    if ! person p9 "Check 9. As an Admin whose sign-in is older than the fresh sign-in time, in the web console: raise $(op_label CPC_OP_WRITE) to dangerous, then remove that override again."; then
        needs_person "check 9 (an Admin, a person, web console)"
        return
    fi
    ask c9-fresh "Did MEHO ask for a fresh sign-in before the raise AND before the removal?"
    post_rows c9-rows "$since" "/ui/"
    local line="check 9 (an Admin, a person, web console): fresh sign-in asked each time: $ANSWER, audit rows ${ROW_IDS:-none}"
    if [[ "$ANSWER" == none ]]; then
        needs_person "check 9 (an Admin, a person, web console)"
        return
    fi
    local problems=()
    [[ "$ANSWER" == yes ]] || problems+=("expected a fresh sign-in each time")
    [[ "$ROW_IDS" == *,* ]] || problems+=("expected the audit rows of the raise and the removal")
    report "$line" ${problems[@]+"${problems[@]}"}
}

check_10() {
    local since request_web request_cli
    call_op c10-web mcp CPC_PERSON_TOKEN CPC_OP_OWN_AGENT
    request_web="$CALL_REQUEST"
    # Table row: "floor", a token call runs only safe operations.
    expect c10-web-row 10 "the Operator's AI tool, a personal agent" MCP waits "floor:token_runs_only_safe" personal_agent
    since="$(now_iso)"
    if person p10-web "Check 10. As the Operator of CPC_PERSON_TOKEN, in the web console, approve your own request ${request_web:-<none>}."; then
        expect_click 10 "the Operator, a person" "$request_web" own "$since" c10-web-approve
    else
        needs_person "check 10 (the Operator, a person, web console)"
    fi

    call_op c10-cli mcp CPC_PERSON_TOKEN CPC_OP_OWN_AGENT
    request_cli="$CALL_REQUEST"
    expect c10-cli-row 10 "the Operator's AI tool, a personal agent" MCP waits "floor:token_runs_only_safe" personal_agent
    printf '[person] %s\n' "Check 10. As the same Operator, on the command line: meho approvals approve ${request_cli:-<none>} --backplane $BACKPLANE. MEHO must refuse it, because only the web console approves your own agent's request."
    check_skip "check 10 (command line approve): needs a person"
}

# Check 11: each login of MEHO's own code, with the caller kind it writes.
check_11_runner() {
    local since tries=0 op target identity code
    since="$(now_iso)"
    rest c11-sensor GET "/api/v1/sensors/${CPC_SENSOR_ID:-none}" CPC_ADMIN_TOKEN
    op="$(jq -r '.op_id // ""' <<<"$REPLY_BODY" 2>/dev/null || true)"
    target="$(jq -r '.target.name // ""' <<<"$REPLY_BODY" 2>/dev/null || true)"
    identity="$(jq -r '.identity_sub // ""' <<<"$REPLY_BODY" 2>/dev/null || true)"
    rest c11-run POST "/api/v1/sensors/${CPC_SENSOR_ID:-none}/run" CPC_ADMIN_TOKEN '{}'
    code="$REPLY_CODE"
    local filter
    filter="$(jq -nc --arg o "$op" --arg s "$since" '{op_id:$o, since:$s, limit:1000}')"
    clear_row
    while :; do
        audit_rows c11-row "$filter"
        # The row of this sensor's run: its operation, its target and its login.
        local found
        found="$(jq -c --arg t "$target" --arg i "$identity" \
            '[.[] | select((.target_name // "") == $t and .principal_sub == $i)][0] // empty' <<<"$ROWS")"
        if [[ -n "$found" ]]; then read_row_body "$found"; fi
        [[ -n "$ROW_ID" || -n "$DRY_DIR" || $tries -ge 24 || ! "$code" =~ ^2 ]] && break
        tries=$((tries + 1))
        sleep 5
    done
    if [[ -n "$ROW_ID" && -z "$ROW_HAS_KIND" ]]; then
        check_skip "check 11: not run: waits for #7073"
        return 1
    fi
    local problems=()
    [[ -n "$op" && -n "$identity" ]] || problems+=("could not read the sensor")
    [[ "$code" =~ ^2 ]] || problems+=("expected the sensor run to start")
    [[ -n "$ROW_ID" ]] || problems+=("no audit row of the sensor's run")
    [[ "$ROW_KIND" == system ]] || problems+=("expected caller kind system")
    report "check 11 (the check runner, a sensor run started over REST): HTTP $code, caller kind ${ROW_KIND:-none}, rule ${ROW_RULE:-none}, audit row ${ROW_ID:-none}" ${problems[@]+"${problems[@]}"}
}

check_11() {
    check_11_runner || return 0
    # The web console reader stands for the person: the console run of check 1.
    if [[ -z "$PERSON_STEP_1_DONE" ]]; then
        waits_for "check 11 (the web console reader)" 1
    else
        clear_row
        read_row_body "${C1_ROW:-null}"
        local problems=()
        [[ -n "$ROW_ID" ]] || problems+=("no audit row of the console run of check 1")
        [[ "$ROW_KIND" == person ]] || problems+=("expected caller kind person")
        report "check 11 (the web console reader, the console run of check 1): caller kind ${ROW_KIND:-none}, audit row ${ROW_ID:-none}" ${problems[@]+"${problems[@]}"}
    fi
    check_skip "check 11 (the event intake): not run: the script cannot send an event to the install"
    check_skip "check 11 (the findings investigator): not run: it needs a sensor change and an AI model on the install"
    check_skip "check 11 (the runner executor): not run: it needs a satellite runner"
}

# --- Taking back what the run made -------------------------------------------

LEFT_COUNT=0
left() { LEFT_COUNT=$((LEFT_COUNT + 1)); printf '[left] %s\n' "$1"; }

# Withdraw each request of this run that still waits, with the login that made it.
withdraw_requests() {
    local entry request token_name _rest
    local -a remaining=(${REQUESTS[@]+"${REQUESTS[@]}"})
    REQUESTS=()
    for entry in ${remaining[@]+"${remaining[@]}"}; do
        read -r request token_name _rest <<<"$entry"
        request_status "withdraw-$request-status" "$request"
        [[ "$REQUEST_STATUS" == pending ]] || continue
        rest "withdraw-$request" POST "/api/v1/approvals/$request/withdraw" "$token_name" \
            '{"reason":"custom-policy-check.sh takes back its own request"}'
        if [[ "$REPLY_CODE" != 200 ]]; then
            left "request $request (sent with ${token_name#CPC_}): not withdrawn, HTTP $REPLY_CODE"
        fi
    done
}

# Ask the person to remove what they added, then read back that it is gone.
take_back_changes() {
    if ! person p-takeback "Take back. As an Admin, in the web console: remove the personal agent rule of check 4, the custom role ${CPC_CUSTOM_ROLE:-<CPC_CUSTOM_ROLE>} of check 5, and the lowerings of $(op_label CPC_OP_LOWERED) (check 1) and $(op_label CPC_OP_DELETE) (check 6)."; then
        left "the agent rule, the custom role and the lowerings: needs a person"
        return
    fi
    rest takeback-overrides GET /api/v1/connectors/operation-overrides CPC_ADMIN_TOKEN
    local overrides="$REPLY_BODY" op_var op connector
    if [[ "$REPLY_CODE" != 200 ]]; then
        left "the lowerings: could not read the overrides, HTTP $REPLY_CODE"
    else
        for op_var in CPC_OP_LOWERED CPC_OP_DELETE; do
            op="$(op_part "$op_var" 2)"; connector="$(op_part "$op_var" 1)"
            if jq -e --arg o "$op" --arg c "$connector" --arg s "$RUN_START" \
                '[.overrides[]? | select(.op_id == $o and .connector_id == $c and ((.set_at // "") >= $s))] | length > 0' \
                <<<"$overrides" >/dev/null 2>&1; then
                left "the lowering of $(op_label "$op_var"): still there"
            fi
        done
    fi
    explain takeback-agent-rule CPC_PERSON_TOKEN CPC_OP_AGENT_RULE token
    local answer
    answer="$(jq -r '.answer // "none"' <<<"$REPLY_BODY" 2>/dev/null || echo none)"
    if [[ "$REPLY_CODE" != 200 || "$answer" == run || "$answer" == none ]]; then
        left "the agent rule of check 4: still there or not readable (the Operator's token gets $answer for $(op_label CPC_OP_AGENT_RULE))"
    fi
    explain takeback-role CPC_ADMIN_TOKEN CPC_OP_APPROVER token
    if [[ "$REPLY_CODE" != 200 ]] || jq -e --arg r "${CPC_CUSTOM_ROLE:-}" \
        '[.approvers.groups[]? | select(. == $r)] | length > 0' <<<"$REPLY_BODY" >/dev/null 2>&1; then
        left "the custom role ${CPC_CUSTOM_ROLE:-} of check 5: still there or not readable"
    fi
}

check_7() {
    if ! person p7 "Check 7. Turn the switch CUSTOM_POLICY off on the install and restart MEHO. Wait until /ready answers again."; then
        needs_person "check 7 (a person turns the switch off)"
        waits_for "check 7 (every probe, REST policy explain)" 7
        return
    fi
    read_switch c7-ready
    if [[ "$SWITCH" != off ]]; then
        check_fail "check 7 (REST /ready): the switch CUSTOM_POLICY is $SWITCH; expected off"
        return
    fi
    PROBE_SINCE="$(now_iso)"
    local baseline="${CPC_BASELINE:-$DRY_DIR/baseline.tsv}"
    if [[ ! -f "$baseline" ]]; then
        check_fail "check 7: no baseline file; run --record-baseline before the switch is turned on"
        return
    fi
    local i=0 entry check token_name op_var via key before
    for entry in "${PROBES[@]}"; do
        i=$((i + 1))
        read -r check token_name op_var via <<<"$entry"
        key="$(probe_key "$check" "$token_name" "$op_var" "$via")"
        before="$(awk -F'\t' -v k="$key" '$1 == k {print $2}' "$baseline")"
        probe "c7-$i" "$token_name" "$op_var" "$via"
        local problems=()
        [[ -z "$PROBE_PROBLEM" ]] || problems+=("$PROBE_PROBLEM")
        [[ "$before" == run || "$before" == wait || "$before" == deny ]] || problems+=("no valid baseline answer")
        [[ "$PROBE_ANSWER" == "$before" ]] || problems+=("expected the answer of before")
        report "check 7 ($(probe_who "$check" "$token_name" "$op_var" "$via"), REST policy explain): $PROBE_ANSWER, before ${before:-none}, audit row ${PROBE_ROW:-none}" ${problems[@]+"${problems[@]}"}
    done
}

# --- Main ----------------------------------------------------------------------

note "the install: ${BACKPLANE:-<recorded answers>}"
read_switch ready
SANDBOX="$(jq -r '.sandbox_mode // "unknown"' <<<"$READY_BODY" 2>/dev/null || echo unknown)"
READY_STATUS="$(jq -r '.status // "unknown"' <<<"$READY_BODY" 2>/dev/null || echo unknown)"
if [[ "$SANDBOX" != on ]]; then
    stop "refused: the install is not in sandbox mode (sandbox_mode: $SANDBOX). Run it only on a sandbox install."
fi
if [[ "$READY_CODE" != 200 || "$READY_STATUS" != ready ]]; then
    stop "refused: /ready answers HTTP $READY_CODE with status $READY_STATUS, not 200 ready."
fi
get_open version /version
note "the install's version: $(jq -r '"\(.git_sha // "unknown")\(if .chart_version then " (chart \(.chart_version))" else "" end)"' <<<"$REPLY_BODY" 2>/dev/null || echo unknown)"
note "the install is in sandbox mode"

if [[ -n "$BASELINE_OUT" ]]; then
    [[ "$SWITCH" == off ]] || stop "refused: the switch CUSTOM_POLICY is $SWITCH. Record the baseline with the switch off."
    record_baseline
    note "baseline saved; now turn the switch CUSTOM_POLICY on and run the checks"
    exit 0
fi

[[ "$SWITCH" == on ]] || stop "refused: the switch CUSTOM_POLICY is $SWITCH. Turn it on and restart MEHO first (set CPC_METRICS_TOKEN when /ready hides its features)."
note "the switch CUSTOM_POLICY is on"

RUN_START="$(now_iso)"

check_1
check_2
check_3
check_4
check_5
check_6
check_8
check_9
check_10
check_11
withdraw_requests
take_back_changes
check_7

echo
echo "custom-policy-check.sh: $PASS_COUNT passed, $FAIL_COUNT failed, $SKIP_COUNT not run, $LEFT_COUNT not taken back"
if [[ $FAIL_COUNT -gt 0 ]]; then exit 1; fi
if [[ $SKIP_COUNT -gt 0 || $LEFT_COUNT -gt 0 ]]; then exit 3; fi
exit 0
