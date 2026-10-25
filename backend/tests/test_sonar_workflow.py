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
def test_gate_status_targets_upstream_commit(event: str, conclusion: str) -> None:
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
const core = {setOutput: (key, value) => outputs[key] = value};
const github = {rest: {repos: {
  listPullRequestsAssociatedWithCommit: async () => ({data: [{number: 7,
    head: {sha: 'abc123', ref: 'feature'},
    base: {ref: 'main', repo: {full_name: 'owner/repo'}}}]}),
  createCommitStatus: async value => statuses.push(value)
}}};
(async () => {
  let error = null;
  try { await (async () => { METADATA })(); } catch (e) { error = e.message; }
  process.env.JOB_STATUS = error ? 'failure' : 'success';
  await (async () => { TERMINAL })();
  console.log(JSON.stringify({statuses, outputs, error}));
})();
"""
    harness = harness.replace("RUN", json.dumps(run)).replace("METADATA", metadata)
    harness = harness.replace("TERMINAL", terminal)
    result = subprocess.run([node, "-e", harness], check=True, capture_output=True, text=True)
    value = json.loads(result.stdout)
    assert [s["sha"] for s in value["statuses"]] == ["abc123", "abc123"]
    assert value["statuses"][0]["state"] == "pending"
    assert value["statuses"][-1]["state"] == ("success" if conclusion == "success" else "failure")
    if event == "pull_request":
        assert json.loads(value["outputs"]["pr"]) == {
            "number": 7,
            "head": "feature",
            "base": "main",
        }
    else:
        assert value["outputs"]["pr"] == ""
