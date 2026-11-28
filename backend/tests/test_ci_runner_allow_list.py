# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
# The allow list holds whole workflow lines word for word, so some are long.
# ruff: noqa: E501

"""Every workflow job runs on a runner from a short allow list.

GitHub's standard runners are free for a public repository. Larger
runners are billed, also for a public repository. This test reads each
file in `.github/workflows/` as text. Every line that has `runs-on`
outside a comment must be, word for word, one of the texts in
ALLOWED_RUNS_ON. The test does not work out what an expression gives:
a new form, or a changed one, fails until someone adds its exact text
to the list on purpose.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# backend/tests/<this> -> parents[2] == repo root
_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOWS_DIR = _REPO_ROOT / ".github" / "workflows"

_MATRIX_RUNS_ON = "runs-on: ${{ matrix.runner }}"

# Each text is a whole `runs-on:` line as it stands in the workflow
# files, without its leading spaces. Every name in them is a standard
# free GitHub-hosted runner or one of our own runner sets.
ALLOWED_RUNS_ON = frozenset(
    {
        "runs-on: ubuntu-latest",
        "runs-on: meho-runners-ci",
        "runs-on: meho-runners-ci-heavy",
        "runs-on: meho-runners-ci-heavy-site2",
        "runs-on: ${{ github.event_name == 'merge_group' && 'meho-runners-ci' || 'ubuntu-latest' }}",
        "runs-on: ${{ github.event_name == 'merge_group' && 'meho-runners-ci-heavy-queue' || 'meho-runners-ci-heavy' }}",
        "runs-on: ${{ github.event_name == 'merge_group' && 'meho-runners-ci-heavy-queue' || 'ubuntu-latest' }}",
        "runs-on: ${{ github.event_name == 'pull_request' && 'ubuntu-latest' || 'meho-runners-ci' }}",
        "runs-on: ${{ github.run_attempt == '1' && 'meho-runners-ci-heavy-site2' || 'meho-runners-ci-heavy' }}",
        "runs-on: ${{ github.run_attempt == '1' && 'ubuntu-latest' || 'meho-runners-ci-heavy' }}",
        "runs-on: ${{ (github.event_name == 'pull_request' && github.event.pull_request.head.repo.full_name != github.repository) && 'ubuntu-latest' || 'meho-runners-ci' }}",
        "runs-on: ${{ (github.event_name == 'merge_group' || (github.event_name == 'pull_request' && github.event.pull_request.head.repo.full_name != github.repository)) && 'ubuntu-latest' || 'meho-runners-ci' }}",
        _MATRIX_RUNS_ON,
    }
)

# The one allowed matrix for `runs-on: ${{ matrix.runner }}`: the whole
# matrix of the job is this line. Every row in it has a `runner` key
# with a plain allowed name (`windows-latest` or `windows-11-arm`).
ALLOWED_MATRIX_INCLUDE = frozenset(
    {
        "include: ${{ fromJSON(vars.MEHO_WINDOWS_ARM_CHECK == 'skip' && "
        '\'[{"arch":"amd64","runner":"windows-latest"}]\' || '
        '\'[{"arch":"amd64","runner":"windows-latest"},'
        '{"arch":"arm64","runner":"windows-11-arm"}]\') }}',
    }
)

_LOCAL_REUSABLE = "./.github/workflows/"

_FIX_HINT = (
    "Add a text to ALLOWED_RUNS_ON in this test only if every runner name in it is "
    "a standard free GitHub-hosted runner or one of our own runner sets."
)


def _code(line: str) -> str:
    """The line without a comment, so `runs-on` in a comment is not checked."""
    return line.split("#", 1)[0]


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _is_content(line: str) -> bool:
    return bool(_code(line).strip())


def _job_lines(lines: list[str], index: int) -> list[int]:
    """The line numbers (0-based) of the job that holds line `index`."""
    key_indent = _indent(lines[index])
    start = index
    while start > 0 and not (_is_content(lines[start]) and _indent(lines[start]) < key_indent):
        start -= 1
    job_indent = _indent(lines[start])
    end = start + 1
    while end < len(lines) and not (_is_content(lines[end]) and _indent(lines[end]) <= job_indent):
        end += 1
    return list(range(start, end))


def _matrix_lines(lines: list[str], job: list[int]) -> list[str] | None:
    """The stripped lines under the job's `matrix:` key, or None without one."""
    for pos, i in enumerate(job):
        if _code(lines[i]).strip() == "matrix:":
            indent = _indent(lines[i])
            body: list[str] = []
            for j in job[pos + 1 :]:
                if not _is_content(lines[j]):
                    continue
                if _indent(lines[j]) <= indent:
                    break
                body.append(lines[j].strip())
            return body
    return None


def bad_lines(file_name: str, text: str) -> list[str]:
    """Each `runs-on` line of one workflow file that is not on the allow list."""
    lines = text.splitlines()
    bad: list[str] = []
    for index, line in enumerate(lines):
        code = _code(line)
        where = f"{file_name}:{index + 1}"
        stripped = line.strip()
        if "runs-on" in code and stripped not in ALLOWED_RUNS_ON:
            bad.append(f"{where}: {stripped!r}")
        elif stripped == _MATRIX_RUNS_ON:
            matrix = _matrix_lines(lines, _job_lines(lines, index))
            if matrix is None or len(matrix) != 1 or matrix[0] not in ALLOWED_MATRIX_INCLUDE:
                bad.append(f"{where}: matrix not on the allow list: {matrix!r}")
        value = code.strip().removeprefix("- ")
        if value.startswith("uses:"):
            target = value.removeprefix("uses:").strip().strip("'\"")
            if ".github/workflows/" in target and not target.startswith(_LOCAL_REUSABLE):
                bad.append(f"{where}: reusable workflow from another repository: {target!r}")
    return bad


def _workflow_files() -> list[Path]:
    return sorted(_WORKFLOWS_DIR.glob("*.yml")) + sorted(_WORKFLOWS_DIR.glob("*.yaml"))


def _read(name: str) -> str:
    return (_WORKFLOWS_DIR / name).read_text()


def test_workflow_files_found() -> None:
    assert _workflow_files(), f"no workflow files in {_WORKFLOWS_DIR}"


@pytest.mark.parametrize("path", _workflow_files(), ids=lambda p: p.name)
def test_every_job_uses_an_allowed_runner(path: Path) -> None:
    bad = bad_lines(path.name, path.read_text())
    assert not bad, f"runs-on not on the allow list: {bad}. {_FIX_HINT}"


def test_a_larger_runner_fails() -> None:
    text = _read("ci.yml").replace("runs-on: ubuntu-latest", "runs-on: ubuntu-latest-16-cores", 1)
    bad = bad_lines("ci.yml", text)
    assert len(bad) == 1 and "'runs-on: ubuntu-latest-16-cores'" in bad[0]


def test_a_text_changed_by_one_character_fails() -> None:
    old = "&& 'meho-runners-ci' || 'ubuntu-latest' }}"
    text = _read("ci.yml")
    assert old in text
    bad = bad_lines("ci.yml", text.replace(old, "&& 'meho-runners-ci' || 'ubuntu-latest'  }}", 1))
    assert len(bad) == 1


def test_a_new_file_with_a_text_off_the_list_fails() -> None:
    text = "on: push\njobs:\n  build:\n    runs-on: macos-latest\n    steps: []\n"
    assert bad_lines("new.yml", text) == ["new.yml:4: 'runs-on: macos-latest'"]


def test_a_runner_made_of_two_expressions_fails() -> None:
    text = (
        "jobs:\n  build:\n"
        "    runs-on: ${{ vars.PREFIX }}-${{ inputs.windows && 'windows-latest' || 'ubuntu-latest' }}\n"
    )
    assert len(bad_lines("new.yml", text)) == 1


def test_a_matrix_row_without_a_runner_fails() -> None:
    old = '{"arch":"arm64","runner":"windows-11-arm"}'
    text = _read("cli-release.yml")
    assert old in text
    bad = bad_lines("cli-release.yml", text.replace(old, '{"arch":"arm64"}', 1))
    assert len(bad) == 1 and "matrix not on the allow list" in bad[0]


def test_a_matrix_runner_without_the_allowed_matrix_fails() -> None:
    text = (
        "jobs:\n  build:\n    runs-on: ${{ matrix.runner }}\n"
        "    strategy:\n      matrix:\n        runner: [ubuntu-latest, macos-latest]\n"
    )
    assert len(bad_lines("new.yml", text)) == 1


def test_jobs_named_on_and_yes_are_both_checked() -> None:
    # A YAML 1.1 reader turns both job names into True and keeps one job.
    text = "jobs:\n  on:\n    runs-on: macos-latest\n  yes:\n    runs-on: ubuntu-latest\n"
    assert bad_lines("new.yml", text) == ["new.yml:3: 'runs-on: macos-latest'"]


def test_a_runs_on_over_more_lines_fails() -> None:
    text = "jobs:\n  build:\n    runs-on:\n      - self-hosted\n      - meho-runners-ci\n"
    assert bad_lines("new.yml", text) == ["new.yml:3: 'runs-on:'"]


def test_runs_on_in_a_comment_is_not_checked() -> None:
    text = "jobs:\n  build:\n    # runs-on: macos-latest\n    runs-on: ubuntu-latest\n"
    assert bad_lines("new.yml", text) == []


def test_an_outside_reusable_workflow_fails() -> None:
    text = "jobs:\n  build:\n    uses: some-org/some-repo/.github/workflows/build.yml@v1\n"
    assert len(bad_lines("new.yml", text)) == 1


def test_a_local_reusable_workflow_passes() -> None:
    text = "jobs:\n  build:\n    uses: ./.github/workflows/linux-download.yml\n"
    assert bad_lines("new.yml", text) == []
