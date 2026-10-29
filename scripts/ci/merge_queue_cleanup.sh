#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# Cancels the open merge-queue runs of every workflow whose group the queue
# has dropped (#5292). .github/workflows/merge-queue-cleanup.yml runs it
# every 5 minutes; its header explains why.
#
# A run is cancelled only when all of these hold:
#   - its commit is not in the queue (GraphQL `mergeQueue`, `headCommit`),
#   - its queue branch is gone (the queue deletes it when the group leaves),
#   - its commit is not on the base branch: the merge base of the commit and
#     the branch is not the commit itself (a merged group's commit is part
#     of the branch),
#   - and, read once more right before the cancel, the queue still does not
#     hold the commit and the exact branch is still gone. A new group can
#     take the same commit or branch after the first reads. The second look
#     makes that window small; it cannot close it, because the reads and the
#     cancel are separate API calls.
# When a read fails or its answer cannot be read, the sweep stops with an
# error and cancels nothing after that point. A missing answer never counts
# as "empty queue" or "branch gone".
#
# Needs gh (logged in through GH_TOKEN), jq, and these variables:
#   GITHUB_REPOSITORY  owner/name
#   BASE_BRANCH        the branch the merge queue merges into
#   DRY_RUN            "true": do every read, print what it would cancel,
#                      cancel nothing
set -euo pipefail

api="repos/${GITHUB_REPOSITORY}"
prefix="gh-readonly-queue/${BASE_BRANCH}/"

stop() {
  echo "::error title=Merge queue cleanup::${1} The sweep stopped; it cancels nothing after a failed read."
  exit 1
}

# Prints the head commits of all queue entries as a JSON array.
read_queue() {
  local answer
  # shellcheck disable=SC2016  # $owner, $name and $branch are GraphQL variables
  answer="$(gh api graphql \
      -f owner="${GITHUB_REPOSITORY%/*}" -f name="${GITHUB_REPOSITORY#*/}" -f branch="${BASE_BRANCH}" \
      -f query='query($owner: String!, $name: String!, $branch: String!) {
        repository(owner: $owner, name: $name) {
          mergeQueue(branch: $branch) {
            entries(first: 100) { totalCount nodes { headCommit { oid } } }
          }
        }
      }')" || return 1
  jq -ce '.data.repository.mergeQueue.entries
      | if . == null then error("no merge queue in the answer")
        elif .totalCount > (.nodes | length) then error("more queue entries than one page")
        else [.nodes[].headCommit.oid // empty] end' <<<"${answer}"
}

# Prints the branch names under `$1` as a JSON array. The matching-refs API
# answers with a list, also an empty one, so an error never looks like a
# deleted branch.
read_branches() {
  local answer
  answer="$(gh api "${api}/git/matching-refs/heads/${1}")" || return 1
  jq -ce 'if type == "array" then [.[].ref | ltrimstr("refs/heads/")]
          else error("not a list of refs") end' <<<"${answer}"
}

# Prints true when commit `$1` is on the base branch: its merge base with
# the branch is the commit itself.
on_base_branch() {
  local answer
  answer="$(gh api "${api}/compare/${1}...${BASE_BRANCH}?per_page=1")" || return 1
  jq -r --arg sha "${1}" '(.merge_base_commit.sha // error("no merge base in the answer")) == $sha' \
    <<<"${answer}"
}

# 1. The open merge-queue runs of every workflow, all pages.
runs='[]'
for status in queued in_progress; do
  pages="$(gh api "${api}/actions/runs?event=merge_group&status=${status}&per_page=100" --paginate --slurp)" \
    || stop "Could not list the ${status} merge-queue runs."
  runs="$(jq -c --argjson acc "${runs}" --arg prefix "${prefix}" \
      '$acc + [.[].workflow_runs[] | select((.head_branch // "") | startswith($prefix))
               | {id, name, head_sha, head_branch}] | unique_by(.id)' <<<"${pages}")" \
    || stop "Could not read the list of ${status} merge-queue runs."
done
echo "$(jq length <<<"${runs}") open merge-queue run(s)"
if [ "$(jq length <<<"${runs}")" -eq 0 ]; then
  exit 0
fi

# 2. Runs whose commit the queue does not hold.
in_queue="$(read_queue)" || stop "Could not read the merge queue."
candidates="$(jq -c --argjson queue "${in_queue}" \
    '[.[] | select(.head_sha | IN($queue[]) | not)]' <<<"${runs}")" \
  || stop "Could not compare the runs with the queue."
echo "$(jq length <<<"${candidates}") run(s) whose commit is not in the queue"
if [ "$(jq length <<<"${candidates}")" -eq 0 ]; then
  exit 0
fi

# 3. Of those, the runs whose queue branch is gone.
branches="$(read_branches "${prefix}")" || stop "Could not list the queue branches."
candidates="$(jq -c --argjson branches "${branches}" \
    '[.[] | select(.head_branch | IN($branches[]) | not)]' <<<"${candidates}")" \
  || stop "Could not compare the runs with the queue branches."
if [ "$(jq length <<<"${candidates}")" -eq 0 ]; then
  echo "every such run still has its queue branch; nothing to cancel"
  exit 0
fi

# 4. Per commit: skip it when it is on the base branch, else check each of
# its runs once more and cancel it.
for sha in $(jq -r '[.[].head_sha] | unique | .[]' <<<"${candidates}"); do
  merged="$(on_base_branch "${sha}")" \
    || stop "Could not check whether commit ${sha} is on ${BASE_BRANCH}."
  if [ "${merged}" = "true" ]; then
    echo "commit ${sha} is on ${BASE_BRANCH}, so its group merged; keeping its runs"
    continue
  fi
  while IFS=$'\t' read -r -u 3 id name branch; do
    in_queue="$(read_queue)" || stop "Could not read the merge queue again."
    if [ "$(jq -r --arg sha "${sha}" 'any(.[]; . == $sha)' <<<"${in_queue}")" = "true" ]; then
      echo "commit ${sha} is in the queue again; keeping run ${id} (${name})"
      continue
    fi
    found="$(read_branches "${branch}")" || stop "Could not read the branch ${branch} again."
    if [ "$(jq -r --arg branch "${branch}" 'any(.[]; . == $branch)' <<<"${found}")" = "true" ]; then
      echo "branch ${branch} exists again; keeping run ${id} (${name})"
      continue
    fi
    if [ "${DRY_RUN}" = "true" ]; then
      echo "dry run: would cancel run ${id} (${name}) of the dropped group ${branch}"
    elif gh api -X POST "${api}/actions/runs/${id}/cancel" >/dev/null; then
      echo "::notice title=Merge queue cleanup::Cancelled run ${id} (${name}): the queue has dropped its group ${branch}."
    else
      # Most often the run finished between the list and the cancel.
      echo "::warning title=Merge queue cleanup::Could not cancel run ${id} (${name}) of ${branch}; the next sweep tries again."
    fi
  done 3< <(jq -r --arg sha "${sha}" \
      '.[] | select(.head_sha == $sha) | [.id, .name, .head_branch] | @tsv' <<<"${candidates}")
done
