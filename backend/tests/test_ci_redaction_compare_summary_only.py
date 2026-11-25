# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The compare-with-main step in CI runs the judge in summary mode (#6867).

The job ``redaction-compare`` in ``.github/workflows/ci.yml`` writes to a
public log. The docs (``docs/codebase/redaction.md``, "Compare with main")
say that this step prints one summary line and that the full compare is run
locally. The flag ``--summary-only`` of ``scripts/ci/redaction_compare.py``
makes that true. This test fails when the flag is taken out of the workflow,
when the job or its judge step is renamed or removed, and when the step
only prints the judge command instead of running it. It reads every step
of every job, so a judge call in another step or job needs the flag too.
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


_OPERATORS = frozenset({"&&", "||", ";", "|"})


def _jobs(workflow_text: str) -> dict[str, Any]:
    workflow = cast(dict[str, Any], yaml.safe_load(workflow_text))
    return cast(dict[str, Any], workflow.get("jobs") or {})


def _check_named_step_exists(jobs: dict[str, Any]) -> None:
    """The job and its judge step exist, found by their names."""
    assert _JOB in jobs, f"ci.yml has no job '{_JOB}'. {_WHERE}"
    names = [step.get("name") for step in jobs[_JOB].get("steps") or []]
    if _STEP not in names:
        raise AssertionError(f"Job '{_JOB}' has no step named '{_STEP}'. {_WHERE}")


def _steps(jobs: dict[str, Any]) -> list[tuple[str, str, str]]:
    """(job, step name, run text) for every step of every job that has a run text."""
    found: list[tuple[str, str, str]] = []
    for job_id, job in jobs.items():
        for index, step in enumerate(job.get("steps") or []):
            run = step.get("run")
            if run:
                found.append((job_id, str(step.get("name") or f"step {index + 1}"), run))
    return found


def _call_words(line: str) -> list[str]:
    """The words of the first command on the line.

    Words after the first shell operator (``&&``, ``||``, ``;``, ``|``) or
    after a shell comment belong to another command, so they do not count.
    """
    lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    words: list[str] = []
    for word in lexer:
        if word in _OPERATORS:
            break
        words.append(word)
    return words


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


def _check_line(job: str, step: str, line: str) -> bool:
    """Check one shell line; True when it is a judge call."""
    if _SCRIPT not in line or "--judge" not in line:
        return False
    words = _call_words(line)
    where = f"Job '{job}', step '{step}'"
    assert _runs_script(words) and "--judge" in words, (
        f"{where}: a line names {_SCRIPT} and --judge, but the test cannot read it "
        f"as a judge call. Update backend/tests/{Path(__file__).name} so it can "
        f"read this line: {line.strip()}"
    )
    assert "--summary-only" in words, (
        f"{where}: the judge call has no --summary-only. "
        "The job log is public, so the judge must print only one summary line."
    )
    return True


def _check(workflow_text: str) -> None:
    jobs = _jobs(workflow_text)
    _check_named_step_exists(jobs)
    named_step_calls = 0
    for job, step, run in _steps(jobs):
        # Join the lines that end in a backslash, as the shell does.
        for line in run.replace("\\\n", " ").splitlines():
            if _check_line(job, step, line) and (job, step) == (_JOB, _STEP):
                named_step_calls += 1
    assert named_step_calls, (
        f"Step '{_STEP}' of job '{_JOB}' has no line that runs {_SCRIPT} with --judge. {_WHERE}"
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
    with pytest.raises(
        AssertionError, match=f"Job '{_JOB}', step '{re.escape(_STEP)}': .*no --summary-only"
    ):
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
    with pytest.raises(AssertionError, match="cannot read it as a judge call"):
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


_JUDGE = "python3 -I scripts/ci/redaction_compare.py --judge base.jsonl head.jsonl"


def _with_step(job: str, run: str) -> str:
    """The workflow with one more step, named ``Extra judge``, in ``job``."""
    workflow = cast(dict[str, Any], yaml.safe_load(_ci_text()))
    jobs = workflow["jobs"]
    jobs.setdefault(job, {"runs-on": "ubuntu-latest", "steps": []})
    jobs[job]["steps"].append({"name": "Extra judge", "run": run})
    return cast(str, yaml.safe_dump(workflow, sort_keys=False))


def test_judge_call_in_another_step_fails() -> None:
    with pytest.raises(
        AssertionError, match=f"Job '{_JOB}', step 'Extra judge': the judge call has no"
    ):
        _check(_with_step(_JOB, _JUDGE))


def test_judge_call_in_another_job_fails() -> None:
    with pytest.raises(
        AssertionError, match="Job 'other-job', step 'Extra judge': the judge call has no"
    ):
        _check(_with_step("other-job", _JUDGE))


def test_judge_call_with_flag_in_another_job_passes() -> None:
    _check(_with_step("other-job", f"{_JUDGE} \\\n  --summary-only"))


@pytest.mark.parametrize(
    "run",
    [
        f"time {_JUDGE} --summary-only",
        f"FOO=1 {_JUDGE} --summary-only",
        f"cd backend && {_JUDGE} --summary-only",
    ],
)
def test_judge_call_after_another_word_fails(run: str) -> None:
    with pytest.raises(
        AssertionError,
        match=r"Job 'other-job', step 'Extra judge': .*cannot read it as a judge call"
        r".*Update backend/tests/test_ci_redaction_compare_summary_only\.py",
    ):
        _check(_with_step("other-job", run))


@pytest.mark.parametrize(
    "run",
    [
        f"{_JUDGE} && echo --summary-only",
        f"{_JUDGE} || echo --summary-only",
        f"{_JUDGE}; echo --summary-only",
        f"{_JUDGE} | tee out --summary-only",
        f"{_JUDGE}&&echo --summary-only",
        f"{_JUDGE} # --summary-only",
    ],
)
def test_flag_after_shell_operator_or_comment_fails(run: str) -> None:
    with pytest.raises(AssertionError, match="the judge call has no --summary-only"):
        _check(_with_step(_JOB, run))
