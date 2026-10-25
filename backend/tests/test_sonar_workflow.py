# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Exercise scan routing without calling GitHub or SonarCloud."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/quality-gate.yml"


@pytest.mark.parametrize("event", ["push", "pull_request", "merge_group"])
@pytest.mark.parametrize("conclusion", ["success", "failure", "cancelled"])
@pytest.mark.parametrize("queue_case", ["valid", "missing", "gap", "failed", "stale"])
def test_gate_status_targets_upstream_commit(event: str, conclusion: str, queue_case: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is needed to exercise the GitHub script")
    steps = yaml.safe_load(_WORKFLOW.read_text())["jobs"]["sonarcloud"]["steps"]
    metadata = steps[0]["with"]["script"]
    terminal = steps[-1]["with"]["script"]
    run = {"event": event, "conclusion": conclusion, "head_branch": "main", "head_sha": "abc123"}
    harness = """
const statuses = [];
const outputs = {};
const context = {payload: {workflow_run: RUN}, repo: {owner: 'owner', repo: 'repo'},
  serverUrl: 'https://github.com', runId: 42};
const core = {setOutput: (key, value) => outputs[key] = value, info: () => {}};
const queueCase = QUEUE_CASE;
const github = {rest: {repos: {
  listPullRequestsAssociatedWithCommit: async () => ({data: [{number: 7,
    head: {sha: 'abc123', ref: 'feature'},
    base: {ref: 'main', repo: {full_name: 'owner/repo'}}}]}),
  createCommitStatus: async value => statuses.push(value),
  listCommitStatusesForRef: async () => {}
}},
  paginate: async (method, args) => {
    // Both PR heads must be checked. One passing final PR is insufficient.
    if (args.ref !== 'pr1' && args.ref !== 'pr2') return [];
    return [{context: 'SonarCloud Quality Gate',
      state: queueCase === 'failed' && args.ref === 'pr1' ? 'failure' : 'success',
      creator: {login: 'github-actions[bot]'}}];
  },
  graphql: async () => ({repository: {
    defaultBranchRef: {name: 'main', target: {oid: 'main0'}},
    mergeQueue: {entries: {pageInfo: {hasNextPage: false}, nodes: [
      {position: 1, baseCommit: {oid: 'main0'}, headCommit: {oid: 'queue1'},
        pullRequest: {number: 1, headRefOid: 'pr1', baseRefName: 'main'}},
      {position: queueCase === 'gap' ? 3 : 2,
        baseCommit: {oid: queueCase === 'stale' ? 'old' : 'queue1'},
        headCommit: {oid: queueCase === 'missing' ? 'other' : 'abc123'},
        pullRequest: {number: 2, headRefOid: 'pr2', baseRefName: 'main'}}
    ]}}
  }})
};
(async () => {
  let error = null;
  try { await (async () => { METADATA })(); } catch (e) { error = e.message; }
  process.env.JOB_STATUS = error ? 'failure' : 'success';
  await (async () => { TERMINAL })();
  console.log(JSON.stringify({statuses, outputs, error}));
})();
"""
    harness = harness.replace("RUN", json.dumps(run)).replace("QUEUE_CASE", json.dumps(queue_case))
    harness = harness.replace("METADATA", metadata)
    harness = harness.replace("TERMINAL", terminal)
    result = subprocess.run([node, "-e", harness], check=True, capture_output=True, text=True)
    value = json.loads(result.stdout)
    assert [s["sha"] for s in value["statuses"]] == ["abc123", "abc123"]
    assert value["statuses"][0]["state"] == "pending"
    passed = conclusion == "success" and (event != "merge_group" or queue_case == "valid")
    assert value["statuses"][-1]["state"] == ("success" if passed else "failure")
    if event == "pull_request":
        assert json.loads(value["outputs"]["pr"]) == {
            "number": 7,
            "head": "feature",
            "base": "main",
        }
    else:
        assert value["outputs"]["pr"] == ""
