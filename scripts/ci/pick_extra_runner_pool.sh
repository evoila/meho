#!/usr/bin/env bash
# Decides, in the `changes` job of ci.yml, whether unit test shards 4-6 may
# run on the second runner pool. Writes `runner=<pool name>` to
# $GITHUB_OUTPUT, or `runner=` for today's pool.
#
# Every doubt means today's pool, and this script never fails the job.
#
# How it knows the pool is online: the job token cannot list self-hosted
# runners (GitHub answers 403), so it reads the run history of the smoke
# workflow, which runs on the pool every 30 minutes. A smoke run that passed
# proves that a runner of the pool really took a job. GitHub writes that
# history itself, so the pool has nothing to upload.
#
# run_started_at, not updated_at: a run can only look older than it is, never
# newer. A smoke run that waited long for a runner counts as old, and then
# the shards stay on today's pool.
set -uo pipefail

pool="meho-runners-ci-heavy-extra"
smoke_workflow="extra-runner-smoke.yml"
max_age_seconds=$((45 * 60))

decide() {
  # $1 = the pool name, or empty for today's pool. $2 = why, for the log.
  echo "runner=${1}" >> "${GITHUB_OUTPUT}"
  echo "::notice title=Unit shards 4-6::${2}"
  exit 0
}

if [[ "${MEHO_CI_EXTRA_POOL:-}" != "on" ]]; then
  decide "" "today's pool: the second runner pool is not switched on"
fi
if [[ "${EVENT_NAME:-}" == "merge_group" ]]; then
  decide "" "today's pool: the merge queue keeps its own runners"
fi
if [[ "${EVENT_NAME:-}" == "pull_request" && "${HEAD_REPO:-}" != "${GITHUB_REPOSITORY}" ]]; then
  decide "" "today's pool: a pull request from a fork never uses the second pool"
fi
# The token of a Dependabot run cannot cancel a run, so the watchdog could
# not stop shards that wait on a dead pool.
if [[ "${GITHUB_ACTOR:-}" == "dependabot[bot]" ]]; then
  decide "" "today's pool: a Dependabot run stays on today's pool"
fi

if ! runs="$(gh api "repos/${GITHUB_REPOSITORY}/actions/workflows/${smoke_workflow}/runs?branch=main&status=success&per_page=10" 2>/dev/null)"; then
  decide "" "today's pool: could not read the smoke runs"
fi

# Prints the age in seconds of the newest smoke run that passed on main of
# this repository. jq's `max` so the order of the answer does not matter.
age="$(jq -er '
  [.workflow_runs[]
   | select(.conclusion == "success" and .head_repository.full_name == .repository.full_name)
   | .run_started_at | fromdateiso8601]
  | max // empty
  | now - . | floor' <<<"${runs}" 2>/dev/null)"

# Also catches an unreadable answer, no run at all and a time in the future.
if [[ ! "${age}" =~ ^[0-9]+$ ]]; then
  decide "" "today's pool: no smoke run passed that this job can use"
fi
if (( age > max_age_seconds )); then
  decide "" "today's pool: the newest smoke pass is ${age} s old (limit ${max_age_seconds} s)"
fi
decide "${pool}" "shards 4-6 use ${pool}: a smoke check passed there ${age} s ago"
