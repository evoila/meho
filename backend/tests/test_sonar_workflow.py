# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Exercise scan routing without calling GitHub or SonarCloud."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/quality-gate.yml"


@pytest.mark.parametrize("event", ["push", "pull_request", "merge_group"])
@pytest.mark.parametrize("conclusion", ["success", "failure", "cancelled"])
@pytest.mark.parametrize(
    "queue_case", ["valid", "missing", "gap", "failed", "stale", "forged", "wrong_head", "expired"]
)
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
}, actions: {
  getWorkflow: async () => ({data: {id: 123}}),
  getWorkflowRun: async () => ({data: {workflow_id: 123, head_branch: 'main',
    event: queueCase === 'forged' ? 'pull_request' : 'workflow_run', conclusion: 'success'}}),
  listWorkflowRunArtifacts: async () => {}
}},
  paginate: async (method, args) => {
    if (args.run_id) return [{
      name: 'sonar-gate-' + (queueCase === 'wrong_head' ? 'other'
        : args.run_id === 70 ? 'pr1' : 'pr2'),
      expired: queueCase === 'expired'
    }];
    // Both PR heads must be checked. One passing final PR is insufficient.
    if (args.ref !== 'pr1' && args.ref !== 'pr2') return [];
    return [{context: 'SonarCloud Quality Gate',
      state: queueCase === 'failed' && args.ref === 'pr1' ? 'failure' : 'success',
      creator: {login: 'github-actions[bot]'},
      target_url: `https://github.com/owner/repo/actions/runs/${args.ref === 'pr1' ? 70 : 71}`}];
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


def test_preparation_ignores_checkout_python_modules(tmp_path: Path) -> None:
    """A PR's Python modules must not run in the privileged scan job."""
    steps = yaml.safe_load(_WORKFLOW.read_text())["jobs"]["sonarcloud"]["steps"]
    prepare = next(step for step in steps if step.get("name") == "Prepare trusted scan settings")
    command, script = prepare["run"].split(" <<'PY'\n", 1)
    invocation = shlex.split(command.splitlines()[-1])
    script = script.rsplit("\nPY", 1)[0]
    assert invocation[0] == "python3"
    # The script reads two fixed temporary files prepared by the preceding git
    # commands. Redirect only those paths into this isolated test directory.
    settings = tmp_path / "trusted.properties"
    settings.write_text("sonar.projectKey=test\n")
    version = tmp_path / "release-version"
    version.write_text("v1.2.3\n")
    script = script.replace("/tmp/sonar-trusted.properties", str(settings))
    script = script.replace("/tmp/sonar-release-version", str(version))
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend/coverage.xml").write_text(
        "<coverage><sources><source/></sources></coverage>"
    )
    (tmp_path / "cli").mkdir()
    (tmp_path / "cli/coverage.out").write_text("mode: atomic\n")
    marker = tmp_path / "checkout-code-ran"
    poison = f"open({str(marker)!r}, 'w').write('untrusted')\nraise RuntimeError('poison module')\n"
    for name in ["json.py", "pathlib.py"]:
        (tmp_path / name).write_text(poison)
    (tmp_path / "xml").mkdir()
    (tmp_path / "xml/__init__.py").write_text(poison)
    env = {
        **os.environ,
        "PYTHONPATH": str(tmp_path),
        "ANALYSIS_EVENT": "pull_request",
        "ANALYZED_SHA": "abc123",
        "ANALYSIS_PR": json.dumps({"number": 7, "head": "feature", "base": "main"}),
    }
    # Prove that the fixture can execute checkout code without isolation.
    control = subprocess.run(
        [sys.executable, "-c", "import json"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert control.returncode != 0
    assert marker.exists()
    marker.unlink()
    result = subprocess.run(
        [sys.executable, *invocation[1:]],
        input=script,
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    output = (tmp_path / ".sonar-ci.properties").read_text()
    assert "sonar.projectVersion=v1.2.3\n" in output
    assert "sonar.pullrequest.key=7\n" in output
    assert "sonar.qualitygate.wait=true\n" in output
