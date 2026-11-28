#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# Decides, in the `changes` job of ci.yml, whether unit test shards 1-3 of a
# pull request run on GitHub-hosted runners (the job python-unit-shard-hosted).
# Writes `pr_hosted=on` to $GITHUB_OUTPUT, or `pr_hosted=` for today's pool,
# and one notice that says why.
#
# Every doubt means today's pool, and this script never fails the job.
#
# Inputs (environment):
#   MEHO_CI_PR_HOSTED    the repository variable; only `on` switches it on
#   EVENT_NAME           github.event_name
#   HEAD_REPO            github.event.pull_request.head.repo.full_name
#   RUN_ATTEMPT          github.run_attempt
#   DOCS_ONLY, BACKEND_PYTHON, SINGLE_CONNECTOR, TEST_MAP
#                        the outputs of the `changes` job; the hosted runners
#                        are used only when all six shards really run
#   GITHUB_REPOSITORY, GITHUB_OUTPUT  set by the runner
set -uo pipefail

decide() {
  # $1 = `on` or empty. $2 = why, for the log.
  echo "pr_hosted=${1}" >> "${GITHUB_OUTPUT}"
  echo "::notice title=Unit shards 1-3::${2}"
  exit 0
}

if [[ "${MEHO_CI_PR_HOSTED:-}" != "on" ]]; then
  decide "" "today's pool: GitHub-hosted runners for pull requests are not switched on"
fi
if [[ "${EVENT_NAME:-}" != "pull_request" ]]; then
  decide "" "today's pool: only a pull request run may use GitHub-hosted runners for the unit shards (this is ${EVENT_NAME:-an unknown event})"
fi
if [[ -z "${HEAD_REPO:-}" || "${HEAD_REPO}" != "${GITHUB_REPOSITORY:-}" ]]; then
  decide "" "today's pool: a pull request from a fork keeps today's rule"
fi
if [[ "${RUN_ATTEMPT:-}" != "1" ]]; then
  decide "" "today's pool: a re-run always uses today's pool"
fi
if [[ "${DOCS_ONLY:-}" != "false" || "${BACKEND_PYTHON:-}" != "true" \
  || -n "${SINGLE_CONNECTOR:-}" || "${TEST_MAP:-}" == "true" ]]; then
  decide "" "today's pool: this run does not run all six unit shards"
fi
decide "on" "shards 1-3 use GitHub-hosted runners (ubuntu-latest)"
