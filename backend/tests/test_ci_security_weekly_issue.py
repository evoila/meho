# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for ``scripts/ci/security_weekly_issue.py`` (the weekly scan issue).

The script gets a fake ``gh`` function that records its calls, so no network
is used.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "ci" / "security_weekly_issue.py"
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "security-weekly.yml"

_spec = importlib.util.spec_from_file_location("security_weekly_issue", _SCRIPT)
assert _spec is not None
assert _spec.loader is not None
swi = importlib.util.module_from_spec(_spec)
sys.modules["security_weekly_issue"] = swi
_spec.loader.exec_module(swi)

REPO = "example-org/findings"


def _vuln(
    advisory: str, pkg: str, installed: str = "1.0.0", fixed: str | None = "1.0.1"
) -> dict[str, Any]:
    vuln: dict[str, Any] = {
        "VulnerabilityID": advisory,
        "PkgName": pkg,
        "InstalledVersion": installed,
    }
    if fixed is not None:
        vuln["FixedVersion"] = fixed
    return vuln


def _report(*vulns: dict[str, Any], target: str = "backend/uv.lock") -> dict[str, Any]:
    return {"Results": [{"Target": target, "Vulnerabilities": list(vulns)}]}


class FakeGh:
    """Answers the gh calls of the script and records the writes."""

    def __init__(
        self, issues: list[dict[str, Any]] | None = None, comments: list[str] | None = None
    ):
        self.issues = issues or []
        self.comments = comments or []
        self.writes: list[list[str]] = []

    def __call__(self, args: Sequence[str]) -> str:
        args = list(args)
        if args[:2] == ["issue", "list"]:
            return json.dumps(self.issues)
        if args[:2] == ["issue", "view"]:
            return json.dumps({"comments": [{"body": c} for c in self.comments]})
        self.writes.append(args)
        return ""


def _open_issue_with(findings: list[Any]) -> dict[str, Any]:
    body = swi.render(findings, "2026-11-02", is_comment=False)
    return {"number": 7, "title": swi.TITLE, "body": body}


def test_no_findings_means_no_action() -> None:
    gh = FakeGh()
    assert swi.sync(swi.parse_findings({"Results": []}), REPO, "2026-11-09", gh) == "none"
    assert swi.sync(swi.parse_findings({}), REPO, "2026-11-09", gh) == "none"
    assert swi.sync(swi.parse_findings(_report()), REPO, "2026-11-09", gh) == "none"
    assert gh.writes == []


def test_null_vulnerabilities_field_is_no_finding() -> None:
    report = {"Results": [{"Target": "cli/go.mod", "Vulnerabilities": None}]}
    assert swi.parse_findings(report) == []


def test_new_finding_without_open_issue_opens_one_issue() -> None:
    findings = swi.parse_findings(_report(_vuln("CVE-2099-0001", "examplepkg", "1.0.0", "1.0.5")))
    gh = FakeGh()
    assert swi.sync(findings, REPO, "2026-11-09", gh) == "created"
    assert len(gh.writes) == 1
    call = gh.writes[0]
    assert call[:2] == ["issue", "create"]
    assert call[call.index("--title") + 1] == swi.TITLE
    assert call[call.index("--label") + 1] == "bug,dependencies"
    body = call[call.index("--body") + 1]
    for part in ("examplepkg", "1.0.0", "1.0.5", "CVE-2099-0001", "backend/uv.lock", "2026-11-09"):
        assert part in body


def test_same_finding_again_means_no_action() -> None:
    findings = swi.parse_findings(_report(_vuln("CVE-2099-0001", "examplepkg")))
    gh = FakeGh(issues=[_open_issue_with(findings)])
    assert swi.sync(findings, REPO, "2026-11-16", gh) == "none"
    assert gh.writes == []


def test_new_finding_with_open_issue_adds_one_comment_with_only_new_findings() -> None:
    old = swi.parse_findings(_report(_vuln("CVE-2099-0001", "examplepkg")))
    now = swi.parse_findings(
        _report(_vuln("CVE-2099-0001", "examplepkg"), _vuln("CVE-2099-0002", "otherpkg"))
    )
    gh = FakeGh(issues=[_open_issue_with(old)])
    assert swi.sync(now, REPO, "2026-11-16", gh) == "commented"
    assert len(gh.writes) == 1
    call = gh.writes[0]
    assert call[:3] == ["issue", "comment", "7"]
    body = call[call.index("--body") + 1]
    assert "CVE-2099-0002" in body
    assert "CVE-2099-0001" not in body


def test_finding_named_in_an_earlier_comment_is_not_reported_again() -> None:
    first = swi.parse_findings(_report(_vuln("CVE-2099-0001", "examplepkg")))
    second = swi.parse_findings(_report(_vuln("CVE-2099-0002", "otherpkg")))
    gh = FakeGh(
        issues=[_open_issue_with(first)],
        comments=[swi.render(second, "2026-11-09", is_comment=True)],
    )
    assert swi.sync(first + second, REPO, "2026-11-16", gh) == "none"
    assert gh.writes == []


def test_closed_issue_is_not_found_so_a_new_one_opens() -> None:
    findings = swi.parse_findings(_report(_vuln("CVE-2099-0001", "examplepkg")))
    gh = FakeGh(issues=[])  # gh lists only open issues
    assert swi.sync(findings, REPO, "2026-11-23", gh) == "created"


def test_issue_with_another_title_is_ignored() -> None:
    findings = swi.parse_findings(_report(_vuln("CVE-2099-0001", "examplepkg")))
    gh = FakeGh(issues=[{"number": 3, "title": "Something else", "body": ""}])
    assert swi.sync(findings, REPO, "2026-11-09", gh) == "created"


def test_same_package_in_two_lock_files_is_two_findings() -> None:
    report = {
        "Results": [
            {"Target": "a/package-lock.json", "Vulnerabilities": [_vuln("CVE-2099-0003", "dup")]},
            {"Target": "b/package-lock.json", "Vulnerabilities": [_vuln("CVE-2099-0003", "dup")]},
        ]
    }
    assert len(swi.parse_findings(report)) == 2


def test_duplicate_entries_are_merged_and_missing_fix_is_said() -> None:
    findings = swi.parse_findings(
        _report(_vuln("CVE-2099-0004", "p", fixed=None), _vuln("CVE-2099-0004", "p", fixed=None))
    )
    assert len(findings) == 1
    assert findings[0].fixed == "no fix yet"


@pytest.mark.parametrize("bad", ["[]", '"text"', '{"Results": "x"}'])
def test_odd_report_shape_is_an_error(bad: str, tmp_path: Path) -> None:
    report = tmp_path / "r.json"
    report.write_text(bad)
    assert swi.main(["--report", str(report), "--repo", REPO]) == 1


def test_missing_or_broken_report_is_an_error_not_no_findings(tmp_path: Path) -> None:
    assert swi.main(["--report", str(tmp_path / "missing.json"), "--repo", REPO]) == 1
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    assert swi.main(["--report", str(broken), "--repo", REPO]) == 1


def test_finding_without_id_is_an_error(tmp_path: Path) -> None:
    report = tmp_path / "r.json"
    report.write_text(json.dumps(_report({"PkgName": "x"})))
    assert swi.main(["--report", str(report), "--repo", REPO]) == 1


def test_gh_failure_is_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def failing(_args: Sequence[str]) -> str:
        raise RuntimeError("gh failed")

    monkeypatch.setattr(swi, "run_gh", failing)
    report = tmp_path / "r.json"
    report.write_text(json.dumps(_report(_vuln("CVE-2099-0001", "examplepkg"))))
    assert swi.main(["--report", str(report), "--repo", REPO]) == 1


def test_workflow_has_schedule_dispatch_and_least_privilege() -> None:
    workflow = yaml.safe_load(_WORKFLOW.read_text())
    triggers = workflow[True] if True in workflow else workflow["on"]
    assert triggers["schedule"][0]["cron"]
    assert "workflow_dispatch" in triggers
    assert "pull_request" not in triggers
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["jobs"]["scan"]["runs-on"] == "ubuntu-latest"


def _workflow_step(name: str) -> dict[str, Any]:
    workflow = yaml.safe_load(_WORKFLOW.read_text())
    steps = workflow["jobs"]["scan"]["steps"]
    return next(s for s in steps if s["name"] == name)


def test_workflow_scans_every_severity() -> None:
    scan = _workflow_step("Scan the dependency files (trivy)")
    severities = set(scan["with"]["severity"].split(","))
    assert severities == {"UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL"}


def test_workflow_reads_findings_repo_and_token_from_secrets() -> None:
    env = _workflow_step("Open or update the findings issue")["env"]
    assert env["FINDINGS_REPO"] == "${{ secrets.SECURITY_FINDINGS_REPO }}"
    assert env["GH_TOKEN"] == "${{ secrets.SECURITY_FINDINGS_TOKEN }}"
    assert "vars." not in _WORKFLOW.read_text()
