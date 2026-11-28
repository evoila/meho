#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# Decides, in the `changes` job of ci.yml, whether unit test shards 4-6 run
# on the second runner site. Writes `second_site=on` to $GITHUB_OUTPUT, or
# `second_site=` for today's pool, and one notice that says why.
#
# Every doubt means today's pool, and this script never fails the job.
#
# Inputs (environment):
#   MEHO_CI_SECOND_SITE  the repository variable; only `on` switches it on
#   EVENT_NAME           github.event_name
#   HEAD_REPO            github.event.pull_request.head.repo.full_name
#   ACTOR                github.actor
#   COVERAGE_LABEL       `false` when the pull request does not have the label
#                        ci-coverage-validate; any other value keeps today's pool
#   RUN_ATTEMPT          github.run_attempt
#   DOCS_ONLY, BACKEND_PYTHON, SINGLE_CONNECTOR, TEST_MAP
#                        the outputs of the `changes` job; the site is used
#                        only when all six shards really run
#   GITHUB_REPOSITORY, GITHUB_OUTPUT, GH_TOKEN  set by the runner
#
# How it knows the site is online: the job token cannot list self-hosted
# runners (GitHub answers 403), so it reads the run history of the smoke
# workflow, which runs on the site twice an hour. A smoke run that passed
# proves that a runner of the site really took a job, when its smoke job
# passed too (see below). GitHub writes that history itself, so the site has
# nothing to upload.
#
# run_started_at, not updated_at: a run can only look older than it is, never
# newer. A smoke run that waited long for a runner counts as old, and then
# the shards stay on today's pool.
set -uo pipefail

smoke_workflow="second-site-smoke.yml"
# The `name:` of the smoke job in that workflow.
smoke_job="Smoke check of the second runner site"
max_age_seconds=$((45 * 60))

decide() {
  # $1 = `on` or empty. $2 = why, for the log.
  echo "second_site=${1}" >> "${GITHUB_OUTPUT}"
  echo "::notice title=Unit shards 4-6::${2}"
  exit 0
}

if [[ "${MEHO_CI_SECOND_SITE:-}" != "on" ]]; then
  decide "" "today's pool: the second runner site is not switched on"
fi
if [[ "${EVENT_NAME:-}" != "pull_request" ]]; then
  decide "" "today's pool: only a pull request run may use the second site (this is ${EVENT_NAME:-an unknown event})"
fi
if [[ -z "${HEAD_REPO:-}" || "${HEAD_REPO}" != "${GITHUB_REPOSITORY:-}" ]]; then
  decide "" "today's pool: a pull request from a fork never uses the second site"
fi
# The coverage job joins the coverage files of all shards and works in the
# checked-out code, so with this label every shard stays on today's pool.
if [[ "${COVERAGE_LABEL:-}" != "false" ]]; then
  decide "" "today's pool: a pull request with the label ci-coverage-validate keeps all unit shards on today's pool"
fi
# The token of a Dependabot run cannot cancel a run, so the second-site
# watchdog could not stop shards that wait on a dead site.
if [[ "${ACTOR:-}" == "dependabot[bot]" ]]; then
  decide "" "today's pool: a Dependabot run stays on today's pool"
fi
if [[ "${RUN_ATTEMPT:-}" != "1" ]]; then
  decide "" "today's pool: a re-run always uses today's pool"
fi
if [[ "${DOCS_ONLY:-}" != "false" || "${BACKEND_PYTHON:-}" != "true" \
  || -n "${SINGLE_CONNECTOR:-}" || "${TEST_MAP:-}" == "true" ]]; then
  decide "" "today's pool: this run does not run all six unit shards"
fi

if ! runs="$(gh api "repos/${GITHUB_REPOSITORY}/actions/workflows/${smoke_workflow}/runs?branch=main&status=success&per_page=10" 2>/dev/null)"; then
  decide "" "today's pool: could not read the smoke runs"
fi

# Prints the id, the attempt and the age in seconds of the newest smoke run
# that passed on main of this repository. jq's `max_by` so the order of the
# answer does not matter. Both names must be this repository: two missing
# names are equal in jq, so comparing them only to each other proves nothing.
newest="$(jq -er --arg repo "${GITHUB_REPOSITORY:-}" '
  [.workflow_runs[]
   | select(.conclusion == "success" and .head_branch == "main"
            and $repo != ""
            and .head_repository.full_name == $repo
            and .repository.full_name == $repo)
   | {id, attempt: .run_attempt, started: (.run_started_at | fromdateiso8601)}]
  | max_by(.started) // empty
  | "\(.id) \(.attempt) \(now - .started | floor)"' <<<"${runs}" 2>/dev/null)"
read -r run_id run_attempt age <<<"${newest}"

# Also catches an unreadable answer, no run at all and a time in the future.
if [[ ! "${run_id:-}" =~ ^[0-9]+$ || ! "${run_attempt:-}" =~ ^[0-9]+$ \
  || ! "${age:-}" =~ ^[0-9]+$ ]]; then
  decide "" "today's pool: no smoke run passed that this job can use"
fi
if (( age > max_age_seconds )); then
  decide "" "today's pool: the newest smoke pass is ${age} s old (limit ${max_age_seconds} s)"
fi

# A run also counts as passed when its smoke job was skipped (the switch job
# found the variable not exactly `on`). Only a passed smoke job in that same
# attempt proves that a runner of the site took a job.
if ! jobs="$(gh api "repos/${GITHUB_REPOSITORY}/actions/runs/${run_id}/attempts/${run_attempt}/jobs?per_page=100" 2>/dev/null)"; then
  decide "" "today's pool: could not read the jobs of the newest smoke pass"
fi
if ! jq -e --arg name "${smoke_job}" \
  'any(.jobs[]; .name == $name and .conclusion == "success")' \
  <<<"${jobs}" >/dev/null 2>&1; then
  decide "" "today's pool: the smoke job did not pass in the newest smoke pass"
fi
decide "on" "shards 4-6 use the second runner site: a smoke check passed there ${age} s ago"
