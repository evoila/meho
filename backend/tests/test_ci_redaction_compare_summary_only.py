# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The compare-with-main step in CI runs the judge in summary mode (#6867).

The job ``redaction-compare`` in ``.github/workflows/ci.yml`` writes to a
public log. The docs (``docs/codebase/redaction.md``, "Compare with main")
say that this step prints one summary line and that the full compare is run
locally. The flag ``--summary-only`` of ``scripts/ci/redaction_compare.py``
makes that true. This test fails when the flag is taken out of the workflow,
when the job or its judge step is renamed or removed, and when the step
only prints the judge command instead of running it.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

# backend/tests/<this> → parents[2] == repo root
_CI_YML = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"
_JOB = "redaction-compare"
_STEP = "Build and judge the records of the base and of the pull request"
_SCRIPT = "redaction_compare.py"
_WHERE = (
    f"Update backend/tests/{Path(__file__).name} if the job or its judge step "
    "was renamed or moved on purpose."
)


def _judge_step_script(workflow_text: str) -> str:
    """The ``run`` text of the judge step, found by the step's name."""
    workflow = cast(dict[str, Any], yaml.safe_load(workflow_text))
    jobs = cast(dict[str, Any], workflow.get("jobs") or {})
    assert _JOB in jobs, f"ci.yml has no job '{_JOB}'. {_WHERE}"
    for step in jobs[_JOB].get("steps") or []:
        if step.get("name") == _STEP:
            return cast(str, step.get("run") or "")
    raise AssertionError(f"Job '{_JOB}' has no step named '{_STEP}'. {_WHERE}")


def _runs_script(words: list[str]) -> bool:
    """True when the words start Python on the compare script.

    Before the script there may be only ``python``/``python3`` (or
    ``uv run python``) and its options, so a line such as
    ``echo python3 redaction_compare.py --judge`` does not count.
    """
    at = next((i for i, w in enumerate(words) if w.endswith(_SCRIPT)), None)
    if at is None:
        return False
    launcher = words[:at]
    if launcher[:2] == ["uv", "run"]:
        launcher = launcher[2:]
    if not launcher or launcher[0] not in {"python", "python3"}:
        return False
    return all(w.startswith("-") for w in launcher[1:])


def _judge_commands(workflow_text: str) -> list[list[str]]:
    """The word lists of every call that runs the script with ``--judge``."""
    # Join the lines that end in a backslash, as the shell does.
    joined = _judge_step_script(workflow_text).replace("\\\n", " ")
    commands: list[list[str]] = []
    for line in joined.splitlines():
        if _SCRIPT not in line:
            continue
        words = shlex.split(line, comments=True)
        if _runs_script(words) and "--judge" in words:
            commands.append(words)
    return commands


def _check(workflow_text: str) -> None:
    commands = _judge_commands(workflow_text)
    assert commands, (
        f"Step '{_STEP}' of job '{_JOB}' has no line that runs {_SCRIPT} with --judge. {_WHERE}"
    )
    for command in commands:
        assert "--summary-only" in command, (
            f"The judge call in job '{_JOB}' has no --summary-only. "
            "The job log is public, so the judge must print only one summary line."
        )


def _ci_text() -> str:
    return _CI_YML.read_text(encoding="utf-8")


def _without_flag(text: str) -> str:
    """The text with every ``--summary-only`` taken out, wherever it stands."""
    return re.sub(r"[ \t]+(?:\\\n[ \t]*)?--summary-only\b", "", text)


def test_judge_step_runs_in_summary_mode() -> None:
    _check(_ci_text())


def test_flag_taken_out_fails() -> None:
    text = _ci_text()
    changed = _without_flag(text)
    assert changed != text
    assert "--summary-only" not in changed
    with pytest.raises(AssertionError, match="no --summary-only"):
        _check(changed)


def test_flag_in_another_place_passes() -> None:
    text = _ci_text()
    changed = _without_flag(text)
    changed = changed.replace("--judge ", "--summary-only --judge ")
    assert changed.count("--summary-only --judge ") == 1
    _check(changed)


def test_judge_only_printed_fails() -> None:
    text = _ci_text()
    changed = text.replace(
        'python3 -I "${dir}/redaction_compare.py"',
        'echo python3 -I "${dir}/redaction_compare.py"',
    )
    assert changed != text
    with pytest.raises(AssertionError, match="no line that runs"):
        _check(changed)


def test_job_renamed_fails() -> None:
    text = _ci_text()
    changed = text.replace(f"  {_JOB}:\n", f"  {_JOB}-renamed:\n")
    assert changed != text
    with pytest.raises(AssertionError, match=r"no job.*Update backend/tests/"):
        _check(changed)


def test_judge_step_renamed_fails() -> None:
    text = _ci_text()
    changed = text.replace(f"name: {_STEP}\n", f"name: {_STEP} (renamed)\n")
    assert changed != text
    with pytest.raises(AssertionError, match=r"no step named.*Update backend/tests/"):
        _check(changed)


def test_judge_call_gone_fails() -> None:
    with pytest.raises(AssertionError, match="no line that runs"):
        _check(_ci_text().replace("--judge", "--other"))
