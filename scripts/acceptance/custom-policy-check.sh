#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# custom-policy-check.sh: run the checks of the custom policy sandbox test.
#
# One run checks that, with the switch CUSTOM_POLICY on, the custom policy
# model and every fixed rule work on a real install, through MEHO's own
# paths: the web console, the `meho` command line, MCP, and REST with a
# login's token. For each check the script prints [ok] or [FAIL], the
# caller, the path, and the audit row ids.
#
# The script runs only against an install in sandbox mode
# (docs/decisions/sandbox-mode.md). It reads every token, target and
# operation from the environment. A step that needs a person in the web
# console prints what the person must do and waits for Enter.
#
# The expected rule ids sit next to each check. Each one names its row in
# the rule table of docs/codebase/policy-evaluator.md.
#
# Exit codes: 0 every check passed, 1 at least one check failed,
# 2 usage error, missing input, or an install that is not in sandbox mode.
#
# How to run it, and what each check proves: docs/acceptance/custom-policy-check.md.

set -Eeuo pipefail

usage() {
    cat <<'HELP'
custom-policy-check.sh: run the checks of the custom policy sandbox test.

Usage:
  bash scripts/acceptance/custom-policy-check.sh [--backplane URL]
  bash scripts/acceptance/custom-policy-check.sh --record-baseline FILE
  bash scripts/acceptance/custom-policy-check.sh --dry-run DIR

  --backplane URL         The install. Default: $MEHO_BACKPLANE.
  --record-baseline FILE  With the switch CUSTOM_POLICY still off, save the
                          answers that check 7 compares with, then stop.
                          Run this once before the switch is turned on.
  --dry-run DIR           Read every answer from the recorded answers in
                          DIR (one file <step>.json each) and wait for no
                          person. For the script's own test.
  -h, --help              Print this text.

What it does:
  It refuses to run when /ready does not say "sandbox_mode": "on".
  Then it runs the checks 1 to 6 and 8 to 11 with the switch on. Last, a
  person turns the switch off, and check 7 compares the answers with the
  baseline. So the install ends with the switch off.

Checks:
   1  a lowered level: a person in the console runs the operation at once
   2  a re-levelled operation: an Operator cannot approve it, an Admin can
   3  a carried-over "always wait": needs a preview, then waits
   4  an agent policy: a personal agent runs, a service login waits
   5  an approver policy: only the custom role approves
   6  each fixed rule waits; an agent's approve attempt is refused
   8  a write is never lowered below caution (console and token)
   9  every policy change in the console needs a fresh sign-in
  10  a person approves their own agent's request in the console only
  11  a check-runner call has the caller kind "system"
   7  the switch off again: every answer is as before

Environment (live run). Each CPC_OP_* value is "<connector_id> <op_id> <target>".
A CPC_PARAMS_<name> value (for example CPC_PARAMS_DELETE) is the JSON of the
call's params; the default is {}.

  MEHO_BACKPLANE          The install's address.
  CPC_ADMIN_TOKEN         A tenant Admin's token. Reads audit rows and requests.
  CPC_PERSON_TOKEN        An Operator's own token (a personal agent over REST and MCP).
  CPC_SERVICE_TOKEN       A service login with no standing grant.
  CPC_DELETE_GRANT_TOKEN  A service login with a delete grant for CPC_OP_DELETE.
  CPC_AGENT_TOKEN         An agent login.
  CPC_BASELINE            The file of --record-baseline.
  CPC_OP_LOWERED          check 1: a dangerous operation an Admin lowers to caution.
  CPC_OP_RELEVELLED       check 2: a former caution operation with the plain mark.
  CPC_OP_ALWAYS_WAIT      check 3: an operation with a saved "always wait".
  CPC_OP_AGENT_RULE       check 4: a caution operation for the personal agent rule.
  CPC_OP_APPROVER         check 5: a dangerous operation on the approver policy's connector.
  CPC_OP_CREDENTIAL_READ  check 6: an operation that reads a credential.
  CPC_OP_CREDENTIAL_WRITE check 6: an operation that writes a credential.
  CPC_OP_RIGHTS_CHANGE    check 6: an operation that changes rights.
  CPC_OP_POLICY_CHANGE    check 6: an operation that changes the policy.
  CPC_OP_DESTRUCTIVE      check 6: a destructive operation.
  CPC_OP_DELETE           check 6: a delete of a disposable system.
  CPC_OP_CALLER_ADDRESS   check 6: an operation that calls an address the caller gives.
  CPC_OP_ROOT_COMMAND     check 6: a root command on a host.
  CPC_OP_WRITE            check 8: a caution write.
  CPC_OP_OWN_AGENT        check 10: an operation that waits for the person's own agent.
  CPC_SENSOR_ID           check 11: a sensor whose check runs CPC_OP_SENSOR.
  CPC_OP_SENSOR           check 11: the operation of that sensor.

The `meho` command line must be signed in as the same Operator.
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

command -v jq >/dev/null || { echo "custom-policy-check.sh: jq is needed" >&2; exit 2; }

if [[ -z "$DRY_DIR" ]]; then
    command -v curl >/dev/null || { echo "custom-policy-check.sh: curl is needed" >&2; exit 2; }
    [[ -n "$BACKPLANE" ]] || { echo "custom-policy-check.sh: set MEHO_BACKPLANE or --backplane" >&2; exit 2; }
    missing=()
    for name in CPC_ADMIN_TOKEN CPC_PERSON_TOKEN CPC_SERVICE_TOKEN CPC_DELETE_GRANT_TOKEN \
        CPC_AGENT_TOKEN CPC_OP_LOWERED CPC_OP_RELEVELLED CPC_OP_ALWAYS_WAIT \
        CPC_OP_AGENT_RULE CPC_OP_APPROVER CPC_OP_CREDENTIAL_READ CPC_OP_CREDENTIAL_WRITE \
        CPC_OP_RIGHTS_CHANGE CPC_OP_POLICY_CHANGE CPC_OP_DESTRUCTIVE CPC_OP_DELETE \
        CPC_OP_CALLER_ADDRESS CPC_OP_ROOT_COMMAND CPC_OP_WRITE CPC_OP_OWN_AGENT \
        CPC_SENSOR_ID CPC_OP_SENSOR; do
        [[ -n "${!name:-}" ]] || missing+=("$name")
    done
    if [[ -z "$BASELINE_OUT" && -z "${CPC_BASELINE:-}" ]]; then
        missing+=("CPC_BASELINE")
    fi
    if [[ ${#missing[@]} -gt 0 ]]; then
        echo "custom-policy-check.sh: these are not set: ${missing[*]}" >&2
        exit 2
    fi
    command -v meho >/dev/null || { echo "custom-policy-check.sh: the meho command line is needed" >&2; exit 2; }
fi

PASS_COUNT=0
FAIL_COUNT=0

check_ok() { PASS_COUNT=$((PASS_COUNT + 1)); printf '[ok]   %s\n' "$1"; }
check_fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); printf '[FAIL] %s\n' "$1"; }
note() { printf '[note] %s\n' "$1"; }

# --- Answers: from the install, or recorded -------------------------------
#
# Every request has a step name. In a dry run, the answer of step <name> is
# the file <name>.json in the dry-run folder: {"code": <HTTP code or exit
# code>, "body": <JSON>}. A missing file is the answer code 0 with body null.

REPLY_CODE=""
REPLY_BODY=""

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

# rest STEP METHOD PATH TOKEN_NAME [BODY] [CONTENT_TYPE]
# The token goes to curl on a file descriptor, never on the command line.
rest() {
    local step="$1" method="$2" path="$3" token_name="$4" body="${5:-}" ctype="${6:-application/json}"
    if [[ -n "$DRY_DIR" ]]; then recorded "$step"; return; fi
    local out args
    out="$(mktemp)"
    args=(-sS -o "$out" -w '%{http_code}' -X "$method" -H "Content-Type: $ctype")
    [[ -n "$body" ]] && args+=(--data-binary "$body")
    REPLY_CODE="$(curl "${args[@]}" \
        -H @<(printf 'Authorization: Bearer %s\n' "${!token_name}") \
        "$BACKPLANE$path")" || REPLY_CODE=000
    REPLY_BODY="$(jq -c . "$out" 2>/dev/null || echo null)"
    rm -f "$out"
}

# mcp STEP TOKEN_NAME TOOL ARGUMENTS_JSON: one tools/call; the reply body is the tool result.
mcp() {
    local step="$1" token_name="$2" tool="$3" arguments="$4"
    if [[ -n "$DRY_DIR" ]]; then recorded "$step"; return; fi
    local init call
    init='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"custom-policy-check","version":"1"}}}'
    local headers session
    headers="$(mktemp)"
    curl -sS -o /dev/null -D "$headers" -X POST -H 'Content-Type: application/json' \
        -H @<(printf 'Authorization: Bearer %s\n' "${!token_name}") \
        --data-binary "$init" "$BACKPLANE/mcp" || true
    session="$(awk 'tolower($1) == "mcp-session-id:" {print $2}' "$headers" | tr -d '\r')"
    rm -f "$headers"
    call="$(jq -nc --arg t "$tool" --argjson a "$arguments" \
        '{jsonrpc:"2.0",id:2,method:"tools/call",params:{name:$t,arguments:$a}}')"
    local out
    out="$(mktemp)"
    REPLY_CODE="$(curl -sS -o "$out" -w '%{http_code}' -X POST \
        -H 'Content-Type: application/json' -H 'MCP-Protocol-Version: 2025-06-18' \
        ${session:+-H "Mcp-Session-Id: $session"} \
        -H @<(printf 'Authorization: Bearer %s\n' "${!token_name}") \
        --data-binary "$call" "$BACKPLANE/mcp")" || REPLY_CODE=000
    REPLY_BODY="$(jq -c '.result.structuredContent // (.result.content[0].text | fromjson? ) // .' \
        "$out" 2>/dev/null || echo null)"
    rm -f "$out"
}

# cli STEP ARGS...: the meho command line, signed in as the Operator.
cli() {
    local step="$1"; shift
    if [[ -n "$DRY_DIR" ]]; then recorded "$step"; return; fi
    local out
    out="$(mktemp)"
    REPLY_CODE=0
    meho "$@" --json >"$out" 2>/dev/null || REPLY_CODE=$?
    REPLY_BODY="$(jq -c . "$out" 2>/dev/null || echo null)"
    rm -f "$out"
}

# person TEXT: a person does TEXT in the web console, then presses Enter.
person() {
    printf '[person] %s\n' "$1"
    if [[ -n "$DRY_DIR" ]]; then return; fi
    printf '         Press Enter when it is done. '
    read -r _ </dev/tty || true
}

# ask STEP QUESTION: a person answers yes or no. Sets ANSWER to yes or no.
ANSWER=""
ask() {
    local step="$1" question="$2" reply=""
    printf '[person] %s [y/n] ' "$question"
    if [[ -n "$DRY_DIR" ]]; then
        recorded "$step"
        reply="$(jq -r '. // ""' <<<"$REPLY_BODY")"
        printf '%s\n' "$reply"
    else
        read -r reply </dev/tty || true
    fi
    case "$reply" in y|Y|yes) ANSWER=yes ;; *) ANSWER=no ;; esac
}

now_iso() {
    if [[ -n "$DRY_DIR" ]]; then echo "2026-01-01T00:00:00Z"; else date -u +%Y-%m-%dT%H:%M:%SZ; fi
}

# --- Operations ------------------------------------------------------------

op_part() { # op_part VAR N: word N (1-3) of "<connector_id> <op_id> <target>"
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

# call_op STEP PATH TOKEN_NAME OP_VAR [PREVIEW_HASH]
# PATH is rest, mcp or cli. Sets CALL_STATUS, CALL_CODE, CALL_AUDIT, CALL_REQUEST.
CALL_STATUS=""; CALL_CODE=""; CALL_AUDIT=""; CALL_REQUEST=""
call_op() {
    local step="$1" path="$2" token_name="$3" op_var="$4" preview_hash="${5:-}"
    local connector op_id target params body
    connector="$(op_part "$op_var" 1)"
    op_id="$(op_part "$op_var" 2)"
    target="$(op_part "$op_var" 3)"
    params="$(op_params "$op_var")"
    body="$(jq -nc --arg c "$connector" --arg o "$op_id" --arg t "$target" \
        --argjson p "$params" --arg h "$preview_hash" \
        '{connector_id:$c, op_id:$o, target:$t, params:$p} + (if $h == "" then {} else {preview_hash:$h} end)' \
        2>/dev/null || echo '{}')"
    case "$path" in
        rest) rest "$step" POST /api/v1/operations/call "$token_name" "$body" ;;
        mcp) mcp "$step" "$token_name" call_operation "$body" ;;
        cli)
            local cli_args=(operation call "$connector" "$op_id" --target "$target" --params "$params")
            [[ -n "$preview_hash" ]] && cli_args+=(--preview-hash "$preview_hash")
            cli "$step" "${cli_args[@]}" ;;
    esac
    CALL_STATUS="$(jq -r '.status // "no answer"' <<<"$REPLY_BODY" 2>/dev/null || echo "no answer")"
    CALL_CODE="$(jq -r '.extras.error_code // ""' <<<"$REPLY_BODY" 2>/dev/null || true)"
    CALL_AUDIT="$(jq -r '.audit_id // ""' <<<"$REPLY_BODY" 2>/dev/null || true)"
    CALL_REQUEST="$(jq -r '.extras.approval_request_id // ""' <<<"$REPLY_BODY" 2>/dev/null || true)"
}

# preview STEP TOKEN_NAME OP_VAR: sets PREVIEW_HASH.
PREVIEW_HASH=""
preview() {
    local step="$1" token_name="$2" op_var="$3" body
    body="$(jq -nc --arg c "$(op_part "$op_var" 1)" --arg o "$(op_part "$op_var" 2)" \
        --arg t "$(op_part "$op_var" 3)" --argjson p "$(op_params "$op_var")" \
        '{connector_id:$c, op_id:$o, target:$t, params:$p}' 2>/dev/null || echo '{}')"
    rest "$step" POST /api/v1/operations/preview "$token_name" "$body"
    PREVIEW_HASH="$(jq -r '.preview_hash // ""' <<<"$REPLY_BODY" 2>/dev/null || true)"
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

ROW_ID=""; ROW_RULE=""; ROW_KIND=""; ROW_STATUS=""

read_row_body() {
    ROW_ID="$(jq -r '.id // ""' <<<"$1")"
    ROW_RULE="$(jq -r '.policy_rule // ""' <<<"$1")"
    ROW_KIND="$(jq -r '.caller_kind // ""' <<<"$1")"
    ROW_STATUS="$(jq -r '.result_status // ""' <<<"$1")"
}

# row_by_id STEP AUDIT_ID: read one audit row.
row_by_id() {
    ROW_ID=""; ROW_RULE=""; ROW_KIND=""; ROW_STATUS=""
    [[ -n "$2" || -n "$DRY_DIR" ]] || return 0
    rest "$1" GET "/api/v1/audit/show/$2" CPC_ADMIN_TOKEN
    [[ "$REPLY_CODE" == 200 ]] && read_row_body "$REPLY_BODY"
    return 0
}

# newest_row STEP OP_ID SINCE: the newest audit row of OP_ID since SINCE.
newest_row() {
    ROW_ID=""; ROW_RULE=""; ROW_KIND=""; ROW_STATUS=""
    local body
    body="$(jq -nc --arg o "$2" --arg s "$3" '{op_id:$o, since:$s, limit:1}')"
    rest "$1" POST /api/v1/audit/query CPC_ADMIN_TOKEN "$body"
    if [[ "$REPLY_CODE" == 200 ]]; then
        read_row_body "$(jq -c '.rows[0] // {}' <<<"$REPLY_BODY")"
    fi
    return 0
}

# rows_since STEP SINCE PART: the ids of the audit rows since SINCE whose path holds PART.
ROW_IDS=""
rows_since() {
    local body
    body="$(jq -nc --arg s "$2" '{since:$s, limit:1000}')"
    rest "$1" POST /api/v1/audit/query CPC_ADMIN_TOKEN "$body"
    ROW_IDS="$(jq -r --arg p "$3" '[.rows[]? | select(.path | contains($p)) | .id] | join(",")' \
        <<<"$REPLY_BODY" 2>/dev/null || true)"
}

# request_status STEP REQUEST_ID: the status of an approval request.
REQUEST_STATUS=""
request_status() {
    rest "$1" GET "/api/v1/approvals/$2" CPC_ADMIN_TOKEN
    REQUEST_STATUS="$(jq -r '.status // "unknown"' <<<"$REPLY_BODY" 2>/dev/null || echo unknown)"
}

# rule_ok RULE WANT: WANT ending in ":" matches a source; else the rule id itself.
# A rule marked "(shadow)" did not decide, so it never matches.
rule_ok() {
    local rule="$1" want="$2"
    [[ "$rule" == *"(shadow)"* ]] && return 1
    if [[ "$want" == *: ]]; then [[ "$rule" == "$want"* ]]; else [[ "$rule" == "$want" || "$rule" == "$want "* ]]; fi
}

# expect STEP CHECK WHO PATH WANT_VERDICT WANT_RULE [WANT_KIND]
# Reads the audit row of the last call_op and prints one line.
expect() {
    local step="$1" check="$2" who="$3" path="$4" want="$5" want_rule="$6" want_kind="${7:-}"
    local got problems=()
    got="$(verdict_of "$CALL_STATUS")"
    row_by_id "$step" "$CALL_AUDIT"
    [[ "$got" == "$want" ]] || problems+=("expected $want")
    [[ -n "$ROW_ID" ]] || problems+=("no audit row")
    if [[ -n "$want_rule" ]] && ! rule_ok "$ROW_RULE" "$want_rule"; then
        problems+=("expected rule $want_rule")
    fi
    if [[ -n "$want_kind" && "$ROW_KIND" != "$want_kind" ]]; then
        problems+=("expected caller kind $want_kind")
    fi
    local line
    line="check $check ($who, $path): $got${CALL_CODE:+ ($CALL_CODE)}, rule ${ROW_RULE:-none}, caller kind ${ROW_KIND:-none}, audit row ${ROW_ID:-none}"
    if [[ ${#problems[@]} -eq 0 ]]; then
        check_ok "$line"
    else
        local problem
        for problem in "${problems[@]}"; do line+="; $problem"; done
        check_fail "$line"
    fi
}

# expect_request CHECK WHO PATH REQUEST_ID WANT_STATUS SINCE
expect_request() {
    local check="$1" who="$2" path="$3" request="$4" want="$5" since="$6" step="$7"
    request_status "$step-request" "$request"
    rows_since "$step-rows" "$since" "$request"
    local line="check $check ($who, $path): request ${request:-none} is $REQUEST_STATUS, audit rows ${ROW_IDS:-none}"
    if [[ -n "$request" && "$REQUEST_STATUS" == "$want" ]]; then
        check_ok "$line"
    else
        check_fail "$line; expected $want"
    fi
}

# --- Check 7 probes: the answers that must be the same with the switch off --
#
# Each probe asks POST /api/v1/policy/explain, which runs nothing and writes
# no call row. Its own request row is the audit row of the probe.

PROBES=(
    "CPC_PERSON_TOKEN CPC_OP_LOWERED"
    "CPC_PERSON_TOKEN CPC_OP_RELEVELLED"
    "CPC_PERSON_TOKEN CPC_OP_ALWAYS_WAIT"
    "CPC_PERSON_TOKEN CPC_OP_AGENT_RULE"
    "CPC_SERVICE_TOKEN CPC_OP_AGENT_RULE"
    "CPC_SERVICE_TOKEN CPC_OP_APPROVER"
    "CPC_AGENT_TOKEN CPC_OP_CREDENTIAL_READ"
    "CPC_AGENT_TOKEN CPC_OP_CREDENTIAL_WRITE"
    "CPC_PERSON_TOKEN CPC_OP_RIGHTS_CHANGE"
    "CPC_AGENT_TOKEN CPC_OP_POLICY_CHANGE"
    "CPC_SERVICE_TOKEN CPC_OP_DESTRUCTIVE"
    "CPC_PERSON_TOKEN CPC_OP_DELETE"
    "CPC_DELETE_GRANT_TOKEN CPC_OP_DELETE"
    "CPC_PERSON_TOKEN CPC_OP_CALLER_ADDRESS"
    "CPC_SERVICE_TOKEN CPC_OP_ROOT_COMMAND"
    "CPC_PERSON_TOKEN CPC_OP_WRITE"
)

# probe STEP TOKEN_NAME OP_VAR: sets PROBE_ANSWER and PROBE_ROW.
PROBE_ANSWER=""; PROBE_ROW=""
probe() {
    local step="$1" token_name="$2" op_var="$3" body request_id
    request_id="$(od -An -N16 -tx1 /dev/urandom 2>/dev/null | tr -d ' \n')"
    body="$(jq -nc --arg c "$(op_part "$op_var" 1)" --arg o "$(op_part "$op_var" 2)" \
        --arg t "$(op_part "$op_var" 3)" '{connector_id:$c, op_id:$o, target:$t, me:true}')"
    if [[ -n "$DRY_DIR" ]]; then
        recorded "$step"
    else
        local out
        out="$(mktemp)"
        REPLY_CODE="$(curl -sS -o "$out" -w '%{http_code}' -X POST \
            -H 'Content-Type: application/json' -H "X-Request-Id: $request_id" \
            -H @<(printf 'Authorization: Bearer %s\n' "${!token_name}") \
            --data-binary "$body" "$BACKPLANE/api/v1/policy/explain")" || REPLY_CODE=000
        REPLY_BODY="$(jq -c . "$out" 2>/dev/null || echo null)"
        rm -f "$out"
    fi
    PROBE_ANSWER="$(jq -r '.answer // "no answer"' <<<"$REPLY_BODY" 2>/dev/null || echo "no answer")"
    # The probe's own request row: the one with the request id it sent.
    # Recorded answers have no request id to match, so a dry run takes the first row.
    [[ -n "$DRY_DIR" ]] && request_id=""
    rest "$step-row" POST /api/v1/audit/query CPC_ADMIN_TOKEN \
        "$(jq -nc --arg s "$PROBE_SINCE" '{since:$s, op_id:"policy.explain", limit:1000}')"
    PROBE_ROW="$(jq -r --arg r "$request_id" \
        '[.rows[]? | select($r == "" or ((.request_id // "") | gsub("-"; "")) == $r) | .id][0] // ""' \
        <<<"$REPLY_BODY" 2>/dev/null || true)"
}

probe_key() { printf '%s %s' "${1#CPC_}" "${2#CPC_OP_}"; }

record_baseline() {
    PROBE_SINCE="$(now_iso)"
    : >"$BASELINE_OUT"
    local i=0 entry
    for entry in "${PROBES[@]}"; do
        i=$((i + 1))
        read -r token_name op_var <<<"$entry"
        probe "baseline-$i" "$token_name" "$op_var"
        printf '%s\t%s\n' "$(probe_key "$token_name" "$op_var")" "$PROBE_ANSWER" >>"$BASELINE_OUT"
        note "baseline $(probe_key "$token_name" "$op_var"): $PROBE_ANSWER, audit row ${PROBE_ROW:-none}"
    done
}

# --- The checks --------------------------------------------------------------

check_1() {
    local since
    since="$(now_iso)"
    person "Check 1. As an Admin, in the web console, lower $(op_label CPC_OP_LOWERED) from dangerous to caution. Give a reason and sign in again when MEHO asks. Then, as an Operator in the web console, run $(op_label CPC_OP_LOWERED)."
    newest_row c1-row "$(op_part CPC_OP_LOWERED 2)" "$since"
    CALL_STATUS="$ROW_STATUS"; CALL_CODE=""; CALL_AUDIT="$ROW_ID"
    # Table row: "override", an admin's edit of the operation.
    expect c1-show 1 "an Operator, a person" "web console" runs "override:" person
}

check_2() {
    local since request
    call_op c2-call rest CPC_PERSON_TOKEN CPC_OP_RELEVELLED
    request="$CALL_REQUEST"
    # Table row: "floor", a token call runs only safe operations.
    expect c2-call-row 2 "an Operator's token, a personal agent" REST waits "floor:token_runs_only_safe" personal_agent
    since="$(now_iso)"
    person "Check 2. As a second Operator (not the one of CPC_PERSON_TOKEN), in the web console, click Approve on request ${request:-<none>}. MEHO must refuse it."
    expect_request 2 "a second Operator, a person" "web console" "$request" pending "$since" c2-operator
    since="$(now_iso)"
    person "Check 2. As an Admin, in the web console, approve request ${request:-<none>}."
    expect_request 2 "an Admin, a person" "web console" "$request" approved "$since" c2-admin
}

check_3() {
    call_op c3-nopreview rest CPC_PERSON_TOKEN CPC_OP_ALWAYS_WAIT
    local line
    line="check 3 (an Operator's token, a personal agent, REST): without a preview: $(verdict_of "$CALL_STATUS")${CALL_CODE:+ ($CALL_CODE)}, audit row ${CALL_AUDIT:-none}"
    if [[ "$CALL_STATUS" != ok && "$CALL_CODE" == preview_binding_required ]]; then
        check_ok "$line"
    else
        check_fail "$line; expected preview_binding_required"
    fi
    preview c3-preview CPC_PERSON_TOKEN CPC_OP_ALWAYS_WAIT
    call_op c3-call rest CPC_PERSON_TOKEN CPC_OP_ALWAYS_WAIT "$PREVIEW_HASH"
    # Table row: "floor", a destructive operation waits.
    expect c3-call-row 3 "an Operator's token, a personal agent" REST waits "floor:destructive_waits" personal_agent
}

check_4() {
    person "Check 4. As an Admin, in the web console, add a personal agent rule that lets the Operator's AI tool run $(op_label CPC_OP_AGENT_RULE) at once. Add no grant for the service login."
    call_op c4-agent mcp CPC_PERSON_TOKEN CPC_OP_AGENT_RULE
    # Table row: "tenant_rule", a rule of the tenant's agent policy.
    expect c4-agent-row 4 "an Operator's AI tool, a personal agent" MCP runs "tenant_rule:" personal_agent
    call_op c4-service rest CPC_SERVICE_TOKEN CPC_OP_AGENT_RULE
    # Table row: "default", the default for a service agent.
    expect c4-service-row 4 "a service login without a grant, a service agent" REST waits "default:" service_agent
}

check_5() {
    local since request
    person "Check 5. As an Admin, in the web console, make a custom role that approves requests on connector $(op_part CPC_OP_APPROVER 1), and give it to one person. Make sure a second person does not have the role."
    call_op c5-call rest CPC_SERVICE_TOKEN CPC_OP_APPROVER
    request="$CALL_REQUEST"
    expect c5-call-row 5 "a service login, a service agent" REST waits "" service_agent
    since="$(now_iso)"
    person "Check 5. As the person WITHOUT the custom role, in the web console, click Approve on request ${request:-<none>}. MEHO must refuse it at the click."
    expect_request 5 "a person without the role" "web console" "$request" pending "$since" c5-without
    since="$(now_iso)"
    person "Check 5. As the person WITH the custom role, in the web console, approve request ${request:-<none>}."
    expect_request 5 "a person with the custom role" "web console" "$request" approved "$since" c5-with
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

    call_op c6-policy mcp CPC_AGENT_TOKEN CPC_OP_POLICY_CHANGE
    # Table row: "floor", a change of the policy waits.
    expect c6-policy-row "6 change of the policy" "an agent login, a service agent" MCP waits "floor:policy_change_waits" service_agent

    preview c6-destructive-preview CPC_SERVICE_TOKEN CPC_OP_DESTRUCTIVE
    call_op c6-destructive rest CPC_SERVICE_TOKEN CPC_OP_DESTRUCTIVE "$PREVIEW_HASH"
    # Table row: "floor", a destructive operation waits.
    expect c6-destructive-row "6 destructive" "a service login without a grant, a service agent" REST waits "floor:destructive_waits" service_agent

    # The delete line names each caller (decision 5 of the record): in
    # sandbox mode a service login with a delete grant does not wait.
    call_op c6-delete-person rest CPC_PERSON_TOKEN CPC_OP_DELETE
    local delete_request="$CALL_REQUEST"
    # Table row: "floor", a delete waits.
    expect c6-delete-person-row "6 delete" "an Operator's token, a personal agent" REST waits "floor:delete_waits" personal_agent
    call_op c6-delete-agent mcp CPC_AGENT_TOKEN CPC_OP_DELETE
    expect c6-delete-agent-row "6 delete" "an agent login, a service agent" MCP waits "floor:delete_waits" service_agent
    call_op c6-delete-service rest CPC_SERVICE_TOKEN CPC_OP_DELETE
    expect c6-delete-service-row "6 delete" "a service login without a grant, a service agent" REST waits "floor:delete_waits" service_agent
    call_op c6-delete-grant rest CPC_DELETE_GRANT_TOKEN CPC_OP_DELETE
    # Table row: "standing_grant", the sandbox delete grant.
    expect c6-delete-grant-row "6 delete" "a service login with a delete grant, a service agent" REST runs "standing_grant:sandbox_delete_grant" service_agent

    person "Check 6. As an Admin, in the web console, lower $(op_label CPC_OP_DELETE) by one risk level, with a reason and a fresh sign-in."
    call_op c6-delete-lowered rest CPC_PERSON_TOKEN CPC_OP_DELETE
    expect c6-delete-lowered-row "6 delete after a lowering" "an Operator's token, a personal agent" REST waits "floor:delete_waits" personal_agent

    call_op c6-address mcp CPC_PERSON_TOKEN CPC_OP_CALLER_ADDRESS
    # Table row: "floor", a call to an address the caller gives waits.
    expect c6-address-row "6 caller address" "an Operator's AI tool, a personal agent" MCP waits "floor:caller_address_waits" personal_agent

    call_op c6-root rest CPC_SERVICE_TOKEN CPC_OP_ROOT_COMMAND
    # Table row: "floor", a root command on a host waits.
    expect c6-root-row "6 root command" "a service login without a grant, a service agent" REST waits "floor:root_command_waits" service_agent

    local since
    since="$(now_iso)"
    rest c6-agent-approve POST "/api/v1/approvals/${delete_request:-none}/approve" CPC_AGENT_TOKEN '{"params":{}}'
    local code="$REPLY_CODE"
    request_status c6-agent-approve-request "$delete_request"
    rows_since c6-agent-approve-rows "$since" "$delete_request"
    # Table row: "floor", only a person approves (only_a_person_approves).
    local line="check 6 agent approve (an agent login, a service agent, REST): HTTP $code, request ${delete_request:-none} is $REQUEST_STATUS, audit rows ${ROW_IDS:-none}"
    if [[ -n "$delete_request" && "$code" =~ ^4 && "$REQUEST_STATUS" == pending ]]; then
        check_ok "$line"
    else
        check_fail "$line; expected a refusal and a request that still waits"
    fi
}

check_8() {
    local since
    since="$(now_iso)"
    person "Check 8. As an Admin, in the web console, try to lower $(op_label CPC_OP_WRITE) (a write) to safe, with a reason and a fresh sign-in."
    ask c8-console "Did MEHO refuse the lowering to safe in the web console?"
    rows_since c8-console-rows "$since" "$(op_part CPC_OP_WRITE 2)"
    local line="check 8 (an Admin, web console): refused: $ANSWER, audit rows ${ROW_IDS:-none}"
    if [[ "$ANSWER" == yes ]]; then check_ok "$line"; else check_fail "$line; expected a refusal"; fi

    local file
    file="$(printf 'policy: v1\nclassifications:\n  - match: {connector: "%s", op: "%s"}\n    risk: safe\n' \
        "$(op_part CPC_OP_WRITE 1)" "$(op_part CPC_OP_WRITE 2)")"
    since="$(now_iso)"
    rest c8-token POST "/api/v1/policy/import?preview=true" CPC_ADMIN_TOKEN "$file" application/yaml
    local code="$REPLY_CODE"
    rows_since c8-token-rows "$since" "/api/v1/policy/import"
    line="check 8 (an Admin's token, REST, a policy file preview): HTTP $code, audit rows ${ROW_IDS:-none}"
    if [[ "$code" =~ ^4 ]]; then check_ok "$line"; else check_fail "$line; expected a refusal"; fi
}

check_9() {
    local since
    since="$(now_iso)"
    person "Check 9. As an Admin whose sign-in is older than the fresh sign-in time, in the web console: raise $(op_label CPC_OP_WRITE) to dangerous, then remove that override again."
    ask c9-fresh "Did MEHO ask for a fresh sign-in before the raise AND before the removal?"
    rows_since c9-rows "$since" "override"
    local line="check 9 (an Admin, web console): fresh sign-in asked each time: $ANSWER, audit rows ${ROW_IDS:-none}"
    if [[ "$ANSWER" == yes ]]; then check_ok "$line"; else check_fail "$line; expected a fresh sign-in each time"; fi
}

check_10() {
    local since request_web request_cli
    call_op c10-web mcp CPC_PERSON_TOKEN CPC_OP_OWN_AGENT
    request_web="$CALL_REQUEST"
    expect c10-web-row 10 "the Operator's AI tool, a personal agent" MCP waits "" personal_agent
    since="$(now_iso)"
    person "Check 10. As the same Operator, in the web console, approve your own request ${request_web:-<none>}."
    expect_request 10 "the Operator, a person" "web console" "$request_web" approved "$since" c10-web-approve

    call_op c10-cli mcp CPC_PERSON_TOKEN CPC_OP_OWN_AGENT
    request_cli="$CALL_REQUEST"
    since="$(now_iso)"
    note "check 10: the meho command line now approves ${request_cli:-<none>}; sign in when the browser asks."
    cli c10-cli-approve approvals approve "${request_cli:-none}"
    local code="$REPLY_CODE"
    request_status c10-cli-request "$request_cli"
    rows_since c10-cli-rows "$since" "$request_cli"
    local line="check 10 (the Operator, command line): exit $code, request ${request_cli:-none} is $REQUEST_STATUS, audit rows ${ROW_IDS:-none}"
    if [[ -n "$request_cli" && "$code" != 0 && "$REQUEST_STATUS" == pending ]]; then
        check_ok "$line"
    else
        check_fail "$line; expected a refusal and a request that still waits"
    fi
}

check_11() {
    local since tries=0
    since="$(now_iso)"
    rest c11-run POST "/api/v1/sensors/${CPC_SENSOR_ID:-none}/run" CPC_ADMIN_TOKEN '{}'
    local code="$REPLY_CODE"
    while :; do
        newest_row c11-row "$(op_part CPC_OP_SENSOR 2)" "$since"
        [[ -n "$ROW_ID" || -n "$DRY_DIR" || $tries -ge 24 ]] && break
        tries=$((tries + 1))
        sleep 5
    done
    local line="check 11 (the check runner, a sensor run started over REST): HTTP $code, caller kind ${ROW_KIND:-none}, rule ${ROW_RULE:-none}, audit row ${ROW_ID:-none}"
    if [[ "$code" =~ ^2 && -n "$ROW_ID" && "$ROW_KIND" == system ]]; then
        check_ok "$line"
    else
        check_fail "$line; expected an audit row with caller kind system"
    fi
}

check_7() {
    person "Check 7. Turn the switch CUSTOM_POLICY off on the install and restart MEHO. Wait until /ready answers again."
    PROBE_SINCE="$(now_iso)"
    local baseline="${CPC_BASELINE:-$DRY_DIR/baseline.tsv}"
    if [[ ! -f "$baseline" ]]; then
        check_fail "check 7: no baseline file; run --record-baseline before the switch is turned on"
        return
    fi
    local i=0 entry key before
    for entry in "${PROBES[@]}"; do
        i=$((i + 1))
        read -r token_name op_var <<<"$entry"
        key="$(probe_key "$token_name" "$op_var")"
        before="$(awk -F'\t' -v k="$key" '$1 == k {print $2}' "$baseline")"
        probe "c7-$i" "$token_name" "$op_var"
        local line="check 7 ($key, REST policy explain): $PROBE_ANSWER, before ${before:-none}, audit row ${PROBE_ROW:-none}"
        if [[ -n "$before" && "$PROBE_ANSWER" == "$before" ]]; then check_ok "$line"; else check_fail "$line; expected the answer of before"; fi
    done
}

# --- Main ----------------------------------------------------------------------

if [[ -n "$DRY_DIR" ]]; then
    recorded ready
    READY="$REPLY_BODY"
else
    READY="$(curl -sS "$BACKPLANE/ready" 2>/dev/null || echo null)"
fi
SANDBOX="$(jq -r '.sandbox_mode // "unknown"' <<<"$READY" 2>/dev/null || echo unknown)"
if [[ "$SANDBOX" != on ]]; then
    echo "custom-policy-check.sh: refused: the install is not in sandbox mode (sandbox_mode: $SANDBOX). Run it only on a sandbox install." >&2
    exit 2
fi
note "the install is in sandbox mode"

if [[ -n "$BASELINE_OUT" ]]; then
    record_baseline
    note "baseline saved; now turn the switch CUSTOM_POLICY on and run the checks"
    exit 0
fi

person "Before the checks: the switch CUSTOM_POLICY is on, and MEHO was restarted with it."
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
check_7

echo
echo "custom-policy-check.sh: $PASS_COUNT passed, $FAIL_COUNT failed"
[[ $FAIL_COUNT -eq 0 ]] || exit 1
