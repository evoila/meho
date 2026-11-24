# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The compare-with-main step in CI runs the judge in summary mode (#6867).

The job ``redaction-compare`` in ``.github/workflows/ci.yml`` writes to a
public log. The docs (``docs/codebase/redaction.md``, "Compare with main")
say that this step prints one summary line and that the full compare is run
locally. The flag ``--summary-only`` of ``scripts/ci/redaction_compare.py``
makes that true. This test fails when the flag is taken out of the workflow,
and when the job or its judge step is renamed or removed.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

# backend/tests/<this> → parents[2] == repo root
_CI_YML = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"
_JOB = "redaction-compare"
_WHERE = (
    f"Update backend/tests/{Path(__file__).name} if the job or its judge step "
    "was renamed or moved on purpose."
)


def _judge_commands(workflow_text: str) -> list[list[str]]:
    """The word lists of every ``redaction_compare.py --judge`` call in the job."""
    workflow = cast(dict[str, Any], yaml.safe_load(workflow_text))
    jobs = cast(dict[str, Any], workflow.get("jobs") or {})
    assert _JOB in jobs, f"ci.yml has no job '{_JOB}'. {_WHERE}"
    commands: list[list[str]] = []
    for step in jobs[_JOB].get("steps") or []:
        script = cast(str, step.get("run") or "")
        # Join the lines that end in a backslash, as the shell does.
        joined = script.replace("\\\n", " ")
        for line in joined.splitlines():
            if "redaction_compare.py" in line and "--judge" in line:
                commands.append(shlex.split(line, comments=True))
    return commands


def _check(workflow_text: str) -> None:
    commands = _judge_commands(workflow_text)
    assert commands, f"Job '{_JOB}' has no step that runs the judge with --judge. {_WHERE}"
    for command in commands:
        assert "--summary-only" in command, (
            f"The judge call in job '{_JOB}' has no --summary-only. "
            "The job log is public, so the judge must print only one summary line."
        )


def test_judge_step_runs_in_summary_mode() -> None:
    _check(_CI_YML.read_text(encoding="utf-8"))


def test_flag_taken_out_fails() -> None:
    text = _CI_YML.read_text(encoding="utf-8")
    assert "--summary-only" in text
    # Only the judge call sits right after "--base-approved ... \".
    changed = text.replace(
        '--base-approved "${dir}/base-approved.yaml" \\\n            --summary-only',
        '--base-approved "${dir}/base-approved.yaml"',
    )
    assert changed != text
    with pytest.raises(AssertionError, match="no --summary-only"):
        _check(changed)


def test_job_renamed_or_judge_step_gone_fails() -> None:
    text = _CI_YML.read_text(encoding="utf-8")
    with pytest.raises(AssertionError, match="no job"):
        _check(text.replace("  redaction-compare:\n", "  redaction-compare-renamed:\n"))
    with pytest.raises(AssertionError, match="no step that runs the judge"):
        _check(text.replace("--judge", "--other"))
