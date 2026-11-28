# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Every workflow job runs on a runner from a short allow list.

GitHub's standard runners are free for a public repository. Larger
runners are billed, also for a public repository. This test reads the
`runs-on` value of every job in `.github/workflows/` and fails when a
name is not on the list, so no workflow picks a paid runner by mistake.

The test reads plain names, lists of names, the expression form
`${{ <condition> && '<name>' || '<name>' }}`, and `${{ matrix.<key> }}`
with its matrix values (also when the `include` list comes from
`fromJSON` of such an expression). Any other value fails the test: a
value the test cannot read does not pass.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

# backend/tests/<this> -> parents[2] == repo root
_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOWS_DIR = _REPO_ROOT / ".github" / "workflows"

# Standard GitHub-hosted runners that the workflow files use (free for a
# public repository).
_GITHUB_HOSTED = frozenset({"ubuntu-latest", "windows-latest", "windows-11-arm"})
# Our own self-hosted runner sets.
_OWN_RUNNER_SETS = frozenset(
    {
        "meho-runners-ci",
        "meho-runners-ci-heavy",
        "meho-runners-ci-heavy-queue",
        "meho-runners-ci-heavy-site2",
    }
)
ALLOWED_RUNNERS = _GITHUB_HOSTED | _OWN_RUNNER_SETS

_FIX_HINT = (
    "Add a name to ALLOWED_RUNNERS in this test only if it is a standard free "
    "GitHub-hosted runner or one of our own runner sets."
)

_EXPRESSION = re.compile(r"\$\{\{\s*(?P<body>.*?)\s*\}\}", re.S)
_TERNARY = re.compile(r"(?P<cond>.+?)\s*&&\s*'(?P<yes>[^']+)'\s*\|\|\s*'(?P<no>[^']+)'", re.S)
_MATRIX_REF = re.compile(r"matrix\.(?P<key>[A-Za-z_][A-Za-z0-9_-]*)")
_FROM_JSON = re.compile(r"fromJSON\((?P<inner>.*)\)", re.S)
_LOCAL_REUSABLE = "./.github/workflows/"


class UnreadableRunsOnError(ValueError):
    """The test cannot work out which runner names a `runs-on` value gives."""


def _strip_groups(text: str) -> str:
    """Remove quoted strings and parenthesised parts, to see the top level."""
    text = re.sub(r"'[^']*'", "''", text)
    previous = None
    while previous != text:
        previous = text
        text = re.sub(r"\([^()]*\)", "_", text)
    return text


def _ternary_names(body: str) -> list[str]:
    """The two names of `<condition> && 'a' || 'b'`.

    The condition must have no `||` at its top level: then `&&` binds
    first and the result is always 'a' or 'b'.
    """
    match = _TERNARY.fullmatch(body.strip())
    if match is None or "||" in _strip_groups(match.group("cond")):
        raise UnreadableRunsOnError(body)
    return [match.group("yes"), match.group("no")]


def _matrix_include(include: Any) -> list[Any]:
    if isinstance(include, list):
        return include
    expr = _EXPRESSION.fullmatch(str(include).strip())
    inner = _FROM_JSON.fullmatch(expr.group("body")) if expr else None
    if inner is None:
        raise UnreadableRunsOnError(f"matrix include {include!r}")
    rows: list[Any] = []
    for literal in _ternary_names(inner.group("inner")):
        parsed = json.loads(literal)
        if not isinstance(parsed, list):
            raise UnreadableRunsOnError(f"matrix include {literal!r}")
        rows.extend(parsed)
    return rows


def _matrix_names(key: str, job: dict[str, Any]) -> list[str]:
    matrix = (job.get("strategy") or {}).get("matrix")
    if not isinstance(matrix, dict):
        raise UnreadableRunsOnError(f"matrix.{key} without a matrix")
    names: list[str] = []
    if key in matrix:
        values = matrix[key]
        if not isinstance(values, list):
            raise UnreadableRunsOnError(f"matrix.{key} = {values!r}")
        names.extend(values)
    for row in _matrix_include(matrix.get("include", [])):
        if isinstance(row, dict) and key in row:
            names.append(row[key])
    if not names or not all(isinstance(n, str) for n in names):
        raise UnreadableRunsOnError(f"matrix.{key} = {names!r}")
    return names


def _string_names(value: str, job: dict[str, Any]) -> list[str]:
    if "${{" not in value:
        return [value]
    expr = _EXPRESSION.fullmatch(value.strip())
    if expr is None:
        raise UnreadableRunsOnError(value)
    body = expr.group("body")
    matrix_ref = _MATRIX_REF.fullmatch(body)
    if matrix_ref is not None:
        return _matrix_names(matrix_ref.group("key"), job)
    return _ternary_names(body)


def runner_names(job: dict[str, Any]) -> list[str]:
    """Every runner name the job's `runs-on` can give."""
    runs_on = job.get("runs-on")
    if isinstance(runs_on, str):
        return _string_names(runs_on, job)
    if isinstance(runs_on, list) and runs_on and all(isinstance(x, str) for x in runs_on):
        return [n for item in runs_on for n in _string_names(item, job)]
    raise UnreadableRunsOnError(repr(runs_on))


def bad_runners(wf: dict[Any, Any]) -> list[str]:
    """The jobs of one workflow whose runner is not on the allow list."""
    bad: list[str] = []
    for name, job in (wf.get("jobs") or {}).items():
        if not isinstance(job, dict):
            bad.append(f"{name}: not a job mapping")
            continue
        if "runs-on" not in job and str(job.get("uses", "")).startswith(_LOCAL_REUSABLE):
            # The called workflow is in the same folder and is checked itself.
            continue
        try:
            names = runner_names(job)
        except (UnreadableRunsOnError, json.JSONDecodeError) as exc:
            bad.append(f"{name}: cannot read runs-on ({exc})")
            continue
        bad.extend(f"{name}: {n!r}" for n in names if n not in ALLOWED_RUNNERS)
    return bad


def _load(path: Path) -> dict[Any, Any]:
    return cast("dict[Any, Any]", yaml.safe_load(path.read_text()))


def _workflow_files() -> list[Path]:
    return sorted(_WORKFLOWS_DIR.glob("*.yml")) + sorted(_WORKFLOWS_DIR.glob("*.yaml"))


def test_workflow_files_found() -> None:
    assert _workflow_files(), f"no workflow files in {_WORKFLOWS_DIR}"


@pytest.mark.parametrize("path", _workflow_files(), ids=lambda p: p.name)
def test_every_job_uses_an_allowed_runner(path: Path) -> None:
    bad = bad_runners(_load(path))
    assert not bad, f"{path.name}: runner not on the allow list: {bad}. {_FIX_HINT}"


def test_a_larger_runner_fails() -> None:
    wf = _load(_WORKFLOWS_DIR / "ci.yml")
    job_name = next(iter(wf["jobs"]))
    wf["jobs"][job_name]["runs-on"] = "ubuntu-latest-16-cores"
    assert bad_runners(wf) == [f"{job_name}: 'ubuntu-latest-16-cores'"]


@pytest.mark.parametrize(
    "runs_on",
    [
        "${{ github.event_name == 'merge_group' && 'meho-runners-ci' || 'ubuntu-24.04-arm' }}",
        ["self-hosted", "meho-runners-ci"],
        "${{ matrix.runner }}",
    ],
    ids=["expression-branch", "list-item", "matrix"],
)
def test_a_name_off_the_list_fails_in_every_form(runs_on: Any) -> None:
    job = {
        "runs-on": runs_on,
        "strategy": {"matrix": {"runner": ["ubuntu-latest", "macos-latest"]}},
    }
    assert bad_runners({"jobs": {"job": job}})


@pytest.mark.parametrize(
    "runs_on",
    [
        "${{ vars.RUNNER }}",
        "${{ github.event_name == 'push' && vars.RUNNER || 'ubuntu-latest' }}",
        "${{ inputs.flag || 'meho-runners-ci' && 'ubuntu-latest' || 'meho-runners-ci' }}",
        "prefix-${{ matrix.runner }}",
        "${{ matrix.missing }}",
        {"group": "large-runners"},
        None,
    ],
    ids=["variable", "variable-branch", "top-level-or", "partial", "no-key", "group", "none"],
)
def test_a_runs_on_it_cannot_read_fails(runs_on: Any) -> None:
    job: dict[str, Any] = {"strategy": {"matrix": {"runner": ["ubuntu-latest"]}}}
    if runs_on is not None:
        job["runs-on"] = runs_on
    bad = bad_runners({"jobs": {"job": job}})
    assert len(bad) == 1 and "cannot read runs-on" in bad[0]


def test_matrix_include_from_json_is_read() -> None:
    include = (
        "${{ fromJSON(vars.X == 'skip' && '[{\"runner\":\"windows-latest\"}]' "
        '|| \'[{"runner":"windows-latest"},{"runner":"windows-11-arm"}]\') }}'
    )
    job = {"runs-on": "${{ matrix.runner }}", "strategy": {"matrix": {"include": include}}}
    assert runner_names(job) == ["windows-latest", "windows-latest", "windows-11-arm"]


def test_an_outside_reusable_workflow_fails() -> None:
    job = {"uses": "some-org/some-repo/.github/workflows/build.yml@v1"}
    assert bad_runners({"jobs": {"job": job}})


def test_a_nested_condition_is_read() -> None:
    runs_on = (
        "${{ (github.event_name == 'merge_group' || (github.event_name == 'pull_request'"
        " && github.event.pull_request.head.repo.full_name != github.repository))"
        " && 'ubuntu-latest' || 'meho-runners-ci' }}"
    )
    assert runner_names({"runs-on": runs_on}) == ["ubuntu-latest", "meho-runners-ci"]
