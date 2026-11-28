#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# Stops a CI run when unit test shards on the second runner site cannot get a
# runner because the site is down. The site can die after its last good smoke
# check. A job with no runner is not stopped by `timeout-minutes`, GitHub gives
# up on it only after 24 hours, and the required check would wait with it.
#
# After the limit, a shard that still waits stops the whole run only when the
# site also started no job at all in the last limit minutes, in any run of the
# workflows that use it. Then it is down. When it did start a job, it is busy:
# the watchdog keeps waiting, at most for the time limit of the shard job. A
# cancelled run must run again in full, which costs more than the wait. When
# the jobs of the other runs cannot be read, the run is stopped after the
# limit, as before.
#
# A stopped run goes red instead of hanging. A re-run uses today's pool (see
# `runs-on` of python-unit-shard-second-site). GitHub has no way to stop one
# job in a run, only the run.
set -uo pipefail

interval_seconds=60
retry_seconds=15
last_tries=3
limit_minutes=30
polls_left=$(( limit_minutes * 60 / interval_seconds ))
# The time limit of python-unit-shard-second-site. A shard on a busy site
# waits at most this much longer.
busy_minutes=35
busy_polls_left=$(( busy_minutes * 60 / interval_seconds ))
expected=3
# The runner label of the second site, and the only workflows that name it.
site_label="meho-runners-ci-heavy-site2"
site_workflows=(ci.yml second-site-smoke.yml)
# How long ago a run of a workflow can have been created and still hold a job
# that a runner of the site started in the last limit minutes. GitHub lets a
# person re-run a run up to 30 days after it was created, and a re-run of the
# smoke check uses the site again. A re-run of ci.yml uses today's pool (see
# `runs-on` of python-unit-shard-second-site), so its first attempt is the
# only one on the site, and that started a few hours after the run at most.
lookback_hours() {
  case "$1" in
    ci.yml) echo 3 ;;
    *) echo $(( 30 * 24 )) ;;
  esac
}

api="repos/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}/attempts/${GITHUB_RUN_ATTEMPT}/jobs?per_page=100"

# Prints "<found> <waiting> <started>" for shards 4-6 (the job
# python-unit-shard-second-site names them like the other shards). A job that
# is in_progress or completed has started on a runner. A job that is queued,
# waiting, requested or pending still waits for one. Any other status, or
# none, counts as neither: an incomplete answer proves nothing.
read_shards() {
  local jobs
  jobs="$(gh api "${api}" 2>/dev/null)" || return 1
  jq -er '
    [.jobs[] | select(.name | test("^Python unit shard [456]/6$"))]
    | [length,
       (map(select(.status == "queued" or .status == "waiting"
                   or .status == "requested" or .status == "pending")) | length),
       (map(select(.status == "in_progress" or .status == "completed")) | length)]
    | map(tostring) | join(" ")
  ' <<<"${jobs}" 2>/dev/null
}

while (( polls_left > 0 )); do
  polls_left=$(( polls_left - 1 ))
  if result="$(read_shards)"; then
    read -r found waiting_now started <<<"${result}"
    if [[ "${found}" -eq "${expected}" && "${started}" -eq "${expected}" ]]; then
      echo "Every unit shard on the second site has a runner. Nothing left to watch."
      exit 0
    fi
    echo "shards found: ${found}/${expected}, started: ${started}, still waiting for a runner: ${waiting_now}"
  else
    echo "could not read this run's jobs; looking again next time"
  fi
  sleep "${interval_seconds}"
done

# Times from the API are like 2026-11-28T09:11:09Z, maybe with a fraction.
jq_defs='def epoch: sub("\\.[0-9]+Z$"; "Z") | fromdateiso8601;'

# Prints the start time (seconds since 1970) of a job that a runner of the
# second site started in the last limit minutes, in any run of the workflows
# that use the site. Prints nothing when there is none. Returns 1 when a read
# fails or its answer is odd. Stops at the first run with such a job, so a
# busy site costs few API calls.
site_last_start() {
  local window=$(( limit_minutes * 60 )) created workflow runs ids run jobs newest
  for workflow in "${site_workflows[@]}"; do
    created="$(jq -nr --argjson s "$(( $(lookback_hours "${workflow}") * 3600 ))" \
      'now - $s | strftime("%Y-%m-%dT%H:%M:%SZ")')" || return 1
    runs="$(gh api --paginate \
      "repos/${GITHUB_REPOSITORY}/actions/workflows/${workflow}/runs?per_page=100&created=%3E%3D${created}" \
      2>/dev/null)" || return 1
    # A run that ended before the window cannot hold a job that started in it.
    # A re-run moves `updated_at` of an old run.
    ids="$(jq -sr --argjson w "${window}" "${jq_defs}"'
      .[].workflow_runs[]
      | select(.status != "completed" or (.updated_at | epoch) >= now - $w)
      | .id' <<<"${runs}" 2>/dev/null)" || return 1
    for run in ${ids}; do
      # `filter=all` reads every attempt: a site job of the first attempt
      # counts also when a later attempt ran on today's pool.
      jobs="$(gh api --paginate \
        "repos/${GITHUB_REPOSITORY}/actions/runs/${run}/jobs?per_page=100&filter=all" \
        2>/dev/null)" || return 1
      # A queued job has a start time too, but no runner name yet.
      newest="$(jq -sr --arg label "${site_label}" --argjson w "${window}" "${jq_defs}"'
        [.[].jobs[]
         | select((.labels // []) | any(. == $label))
         | select(.status == "in_progress" or .status == "completed")
         | select((.runner_name // "") != "")
         | .started_at | epoch | select(. >= now - $w)]
        | max // empty | floor' <<<"${jobs}" 2>/dev/null)" || return 1
      if [[ -n "${newest}" ]]; then
        printf '%s\n' "${newest}"
        return 0
      fi
    done
  done
}

# Prints the reason to stop the run in $reason and returns 0, or returns 1
# when the shards should wait more because the site is busy.
busy_until=0
decide() {
  local now newest
  now="$(date +%s)"
  if (( busy_polls_left <= 0 )); then
    reason="${waiting_now} unit shard(s) waited $(( limit_minutes + busy_minutes )) minutes for a runner of the second site. The site is busy, not down, but this is longer than the time limit of the shard, so this run is stopped."
    return 0
  fi
  if (( now < busy_until )); then
    echo "${waiting_now} unit shard(s) still wait; the second site is busy, so waiting more"
    return 1
  fi
  if ! newest="$(site_last_start)"; then
    reason="${waiting_now} unit shard(s) waited ${limit_minutes} minutes for a runner of the second site, and the jobs of the other runs could not be read, so this run is stopped."
    return 0
  fi
  if [[ -z "${newest}" ]]; then
    reason="${waiting_now} unit shard(s) waited ${limit_minutes} minutes for a runner of the second site, and the site started no job in that time, so it looks down and this run is stopped."
    return 0
  fi
  busy_until=$(( newest + limit_minutes * 60 ))
  echo "::notice title=Second runner site::${waiting_now} unit shard(s) waited ${limit_minutes} minutes or more for a runner of the second site, but the site started a job $(( (now - newest) / 60 )) minutes ago. It is busy, not down, so the watchdog keeps waiting, at most ${busy_minutes} minutes more."
  return 1
}

# What the loop saw is at least a minute old, and a shard may have started
# since. The reads of the site take time too. So once the site looks down (or
# its jobs cannot be read, or the busy wait is over), this run is read once
# more, and the cancel comes right after that read, only when it shows at
# least one shard that still waits. A read of this run that fails, or that
# shows no waiting shard but not every shard started, never leads to a
# cancel: it is read again.
try=1
stop_reason=""
while (( try <= last_tries )); do
  if ! result="$(read_shards)"; then
    echo "could not read this run's jobs before stopping it"
  else
    read -r found waiting_now started <<<"${result}"
    if [[ "${waiting_now}" -gt 0 ]]; then
      if [[ -z "${stop_reason}" ]]; then
        if ! decide; then
          busy_polls_left=$(( busy_polls_left - 1 ))
          try=1
          sleep "${interval_seconds}"
          continue
        fi
        stop_reason="${reason}"
        continue
      fi
      echo "::error title=Second runner site::${stop_reason} Re-run it: a re-run uses today's runners. If this repeats, set the repository variable MEHO_CI_SECOND_SITE to off."
      if gh run cancel "${GITHUB_RUN_ID}" --repo "${GITHUB_REPOSITORY}" >/dev/null 2>&1; then
        echo "Cancel request accepted."
        exit 0
      fi
      echo "the cancel request failed"
    elif [[ "${found}" -eq "${expected}" && "${started}" -eq "${expected}" ]]; then
      echo "Every unit shard on the second site has a runner now. Nothing to stop."
      exit 0
    else
      echo "shards found: ${found}/${expected}, started: ${started}, none known to wait; reading again"
    fi
  fi
  if (( try < last_tries )); then
    sleep "${retry_seconds}"
  fi
  try=$(( try + 1 ))
done
echo "::error title=Second runner site::Could not stop this run, or could not read its jobs in full. Check it, and cancel it by hand if shards still wait."
exit 1
