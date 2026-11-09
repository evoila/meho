#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
"""Open or update the one findings issue of the weekly security scan.

Input: the JSON report of ``trivy fs``. Output: at most one new issue or one
new comment in the findings repository.

- No finding: do nothing.
- Findings, no open findings issue: open one issue that lists all of them.
- Findings, an open findings issue: add one comment that lists only the
  findings the issue and its comments do not name yet.

Every finding line carries a hidden key (``<!-- finding: ... -->``). The next
run reads the keys back from the issue and its comments, so a finding is
never reported twice. A person closes the issue. When the issue is closed and
a finding is still there, the next run opens a new issue.

A report that cannot be read stops the script with an error. It never counts
as "no findings".

Usage: security_weekly_issue.py --report trivy.json --repo <owner/name>
The GitHub CLI needs ``GH_TOKEN`` with ``issues: write`` on that repo.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

TITLE = "Weekly dependency scan: known security advisories found"
LABELS = ("bug", "dependencies")
_KEY_OPEN = "<!-- finding: "
_KEY_CLOSE = " -->"


@dataclass(frozen=True)
class Finding:
    advisory: str
    package: str
    installed: str
    fixed: str
    target: str

    @property
    def key(self) -> str:
        return f"{self.advisory}|{self.package}|{self.installed}|{self.target}"

    def line(self) -> str:
        return (
            f"- `{self.package}` {self.installed}: fixed in {self.fixed}. "
            f"Advisory {self.advisory}. Lock file `{self.target}`. "
            f"{_KEY_OPEN}{self.key}{_KEY_CLOSE}"
        )


def parse_findings(report: Any) -> list[Finding]:
    """Read the findings out of a trivy JSON report, sorted and without duplicates."""
    if not isinstance(report, dict):
        raise ValueError("the trivy report is not a JSON object")
    results = report.get("Results") or []
    if not isinstance(results, list):
        raise ValueError("the trivy report has a Results field that is not a list")
    found: dict[str, Finding] = {}
    for result in results:
        target = str(result.get("Target", ""))
        for vuln in result.get("Vulnerabilities") or []:
            finding = Finding(
                advisory=str(vuln["VulnerabilityID"]),
                package=str(vuln["PkgName"]),
                installed=str(vuln.get("InstalledVersion", "")),
                fixed=str(vuln.get("FixedVersion") or "no fix yet"),
                target=target,
            )
            found[finding.key] = finding
    return [found[key] for key in sorted(found)]


def known_keys(text: str) -> set[str]:
    """The finding keys that a text (issue body or comment) already names."""
    keys: set[str] = set()
    start = text.find(_KEY_OPEN)
    while start != -1:
        begin = start + len(_KEY_OPEN)
        end = text.find(_KEY_CLOSE, begin)
        if end == -1:
            break
        keys.add(text[begin:end])
        start = text.find(_KEY_OPEN, end)
    return keys


def render(findings: Sequence[Finding], scan_date: str, *, is_comment: bool) -> str:
    head = (
        f"New findings in the weekly scan of {scan_date}:"
        if is_comment
        else (
            f"The weekly scan of {scan_date} found packages with known security "
            "advisories that have a fix. Update each package to the fixed version "
            "or newer. A person closes this issue when the findings are fixed."
        )
    )
    return head + "\n\n" + "\n".join(f.line() for f in findings) + "\n"


Gh = Callable[[Sequence[str]], str]


def run_gh(args: Sequence[str]) -> str:
    done = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:2])} failed: {done.stderr.strip()}")
    return done.stdout


def find_open_issue(repo: str, gh: Gh) -> dict[str, Any] | None:
    out = gh(
        [
            "issue", "list", "--repo", repo, "--state", "open",
            "--label", LABELS[0], "--label", LABELS[1],
            "--search", f'"{TITLE}" in:title',
            "--limit", "100", "--json", "number,title,body",
        ]
    )  # fmt: skip
    for issue in json.loads(out):
        if issue["title"] == TITLE:
            return dict(issue)
    return None


def sync(findings: Sequence[Finding], repo: str, scan_date: str, gh: Gh) -> str:
    """Report the findings. Returns what it did: ``none``, ``created``, ``commented``."""
    if not findings:
        return "none"
    issue = find_open_issue(repo, gh)
    if issue is None:
        gh(
            [
                "issue", "create", "--repo", repo, "--title", TITLE,
                "--label", ",".join(LABELS),
                "--body", render(findings, scan_date, is_comment=False),
            ]
        )  # fmt: skip
        return "created"
    number = str(issue["number"])
    view = json.loads(gh(["issue", "view", number, "--repo", repo, "--json", "comments"]))
    text = str(issue.get("body") or "") + "\n".join(c["body"] for c in view["comments"])
    seen = known_keys(text)
    new = [f for f in findings if f.key not in seen]
    if not new:
        return "none"
    gh(
        [
            "issue",
            "comment",
            number,
            "--repo",
            repo,
            "--body",
            render(new, scan_date, is_comment=True),
        ]
    )
    return "commented"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, type=Path, help="trivy JSON report")
    parser.add_argument("--repo", required=True, help="repository for the findings issue")
    args = parser.parse_args(argv)
    try:
        findings = parse_findings(json.loads(args.report.read_text()))
        action = sync(findings, args.repo, datetime.now(UTC).date().isoformat(), run_gh)
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        print(f"security_weekly_issue: error: {exc}", file=sys.stderr)
        return 1
    print(f"security_weekly_issue: {len(findings)} finding(s), action: {action}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
