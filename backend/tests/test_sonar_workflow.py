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
    "queue_case",
    [
        "valid",
        "missing",
        "gap",
        "failed",
        "stale",
        "forged",
        "wrong_head",
        "expired",
        "rerun",
        "mixed_expiry",
        "all_expired",
        "duplicate_wrong_head",
        "failed_scan",
    ],
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
    event: queueCase === 'forged' ? 'pull_request' : 'workflow_run',
    conclusion: queueCase === 'failed_scan' ? 'failure' : 'success'}}),
  listWorkflowRunArtifacts: async () => {}
}},
  paginate: async (method, args) => {
    if (args.run_id) { const artifacts = [{
      name: 'sonar-gate-' + (['wrong_head', 'duplicate_wrong_head'].includes(queueCase) ? 'other'
        : args.run_id === 70 ? 'pr1' : 'pr2'),
      expired: ['expired', 'all_expired'].includes(queueCase)
    }];
      if (queueCase === 'mixed_expiry') return [...artifacts, {...artifacts[0], expired: true}];
      return ['rerun', 'all_expired', 'duplicate_wrong_head', 'failed_scan'].includes(queueCase)
        ? [...artifacts, ...artifacts] : artifacts;
    }
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
    passed = conclusion == "success" and (
        event != "merge_group" or queue_case in {"valid", "rerun", "mixed_expiry"}
    )
    assert value["statuses"][-1]["state"] == ("success" if passed else "failure")
    if event == "pull_request":
        assert json.loads(value["outputs"]["pr"]) == {
            "number": 7,
            "head": "feature",
            "base": "main",
        }
    else:
        assert value["outputs"]["pr"] == ""


@pytest.mark.parametrize("source_links", [False, True])
def test_preparation_ignores_checkout_python_modules(tmp_path: Path, source_links: bool) -> None:
    """A PR's Python modules must not run in the privileged scan job."""
    steps = yaml.safe_load(_WORKFLOW.read_text())["jobs"]["sonarcloud"]["steps"]
    prepare = next(step for step in steps if step.get("name") == "Prepare trusted scan settings")
    command, script = prepare["run"].split(" <<'PY'\n", 1)
    invocation = shlex.split(command.splitlines()[-1])
    script = script.rsplit("\nPY", 1)[0]
    assert invocation[0] == "python3"
    temporary = tmp_path / "runner-temp"
    temporary.mkdir()
    settings = temporary / "sonar-trusted.properties"
    settings.write_text("sonar.projectKey=test\n")
    version = temporary / "sonar-release-version"
    version.write_text("v1.2.3\n")
    python_reports = temporary / "sonar-coverage/python"
    python_reports.mkdir(parents=True)
    (python_reports / "coverage.xml").write_text(
        "<coverage><sources><source/></sources></coverage>"
    )
    go_reports = temporary / "sonar-coverage/go"
    go_reports.mkdir(parents=True)
    (go_reports / "coverage.out").write_text("mode: atomic\n")
    # The analyzed tree has poisoned reports/settings/work files. Preparation
    # must use only the separately downloaded reports and trusted settings.
    source = tmp_path / "analyzed-source"
    (source / "backend").mkdir(parents=True)
    if source_links:
        (source / "backend/coverage.xml").symlink_to(settings)
        (source / "sonar-project.properties").symlink_to(settings)
    else:
        (source / "backend/coverage.xml").write_text("invalid attacker report")
        (source / "sonar-project.properties").write_text(
            "sonar.host.url=https://attacker.invalid\n"
        )
    (source / ".scannerwork").mkdir()
    (source / ".scannerwork/poison").write_text("untrusted scanner workspace")
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    marker = tmp_path / "checkout-code-ran"
    poison = f"open({str(marker)!r}, 'w').write('untrusted')\nraise RuntimeError('poison module')\n"
    for name in ["json.py", "pathlib.py"]:
        (tmp_path / name).write_text(poison)
    (tmp_path / "xml").mkdir()
    (tmp_path / "xml/__init__.py").write_text(poison)
    env = {
        **os.environ,
        "PYTHONPATH": str(tmp_path),
        "RUNNER_TEMP": str(temporary),
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
    assert not marker.exists()
    if source_links:
        assert result.returncode != 0
        assert "symlink escapes checkout" in result.stderr
        assert settings.read_text() == "sonar.projectKey=test\n"
        return
    assert result.returncode == 0, result.stderr
    output = (temporary / "sonar-ci.properties").read_text()
    assert "sonar.projectVersion=v1.2.3\n" in output
    assert "sonar.pullrequest.key=7\n" in output
    assert "sonar.qualitygate.wait=true\n" in output
    assert "attacker.invalid" not in output
    assert f"sonar.python.coverage.reportPaths={python_reports / 'coverage.xml'}\n" in output
    assert f"sonar.go.coverage.reportPaths={go_reports / 'coverage.out'}\n" in output
    assert f"sonar.working.directory={temporary / 'sonar-work'}\n" in output
    assert settings.read_text() == "sonar.projectKey=test\n"
    expected_source_report = (
        "sonar.projectKey=test\n" if source_links else "invalid attacker report"
    )
    assert (source / "backend/coverage.xml").read_text() == expected_source_report
    assert (source / "sonar-project.properties").read_text() == output
    assert not (source / "sonar-project.properties").is_symlink()


def test_privileged_work_files_stay_outside_analyzed_source() -> None:
    """Scanner input is isolated from helper code, reports, and scanner output."""
    steps = yaml.safe_load(_WORKFLOW.read_text())["jobs"]["sonarcloud"]["steps"]
    checkout = next(
        step for step in steps if str(step.get("uses", "")).startswith("actions/checkout@")
    )
    assert checkout["with"]["path"] == "analyzed-source"
    assert checkout["with"]["persist-credentials"] is False
    for step in steps:
        assert step.get("working-directory") != "analyzed-source"
        if str(step.get("uses", "")).startswith("actions/download-artifact@"):
            assert step["with"]["path"].startswith("${{ runner.temp }}/sonar-coverage/")
    scanner = next(step for step in steps if step.get("name") == "SonarCloud scan and gate result")
    assert scanner["with"]["projectBaseDir"] == "analyzed-source"
    assert "project.settings" not in scanner["with"].get("args", "")


@pytest.mark.parametrize(
    ("kind", "allowed"),
    [
        ("internal_file", True),
        ("internal_dir", True),
        ("external_file", False),
        ("external_dir", False),
        ("broken", False),
        ("loop", False),
        ("ancestor", False),
    ],
)
def test_scan_rejects_unsafe_tracked_symlinks(tmp_path: Path, kind: str, allowed: bool) -> None:
    steps = yaml.safe_load(_WORKFLOW.read_text())["jobs"]["sonarcloud"]["steps"]
    prepare = next(step for step in steps if step.get("name") == "Prepare trusted scan settings")
    script = prepare["run"].split(" <<'PY'\n", 1)[1]
    # Run the actual source guard, stopping before report preparation.
    script = script.split("temporary = Path(os.environ['RUNNER_TEMP'])", 1)[0]
    source = tmp_path / "analyzed-source"
    (source / "src").mkdir(parents=True)
    (source / "src/real.py").write_text("# static input\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "dummy.py").write_text("# outside input\n")
    targets = {
        "internal_file": "real.py",
        "internal_dir": "../other",
        "external_file": str(outside / "dummy.py"),
        "external_dir": str(outside),
        "broken": "missing.py",
        "loop": "link.py",
        "ancestor": "..",
    }
    (source / "other").mkdir()
    (source / "src/link.py").symlink_to(targets[kind])
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    result = subprocess.run(
        [sys.executable, "-I", "-"],
        input=script,
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert (result.returncode == 0) is allowed, result.stderr
    if not allowed:
        assert "symlink" in result.stderr
