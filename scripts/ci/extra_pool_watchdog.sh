#!/usr/bin/env bash
# Stops a CI run when unit test shards on the second runner pool cannot get a
# runner. The pool can die after its last good smoke check. A job with no
# runner is not stopped by `timeout-minutes`, GitHub gives up on it only after
# 24 hours, and the required check would wait with it.
#
# After the limit, a shard that still waits stops the whole run, so the check
# goes red instead of hanging. A re-run uses today's pool (see `runs-on` of
# python-unit-shard). GitHub has no way to stop one job in a run, only the run.
#
# A very full pool looks the same as a dead one from here. That is fine: a
# shard that waits this long is better off on today's pool anyway.
set -uo pipefail

interval_seconds=60
retry_seconds=15
last_tries=3
limit_minutes=30
polls_left=$(( limit_minutes * 60 / interval_seconds ))
expected=3

api="repos/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}/attempts/${GITHUB_RUN_ATTEMPT}/jobs?per_page=100"

# Prints "<found> <waiting>" for shards 4-6. A job that is in_progress or
# completed has a runner; every other status is still waiting for one.
read_shards() {
  local jobs
  jobs="$(gh api "${api}" 2>/dev/null)" || return 1
  jq -er '
    [.jobs[] | select(.name | test("^Python unit shard [456]/6$"))]
    | "\(length) \(map(select(.status != "in_progress" and .status != "completed")) | length)"
  ' <<<"${jobs}" 2>/dev/null
}

while (( polls_left > 0 )); do
  polls_left=$(( polls_left - 1 ))
  if result="$(read_shards)"; then
    read -r found waiting_now <<<"${result}"
    if [[ "${found}" -eq "${expected}" && "${waiting_now}" -eq 0 ]]; then
      echo "Every unit shard on the second pool has a runner. Nothing left to watch."
      exit 0
    fi
    echo "shards found: ${found}/${expected}, still waiting for a runner: ${waiting_now}"
  else
    echo "could not read this run's jobs; looking again next time"
  fi
  sleep "${interval_seconds}"
done

# What the loop saw is at least a minute old, and a shard may have started
# since. So every cancel comes right after a fresh read, and a read that fails
# never leads to a cancel.
for (( try = 1; try <= last_tries; try++ )); do
  if result="$(read_shards)"; then
    read -r found waiting_now <<<"${result}"
    # A shard that is not listed is not known to be stuck.
    if [[ "${found}" -ne "${expected}" ]]; then
      echo "Stopped watching after ${limit_minutes} minutes: no shard is known to be stuck, so the run goes on."
      exit 0
    fi
    if [[ "${waiting_now}" -eq 0 ]]; then
      echo "Every unit shard on the second pool has a runner now. Nothing to stop."
      exit 0
    fi
    echo "::error title=Second runner pool::${waiting_now} unit shard(s) waited ${limit_minutes} minutes for a runner of the second pool, so this run is stopped. Re-run it: a re-run uses today's runners. If this repeats, set the repository variable MEHO_CI_EXTRA_POOL to off."
    if gh run cancel "${GITHUB_RUN_ID}" --repo "${GITHUB_REPOSITORY}" >/dev/null 2>&1; then
      echo "Cancel request accepted."
      exit 0
    fi
    echo "the cancel request failed"
  else
    echo "could not read this run's jobs before stopping it"
  fi
  if (( try < last_tries )); then
    sleep "${retry_seconds}"
  fi
done
echo "::error title=Second runner pool::Could not stop this run, or could not read its jobs. Check it, and cancel it by hand if shards still wait."
exit 1
