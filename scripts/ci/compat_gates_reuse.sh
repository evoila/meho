#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# Decides whether the compatibility check of a pull request runs again, or
# copies the result it already has for the same commit (#7105).
# .github/workflows/compat-gates.yml runs it on a GitHub-hosted runner.
#
# The check reads the code, the `breaking-change` label and the pull request
# text. So the result can be copied only when none of them changed:
#   - `labeled` / `unlabeled` with a label other than `breaking-change`;
#   - `edited` where only the title changed (not the text, not the base).
# Every other event runs the full check.
#
# The result to copy is the compatibility job of the newest earlier run of
# this workflow for the same commit and pull request. When that job is still
# running, the script waits for it. It copies only `success` or `failure`.
# A cancelled or skipped job, no earlier run, a run that does not finish in
# time, and every failed read give the full check. The script itself never
# fails.
#
# Writes `reuse=true|false` and `conclusion=success|failure|` to
# $GITHUB_OUTPUT. Needs gh (logged in through GH_TOKEN), jq, and:
#   GITHUB_REPOSITORY, GITHUB_RUN_ID, GITHUB_OUTPUT
#   EVENT_ACTION   the action of the pull_request event
#   LABEL_NAME     the label of a labeled / unlabeled event
#   CHANGES_JSON   `changes` of an edited event, as JSON
#   HEAD_SHA       the head commit of the pull request
#   PR_NUMBER      the number of the pull request
#   CHECK_NAME     the `name:` of the compatibility job
#   WORKFLOW_FILE  the file name of this workflow
#   POLLS, POLL_SECONDS  how long to wait for an open earlier run
set -uo pipefail

polls="${POLLS:-10}"
poll_seconds="${POLL_SECONDS:-30}"
api="repos/${GITHUB_REPOSITORY}"

finish() {
  {
    echo "reuse=${1}"
    echo "conclusion=${2}"
  } >>"${GITHUB_OUTPUT}"
  echo "::notice title=Compatibility check::${3}"
  exit 0
}

full() {
  finish false "" "Runs the full check: ${1}."
}

case "${EVENT_ACTION:-}" in
  labeled | unlabeled)
    if [ -z "${LABEL_NAME:-}" ] || [ "${LABEL_NAME}" = "breaking-change" ]; then
      full "the label breaking-change was ${EVENT_ACTION}"
    fi
    ;;
  edited)
    if ! other="$(jq -er 'keys - ["title"] | join(",")' <<<"${CHANGES_JSON:-}" 2>/dev/null)"; then
      full "the changes of the edit could not be read"
    fi
    if [ -n "${other}" ]; then
      full "the edit changed: ${other}"
    fi
    ;;
  *)
    full "the event '${EVENT_ACTION:-}' can change the result"
    ;;
esac

case "${HEAD_SHA:-}" in
  '' | *[!0-9a-f]*) full "no head commit" ;;
esac
case "${PR_NUMBER:-}" in
  '' | *[!0-9]*) full "no pull request number" ;;
esac

if ! runs="$(gh api "${api}/actions/workflows/${WORKFLOW_FILE}/runs?head_sha=${HEAD_SHA}&event=pull_request&per_page=100")"; then
  full "could not list the earlier runs on this commit"
fi
# The newest run before this one, of this pull request. A fork pull request
# lists no pull request on its runs, so such a run counts too.
if ! earlier="$(jq -er --argjson me "${GITHUB_RUN_ID}" --argjson pr "${PR_NUMBER}" \
    '[.workflow_runs[] | select(.id < $me)
      | select((.pull_requests | length) == 0 or any(.pull_requests[]; .number == $pr))]
     | sort_by(.id) | last | .id // ""' <<<"${runs}")"; then
  full "could not read the earlier runs on this commit"
fi
if [ -z "${earlier}" ]; then
  full "there is no earlier run on this commit"
fi

poll=1
while :; do
  if ! jobs="$(gh api "${api}/actions/runs/${earlier}/jobs?per_page=100")"; then
    full "could not read the jobs of run ${earlier}"
  fi
  if ! state="$(jq -er --arg name "${CHECK_NAME}" \
      '[.jobs[] | select(.name == $name)]
       | if length != 1 then "none"
         elif .[0].status == "completed" then (.[0].conclusion // "none")
         else "open" end' <<<"${jobs}")"; then
    full "could not read the jobs of run ${earlier}"
  fi
  if [ "${state}" != "open" ] || [ "${poll}" -ge "${polls}" ]; then
    break
  fi
  echo "run ${earlier} still checks this commit; looking again in ${poll_seconds} s"
  poll=$((poll + 1))
  sleep "${poll_seconds}"
done

case "${state}" in
  success | failure)
    finish true "${state}" "Copies the result '${state}' of run ${earlier} on the same commit: the ${EVENT_ACTION} event does not change it."
    ;;
  open) full "run ${earlier} did not finish in time" ;;
  *) full "run ${earlier} has no finished result to copy (${state})" ;;
esac
