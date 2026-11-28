# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The judge of the secret-hiding compare (``scripts/ci/redaction_compare.py``).

Each judge test writes a base snapshot and a head snapshot by hand, with
one record, and checks the exit code and what the judge prints. One test runs
the ``--records`` command from ``backend/`` and checks the file it writes.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
import yaml

from tests.redaction_compare.answers import record as answer_record
from tests.redaction_compare.inputs import secret_inputs

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "redaction_compare.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("redaction_compare_script", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


judge_module = _load_script()


def _record(
    hidden: list[str], shown: list[str], sha: str = "a", rest: str = "r"
) -> dict[str, object]:
    return {
        "id": "form.plain.password.bare",
        "path": "redact",
        "hidden": hidden,
        "shown": shown,
        "sha": sha,
        "rest": rest,
    }


def _judge(
    tmp_path: Path,
    base: dict[str, object],
    head: dict[str, object],
    approved: str = "approved: []\n",
    summary_only: bool = False,
) -> int:
    (tmp_path / "base.jsonl").write_text(json.dumps(base) + "\n", encoding="utf-8")
    (tmp_path / "head.jsonl").write_text(json.dumps(head) + "\n", encoding="utf-8")
    (tmp_path / "approved.yaml").write_text(approved, encoding="utf-8")
    return int(
        judge_module.judge(
            tmp_path / "base.jsonl",
            tmp_path / "head.jsonl",
            tmp_path / "approved.yaml",
            summary_only=summary_only,
        )
    )


def test_a_hidden_part_that_is_now_shown_fails(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    code = _judge(tmp_path, _record(["s2"], ["p0"]), _record([], ["p0", "s2"], "b"))
    assert code == 1
    assert "form.plain.password.bare redact: shown:s2" in capfd.readouterr().out


def test_a_plain_part_that_is_now_hidden_fails(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    code = _judge(tmp_path, _record(["s2"], ["p0"]), _record(["p0", "s2"], [], "b"))
    assert code == 1
    assert "form.plain.password.bare redact: hidden:p0" in capfd.readouterr().out


def test_a_text_change_with_the_same_parts_fails(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    code = _judge(tmp_path, _record(["s2"], ["p0"], "a"), _record(["s2"], ["p0"], "b"))
    assert code == 1
    assert "form.plain.password.bare redact: text" in capfd.readouterr().out


def test_a_secret_part_that_is_newly_hidden_passes(tmp_path: Path) -> None:
    assert _judge(tmp_path, _record([], ["p0", "s2"], "a"), _record(["s2"], ["p0"], "b")) == 0


def test_a_newly_hidden_secret_with_another_text_change_fails(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    base = _record([], ["p0", "s2"], "a", "r")
    head = _record(["s2"], ["p0"], "b", "r-without-quotes")
    assert _judge(tmp_path, base, head) == 1
    assert "form.plain.password.bare redact: text" in capfd.readouterr().out


def test_a_newly_hidden_secret_with_another_text_change_can_be_approved(tmp_path: Path) -> None:
    approved = _APPROVED_SHOWN.replace("part: s2", "part: text")
    base = _record([], ["p0", "s2"], "a", "r")
    head = _record(["s2"], ["p0"], "b", "r-without-quotes")
    assert _judge(tmp_path, base, head, approved) == 0


def test_no_difference_passes(tmp_path: Path) -> None:
    assert _judge(tmp_path, _record(["s2"], ["p0"]), _record(["s2"], ["p0"])) == 0


def test_an_input_only_in_the_head_is_not_compared(tmp_path: Path) -> None:
    head = {"id": "other", "path": "redact", "hidden": [], "shown": ["s2"], "sha": "b"}
    (tmp_path / "base.jsonl").write_text(json.dumps(_record([], [])) + "\n", encoding="utf-8")
    (tmp_path / "head.jsonl").write_text(
        json.dumps(_record([], [])) + "\n" + json.dumps(head) + "\n", encoding="utf-8"
    )
    (tmp_path / "approved.yaml").write_text("approved: []\n", encoding="utf-8")
    assert (
        judge_module.judge(
            tmp_path / "base.jsonl", tmp_path / "head.jsonl", tmp_path / "approved.yaml"
        )
        == 0
    )


_APPROVED_SHOWN = """approved:
  - id: form.plain.password.bare
    path: redact
    part: s2
    reason: the old answer hid a part that is plain text
"""


def test_an_approved_difference_passes(tmp_path: Path) -> None:
    code = _judge(
        tmp_path, _record(["s2"], ["p0"]), _record([], ["p0", "s2"], "b"), _APPROVED_SHOWN
    )
    assert code == 0


def test_a_stale_approved_entry_fails(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    code = _judge(tmp_path, _record(["s2"], ["p0"]), _record(["s2"], ["p0"]), _APPROVED_SHOWN)
    assert code == 1
    assert "stale" in capfd.readouterr().out


def test_an_approved_entry_for_another_part_does_not_cover_the_difference(tmp_path: Path) -> None:
    other = _APPROVED_SHOWN.replace("part: s2", "part: s9")
    assert _judge(tmp_path, _record(["s2"], ["p0"]), _record([], ["p0", "s2"], "b"), other) == 1


def test_an_approved_part_does_not_approve_another_change_of_the_same_answer(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    base = _record(["s2"], ["p0"], "a", "r")
    head = _record([], ["p0", "s2"], "b", "r-without-a-field")
    assert _judge(tmp_path, base, head, _APPROVED_SHOWN) == 1
    out = capfd.readouterr().out
    assert "form.plain.password.bare redact: text" in out
    assert "shown:s2" not in out


def test_a_hidden_plain_part_with_another_change_fails_as_text(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    base = _record(["s2"], ["p0"], "a", "r")
    head = _record(["p0", "s2"], [], "b", "r-without-a-field")
    assert _judge(tmp_path, base, head) == 1
    out = capfd.readouterr().out
    assert "form.plain.password.bare redact: hidden:p0" in out
    assert "form.plain.password.bare redact: text" in out


def test_an_approved_part_and_an_approved_text_change_pass(tmp_path: Path) -> None:
    text = _APPROVED_SHOWN.replace("part: s2", "part: text").replace("approved:\n", "")
    base = _record(["s2"], ["p0"], "a", "r")
    head = _record([], ["p0", "s2"], "b", "r-without-a-field")
    assert _judge(tmp_path, base, head, _APPROVED_SHOWN + text) == 0


def test_a_record_removed_from_the_head_fails(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    base = _record([], [])
    other = {**base, "id": "other.input"}
    (tmp_path / "base.jsonl").write_text(
        json.dumps(base) + "\n" + json.dumps(other) + "\n", encoding="utf-8"
    )
    (tmp_path / "head.jsonl").write_text(json.dumps(base) + "\n", encoding="utf-8")
    (tmp_path / "approved.yaml").write_text("approved: []\n", encoding="utf-8")
    code = judge_module.judge(
        tmp_path / "base.jsonl", tmp_path / "head.jsonl", tmp_path / "approved.yaml"
    )
    assert code == 1
    assert "other.input redact: removed" in capfd.readouterr().out


def test_an_empty_head_fails(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "base.jsonl").write_text(json.dumps(_record([], [])) + "\n", encoding="utf-8")
    (tmp_path / "head.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "approved.yaml").write_text("approved: []\n", encoding="utf-8")
    code = judge_module.judge(
        tmp_path / "base.jsonl", tmp_path / "head.jsonl", tmp_path / "approved.yaml"
    )
    assert code == 1
    assert "removed" in capfd.readouterr().out


def test_a_removed_record_can_be_approved(tmp_path: Path) -> None:
    (tmp_path / "base.jsonl").write_text(json.dumps(_record([], [])) + "\n", encoding="utf-8")
    (tmp_path / "head.jsonl").write_text("", encoding="utf-8")
    approved = (
        "approved:\n  - id: form.plain.password.bare\n    path: redact\n"
        "    part: removed\n    reason: the input was merged into another one\n"
    )
    (tmp_path / "approved.yaml").write_text(approved, encoding="utf-8")
    assert (
        judge_module.judge(
            tmp_path / "base.jsonl", tmp_path / "head.jsonl", tmp_path / "approved.yaml"
        )
        == 0
    )


def _judge_with_base_approved(
    tmp_path: Path,
    base: dict[str, object],
    head: dict[str, object],
    approved: str,
    base_approved: str,
) -> int:
    (tmp_path / "base.jsonl").write_text(json.dumps(base) + "\n", encoding="utf-8")
    (tmp_path / "head.jsonl").write_text(json.dumps(head) + "\n", encoding="utf-8")
    (tmp_path / "approved.yaml").write_text(approved, encoding="utf-8")
    (tmp_path / "base-approved.yaml").write_text(base_approved, encoding="utf-8")
    return int(
        judge_module.judge(
            tmp_path / "base.jsonl",
            tmp_path / "head.jsonl",
            tmp_path / "approved.yaml",
            tmp_path / "base-approved.yaml",
        )
    )


_STEPS = sorted({path for item in secret_inputs() for path in item.paths})


def _assert_names_nothing(out: str) -> None:
    record = _record([], [])
    assert str(record["id"]) not in out
    assert not re.search(r"\b(" + "|".join(_STEPS) + r")\b", out)
    assert not re.search(r"\b[sp][0-9]+\b", out)
    assert not re.search(r"[0-9]", out)


def test_the_ci_mode_names_no_input_and_gives_no_number(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    base, head = _record(["s2"], ["p0"]), _record(["p0"], ["s2"], "b", "q")
    code = _judge(tmp_path, base, head, summary_only=True)
    out = capfd.readouterr().out
    assert code == 1
    assert out.splitlines() == [out.strip()]
    assert "not approved" in out
    assert "no difference" not in out
    _assert_names_nothing(out)


def test_the_ci_mode_with_no_difference_passes_with_one_line(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    code = _judge(tmp_path, _record(["s2"], ["p0"]), _record(["s2"], ["p0"]), summary_only=True)
    out = capfd.readouterr().out
    assert code == 0
    assert out == "no difference is left that is not approved\n"
    _assert_names_nothing(out)


def test_the_ci_mode_still_fails_on_a_stale_entry_and_prints_it(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    code = _judge(
        tmp_path,
        _record(["s2"], ["p0"]),
        _record(["s2"], ["p0"]),
        _APPROVED_SHOWN,
        summary_only=True,
    )
    lines = capfd.readouterr().out.splitlines()
    assert code == 1
    assert len(lines) == 2
    assert "stale" in lines[0] and "form.plain.password.bare" in lines[0]
    assert lines[1] == "no difference is left that is not approved"


def test_without_the_ci_mode_the_judge_names_each_difference_and_counts(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    base, head = _record(["s2"], ["p0"]), _record(["p0"], ["s2"], "b", "q")
    assert _judge(tmp_path, base, head) == 1
    assert capfd.readouterr().out.splitlines() == [
        "form.plain.password.bare redact: shown:s2",
        "form.plain.password.bare redact: hidden:p0",
        "form.plain.password.bare redact: text",
        "3 difference(s), 3 not approved",
    ]


def test_the_command_line_takes_the_ci_mode(tmp_path: Path) -> None:
    base, head = _record(["s2"], ["p0"]), _record([], ["p0", "s2"], "b")
    _judge(tmp_path, base, head)
    done = subprocess.run(
        [
            sys.executable,
            "-I",
            str(_SCRIPT),
            "--judge",
            str(tmp_path / "base.jsonl"),
            str(tmp_path / "head.jsonl"),
            "--approved",
            str(tmp_path / "approved.yaml"),
            "--summary-only",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 1, done.stderr
    assert done.stdout.count("\n") == 1
    _assert_names_nothing(done.stdout)


def test_a_merged_entry_that_matches_no_difference_passes(tmp_path: Path) -> None:
    same = _record(["s2"], ["p0"])
    code = _judge_with_base_approved(tmp_path, same, same, _APPROVED_SHOWN, _APPROVED_SHOWN)
    assert code == 0


def test_a_new_entry_that_matches_no_difference_fails_as_stale(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    same = _record(["s2"], ["p0"])
    code = _judge_with_base_approved(tmp_path, same, same, _APPROVED_SHOWN, "approved: []\n")
    assert code == 1
    assert "stale" in capfd.readouterr().out


def test_an_entry_only_in_the_base_file_does_not_approve_a_new_difference(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    base, head = _record(["s2"], ["p0"]), _record([], ["p0", "s2"], "b")
    code = _judge_with_base_approved(tmp_path, base, head, "approved: []\n", _APPROVED_SHOWN)
    assert code == 1
    assert "form.plain.password.bare redact: shown:s2" in capfd.readouterr().out


def test_a_missing_base_approved_file_counts_as_empty(tmp_path: Path) -> None:
    for name, record in (
        ("base.jsonl", _record(["s2"], ["p0"])),
        ("head.jsonl", _record([], ["p0", "s2"], "b")),
    ):
        (tmp_path / name).write_text(json.dumps(record) + "\n", encoding="utf-8")
    (tmp_path / "approved.yaml").write_text(_APPROVED_SHOWN, encoding="utf-8")
    code = judge_module.judge(
        tmp_path / "base.jsonl",
        tmp_path / "head.jsonl",
        tmp_path / "approved.yaml",
        tmp_path / "no-such-file.yaml",
    )
    assert code == 0


def _flag_records(head_text: str) -> tuple[dict[str, object], dict[str, object]]:
    """The base and head records of the real input ``flag.plain.password.bare`` on ``redact``."""
    item = next(i for i in secret_inputs() if i.id == "flag.plain.password.bare")
    label, secret, name = (p.value for p in item.parts)
    base_text = json.dumps(item.payload)
    assert base_text == json.dumps(f"tool --{label} {secret} --name {name}")
    head = head_text.format(label=label, name=name)
    return answer_record(item, "redact", base_text), answer_record(item, "redact", head)


def test_a_flag_name_hidden_with_its_value_fails(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    base, head = _flag_records('"tool --[REDACTED:{label}] --name {name}"')
    assert _judge(tmp_path, base, head) == 1
    assert "flag.plain.password.bare redact: hidden:p0" in capfd.readouterr().out


def test_a_flag_name_hidden_with_its_value_passes_with_its_own_entry(tmp_path: Path) -> None:
    base, head = _flag_records('"tool --[REDACTED:{label}] --name {name}"')
    approved = """approved:
  - id: flag.plain.password.bare
    path: redact
    part: p0
    reason: the flag name is hidden with its value
"""
    assert _judge(tmp_path, base, head, approved) == 0


def test_a_flag_value_hidden_alone_passes(tmp_path: Path) -> None:
    base, head = _flag_records('"tool --{label} [REDACTED:{label}] --name {name}"')
    assert head["hidden"] == ["s1"]
    assert _judge(tmp_path, base, head) == 0


def test_the_records_command_writes_one_record_per_input_and_step(tmp_path: Path) -> None:
    target = tmp_path / "records.jsonl"
    done = subprocess.run(
        [sys.executable, str(_SCRIPT), "--records", str(target)],
        cwd=_SCRIPT.parents[2] / "backend",
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    records = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]
    keys = [(r["id"], r["path"]) for r in records]
    expected = sorted((item.id, path) for item in secret_inputs() for path in item.paths)
    assert sorted(keys) == expected
    assert len(set(keys)) == len(keys)
    digest = re.compile(r"[0-9a-f]{64}")
    for r in records:
        assert set(r) == {"id", "path", "hidden", "shown", "sha", "rest"}
        assert all(isinstance(part, str) for part in r["hidden"] + r["shown"])
        assert digest.fullmatch(r["sha"]) and digest.fullmatch(r["rest"])
    assert judge_module.differences(judge_module.load(target), judge_module.load(target)) == []


_PACKAGE_FILE = "backend/tests/redaction_compare/answers.py"
_CI_YML = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"


def _judge_changed(tmp_path: Path, changed: list[str], approved: str = "approved: []\n") -> int:
    record = _record([], ["p0"])
    (tmp_path / "base.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
    (tmp_path / "head.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
    (tmp_path / "approved.yaml").write_text(approved, encoding="utf-8")
    return int(
        judge_module.judge(
            tmp_path / "base.jsonl",
            tmp_path / "head.jsonl",
            tmp_path / "approved.yaml",
            changed=changed,
        )
    )


def test_a_changed_file_of_the_compare_package_fails(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    assert _judge_changed(tmp_path, [_PACKAGE_FILE]) == 1
    assert f"compare-package {_PACKAGE_FILE}: changed" in capfd.readouterr().out


def test_a_changed_file_of_the_compare_package_with_its_own_entry_passes(tmp_path: Path) -> None:
    entry = (
        "approved:\n"
        "  - id: compare-package\n"
        f"    path: {_PACKAGE_FILE}\n"
        "    part: changed\n"
        "    reason: the input set grows\n"
    )
    assert _judge_changed(tmp_path, [_PACKAGE_FILE], entry) == 0


def test_an_entry_for_one_package_file_does_not_cover_another(tmp_path: Path) -> None:
    entry = (
        "approved:\n"
        "  - id: compare-package\n"
        "    path: backend/tests/redaction_compare/inputs.py\n"
        "    part: changed\n"
        "    reason: the input set grows\n"
    )
    assert _judge_changed(tmp_path, [_PACKAGE_FILE], entry) == 1


def test_no_changed_file_passes(tmp_path: Path) -> None:
    assert _judge_changed(tmp_path, []) == 0


def test_a_package_change_that_makes_a_shown_part_count_as_hidden_fails(tmp_path: Path) -> None:
    # The pull request records look like the base records, because the changed
    # package reads the same answer as hidden. The file list still fails the job.
    base = _record(["s2"], ["p0"])
    (tmp_path / "base.jsonl").write_text(json.dumps(base) + "\n", encoding="utf-8")
    (tmp_path / "head.jsonl").write_text(json.dumps(base) + "\n", encoding="utf-8")
    (tmp_path / "approved.yaml").write_text("approved: []\n", encoding="utf-8")
    (tmp_path / "changed.txt").write_text(_PACKAGE_FILE + "\0", encoding="utf-8")
    command = [
        sys.executable,
        "-I",
        str(_SCRIPT),
        "--judge",
        str(tmp_path / "base.jsonl"),
        str(tmp_path / "head.jsonl"),
        "--changed",
        str(tmp_path / "changed.txt"),
        "--approved",
        str(tmp_path / "approved.yaml"),
        "--summary-only",
    ]
    done = subprocess.run(command, capture_output=True, text=True, check=False)
    assert done.returncode == 1, done.stdout
    _assert_names_nothing(done.stdout)


def _compare_step_text() -> str:
    text = _CI_YML.read_text(encoding="utf-8")
    start = text.index("  redaction-compare:\n")
    return text[start : text.index("\n  connector-package-template:", start)]


def test_the_job_lists_the_changed_package_files_from_git() -> None:
    step = _compare_step_text()
    assert "git diff --name-only --no-renames -z" in step
    assert "-- backend/tests/__init__.py backend/tests/redaction_compare >" in step
    assert '--changed "${dir}/changed.txt"' in step
    assert '"${changed_args[@]}"' in step


def test_the_job_keeps_the_container_error_output_out_of_the_log() -> None:
    step = _compare_step_text()
    # The error output of the container goes to a file, and a failure prints one fixed line.
    assert '> "${dir}/head.jsonl" 2> "${dir}/head.err"; then' in step
    assert "if ! tar -c" in step
    assert "could not be built" in step
    assert "exit 1" in step
    # Nothing prints that file.
    assert not re.search(r"(cat|tail|head|echo|<)\s+\"?\$\{dir\}/head\.err", step)
    assert "head.err" not in step.replace('2> "${dir}/head.err"', "")


def test_the_job_pulls_the_image_in_its_own_step_before_the_record_build() -> None:
    """A failed pull is retried and named; it never shows up as a tar error in the build."""
    job = yaml.safe_load(_CI_YML.read_text(encoding="utf-8"))["jobs"]["redaction-compare"]
    names = [step.get("name") for step in job["steps"]]
    pull = names.index("Pull the Python image for the record build")
    build = names.index("Build and judge the records of the base and of the pull request")
    assert pull < build
    pull_run = job["steps"][pull]["run"]
    pull_line = 'retry.sh" docker pull --quiet "${MEHO_TEST_PYTHON_IMAGE:-${IMAGE}}"'
    assert pull_line in pull_run
    assert "SETUP FAILURE, not a test failure: the Python image did not pull" in pull_run
    assert "docker run --rm -i --pull never " in job["steps"][build]["run"]


_DOCS = Path(__file__).resolve().parents[2] / "docs" / "codebase" / "redaction.md"
_DIFF_LINE = re.compile(r"^\s*(git diff --name-only[^\n]*)$", re.MULTILINE)


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout.strip()


def _changed_list(tmp_path: Path) -> Path:
    """Run the job's own ``git diff`` line on a commit that moves and adds package files.

    The base has ``answers.py``; the pull request moves it to ``reading.py``
    (same content, so git sees a rename) and adds ``entrées.py``.
    """
    repo = tmp_path / "repo"
    package = repo / "backend" / "tests" / "redaction_compare"
    package.mkdir(parents=True)
    _git(repo, "init", "-q")
    (package / "answers.py").write_text("ANSWERS = 1\n" * 20, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    _git(
        repo,
        "mv",
        "backend/tests/redaction_compare/answers.py",
        "backend/tests/redaction_compare/reading.py",
    )
    (package / "entrées.py").write_text("X = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "move")
    head = _git(repo, "rev-parse", "HEAD")
    match = _DIFF_LINE.search(_compare_step_text())
    assert match is not None
    out = tmp_path / "out"
    out.mkdir()
    subprocess.run(
        ["bash", "-euo", "pipefail", "-c", match.group(1)],
        cwd=repo,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "base": base, "head": head, "dir": str(out)},
        check=True,
    )
    return out / "changed.txt"


def _judge_command(tmp_path: Path, changed: Path, approved: str) -> int:
    record = _record([], ["p0"])
    (tmp_path / "base.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
    (tmp_path / "head.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
    (tmp_path / "approved.yaml").write_text(approved, encoding="utf-8")
    command = [
        sys.executable,
        "-I",
        str(_SCRIPT),
        "--judge",
        str(tmp_path / "base.jsonl"),
        str(tmp_path / "head.jsonl"),
        "--changed",
        str(changed),
        "--approved",
        str(tmp_path / "approved.yaml"),
    ]
    return subprocess.run(command, capture_output=True, text=True, check=False).returncode


def _entries(*paths: str) -> str:
    lines = ["approved:"]
    for path in paths:
        lines += [
            "  - id: compare-package",
            f"    path: {path}",
            "    part: changed",
            "    reason: the package is reorganised",
        ]
    return "\n".join(lines) + "\n"


_OLD = "backend/tests/redaction_compare/answers.py"
_NEW = "backend/tests/redaction_compare/reading.py"
_ACCENTED = "backend/tests/redaction_compare/entrées.py"


def test_the_job_lists_both_paths_of_a_moved_file_and_keeps_accented_names(
    tmp_path: Path,
) -> None:
    changed = _changed_list(tmp_path)
    names = changed.read_text(encoding="utf-8").split("\0")
    assert sorted(name for name in names if name) == sorted([_OLD, _NEW, _ACCENTED])


def test_a_moved_file_needs_an_entry_for_its_old_path_too(tmp_path: Path) -> None:
    changed = _changed_list(tmp_path)
    assert _judge_command(tmp_path, changed, _entries(_NEW, _ACCENTED)) == 1


def test_entries_with_the_exact_names_approve_a_move_and_an_accented_file(
    tmp_path: Path,
) -> None:
    changed = _changed_list(tmp_path)
    assert _judge_command(tmp_path, changed, _entries(_OLD, _NEW, _ACCENTED)) == 0


_TESTS_INIT = "backend/tests/__init__.py"


def _changed_list_for_tests_init(tmp_path: Path) -> Path:
    """Run the job's own ``git diff`` line on a commit that changes only the tests package init."""
    repo = tmp_path / "repo"
    package = repo / "backend" / "tests" / "redaction_compare"
    package.mkdir(parents=True)
    _git(repo, "init", "-q")
    (repo / _TESTS_INIT).write_text("", encoding="utf-8")
    (package / "answers.py").write_text("ANSWERS = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / _TESTS_INIT).write_text("import os\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "edit the tests package")
    head = _git(repo, "rev-parse", "HEAD")
    match = _DIFF_LINE.search(_compare_step_text())
    assert match is not None
    out = tmp_path / "out"
    out.mkdir()
    subprocess.run(
        ["bash", "-euo", "pipefail", "-c", match.group(1)],
        cwd=repo,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "base": base, "head": head, "dir": str(out)},
        check=True,
    )
    return out / "changed.txt"


def test_the_job_lists_a_change_of_the_tests_package_init(tmp_path: Path) -> None:
    changed = _changed_list_for_tests_init(tmp_path)
    assert changed.read_bytes() == _TESTS_INIT.encode() + b"\0"


def test_a_change_of_the_tests_package_init_fails_without_an_entry(tmp_path: Path) -> None:
    changed = _changed_list_for_tests_init(tmp_path)
    assert _judge_command(tmp_path, changed, "approved: []\n") == 1


def test_a_change_of_the_tests_package_init_passes_with_its_own_entry(tmp_path: Path) -> None:
    changed = _changed_list_for_tests_init(tmp_path)
    assert _judge_command(tmp_path, changed, _entries(_TESTS_INIT)) == 0


def _local_block() -> str:
    text = _DOCS.read_text(encoding="utf-8")
    block = text[text.index("- **See the differences locally.**") :]
    block = block[block.index("```bash\n") + len("```bash\n") :]
    return block[: block.index("\n  ```\n")] + "\n"


def test_the_local_command_passes_the_changed_package_files_like_the_job() -> None:
    block = _local_block()
    match = _DIFF_LINE.search(block)
    assert match is not None
    assert "--name-only --no-renames -z origin/main HEAD" in match.group(1)
    assert (
        '-- backend/tests/__init__.py backend/tests/redaction_compare > "$tmp/changed.txt"'
        in match.group(1)
    )
    assert 'changed_args=(--changed "$tmp/changed.txt")' in block
    assert "grep -q -e '--changed' \"$rules\"" in block


# --- the local command, run end to end on a small repo ---------------------

_RECORDS = """def records():
    return [{"id": "one", "path": "redact", "hidden": ["s1"], "shown": ["p0"],
             "sha": "a", "rest": "r"}]
"""
_UV = """#!/bin/sh
case "$1" in
  sync) exit 0 ;;
  run) shift; exec "$@" ;;
esac
exit 2
"""


def _old_judge() -> str:
    """The judge without the --changed option, as a base branch can still have it."""
    text = _SCRIPT.read_text(encoding="utf-8")
    assert 'parser.add_argument("--changed")' in text
    return text.replace("--changed", "--not-known").replace("args.changed", "args.not_known")


def _local_repo(
    tmp_path: Path, judge: str, edit: str = "backend/tests/redaction_compare/answers.py"
) -> tuple[Path, Path]:
    """A main branch with the judge, a clone, and a branch that edits the file ``edit``."""
    main = tmp_path / "main"
    package = main / "backend" / "tests" / "redaction_compare"
    package.mkdir(parents=True)
    (package / "answers.py").write_text(_RECORDS, encoding="utf-8")
    (main / _TESTS_INIT).write_text("", encoding="utf-8")
    snapshots = main / "backend" / "tests" / "snapshots"
    snapshots.mkdir()
    (snapshots / "redaction_compare_approved.yaml").write_text("approved: []\n", "utf-8")
    (main / "scripts" / "ci").mkdir(parents=True)
    (main / "scripts" / "ci" / "redaction_compare.py").write_text(judge, encoding="utf-8")
    _git(main, "init", "-q", "-b", "main")
    _git(main, "add", "-A")
    _git(main, "commit", "-q", "-m", "base")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(main), str(clone))
    _git(clone, "checkout", "-q", "-b", "change")
    edited = clone / edit
    edited.write_text(edited.read_text(encoding="utf-8") + "# read the answers\n", "utf-8")
    _git(clone, "commit", "-q", "-am", "edit")
    return main, clone


def _run_local_block(tmp_path: Path, clone: Path) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "uv").write_text(_UV, encoding="utf-8")
    (bin_dir / "uv").chmod(0o755)
    for name in ("python", "python3"):
        if not (bin_dir / name).exists():
            (bin_dir / name).symlink_to(sys.executable)
    return subprocess.run(
        ["bash", "-c", _local_block()],
        cwd=clone,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin:/usr/local/bin", "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_local_command_with_a_judge_that_knows_changed_lists_the_package_file(
    tmp_path: Path,
) -> None:
    _, clone = _local_repo(tmp_path, _SCRIPT.read_text(encoding="utf-8"))
    done = _run_local_block(tmp_path, clone)
    assert done.returncode == 1, done.stderr
    assert "compare-package backend/tests/redaction_compare/answers.py: changed" in done.stdout


def test_the_local_command_lists_a_change_of_the_tests_package_init(tmp_path: Path) -> None:
    _, clone = _local_repo(tmp_path, _SCRIPT.read_text(encoding="utf-8"), edit=_TESTS_INIT)
    done = _run_local_block(tmp_path, clone)
    assert done.returncode == 1, done.stderr
    assert f"compare-package {_TESTS_INIT}: changed" in done.stdout
    approved = clone / "backend" / "tests" / "snapshots" / "redaction_compare_approved.yaml"
    approved.write_text(_entries(_TESTS_INIT), encoding="utf-8")
    done = _run_local_block(tmp_path, clone)
    assert done.returncode == 0, done.stdout + done.stderr


def test_the_local_command_with_an_older_judge_runs_without_changed(tmp_path: Path) -> None:
    _, clone = _local_repo(tmp_path, _old_judge())
    done = _run_local_block(tmp_path, clone)
    assert "unrecognized arguments" not in done.stderr
    assert done.returncode == 0, done.stderr
    assert "0 difference(s), 0 not approved" in done.stdout


def test_the_local_command_stops_when_main_moved_and_uses_the_trees_of_ci_after_a_merge(
    tmp_path: Path,
) -> None:
    main, clone = _local_repo(tmp_path, _SCRIPT.read_text(encoding="utf-8"))
    # Main picks up the same edit on its own: the merged tree does not change the file.
    answers = main / "backend" / "tests" / "redaction_compare" / "answers.py"
    answers.write_text(_RECORDS + "# read the answers\n", encoding="utf-8")
    _git(main, "commit", "-q", "-am", "same edit on main")
    done = _run_local_block(tmp_path, clone)
    assert done.returncode == 1
    assert "merge origin/main into this branch first" in done.stderr
    assert "compare-package" not in done.stdout
    _git(clone, "merge", "-q", "--no-edit", "origin/main")
    done = _run_local_block(tmp_path, clone)
    assert done.returncode == 0, done.stderr
    assert "0 difference(s), 0 not approved" in done.stdout


# --- the exact bytes of each path ------------------------------------------

_PACKAGE = b"backend/tests/redaction_compare/"


def test_distinct_paths_keep_distinct_spellings() -> None:
    names = [
        b"a\rb.py",
        b"a\nb.py",
        b"a\xffb.py",
        b"a\\xffb.py",
        b"a\\u000ab.py",
        b"a b.py",
        "a\u00e9.py".encode(),
        "ae\u0301.py".encode(),
    ]
    spelled = [judge_module.path_name(name) for name in names]
    assert len(set(spelled)) == len(names)
    for one in spelled:
        assert one.isprintable() and not any(letter.isspace() for letter in one)
    assert judge_module.path_name("entrées.py".encode()) == "entrées.py"
    assert judge_module.path_name(b"a\rb.py") == "a\\u000db.py"
    assert judge_module.path_name(b"a\xffb.py") == "a\\xffb.py"


def _write_changed(tmp_path: Path, *names: bytes) -> Path:
    target = tmp_path / "changed.txt"
    target.write_bytes(b"".join(_PACKAGE + name + b"\0" for name in names))
    return target


def test_paths_that_differ_in_a_carriage_return_need_distinct_entries(tmp_path: Path) -> None:
    changed = _write_changed(tmp_path, b"a\rb.py", b"a\nb.py")
    names = judge_module.read_changed(changed)
    assert len(set(names)) == 2
    assert _judge_command(tmp_path, changed, _entries(names[0])) == 1
    assert _judge_command(tmp_path, changed, _entries(names[1])) == 1
    assert _judge_command(tmp_path, changed, _entries(*names)) == 0


def test_a_path_that_is_not_utf8_is_judged_and_needs_its_own_entry(tmp_path: Path) -> None:
    changed = _write_changed(tmp_path, b"a\xffb.py", b"a\\xffb.py")
    names = judge_module.read_changed(changed)
    assert names == [
        "backend/tests/redaction_compare/a\\xffb.py",
        "backend/tests/redaction_compare/a\\\\xffb.py",
    ]
    assert _judge_command(tmp_path, changed, _entries(names[0])) == 1
    assert _judge_command(tmp_path, changed, _entries(*names)) == 0


@pytest.mark.skipif(sys.platform != "linux", reason="needs file names that are not UTF-8")
def test_the_job_lists_exact_bytes_from_git_for_unusual_names(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    package = repo / "backend" / "tests" / "redaction_compare"
    package.mkdir(parents=True)
    _git(repo, "init", "-q")
    (package / "keep.py").write_text("X = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    raw = [b"a\rb.py", b"a\nb.py", b"a\xffb.py"]
    for name in raw:
        with open(os.path.join(os.fsencode(package), name), "wb") as handle:
            handle.write(b"X = 2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add")
    head = _git(repo, "rev-parse", "HEAD")
    match = _DIFF_LINE.search(_compare_step_text())
    assert match is not None
    out = tmp_path / "out"
    out.mkdir()
    subprocess.run(
        ["bash", "-euo", "pipefail", "-c", match.group(1)],
        cwd=repo,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "base": base, "head": head, "dir": str(out)},
        check=True,
    )
    names = judge_module.read_changed(out / "changed.txt")
    assert sorted(names) == sorted(judge_module.path_name(_PACKAGE + name) for name in raw)
    assert len(set(names)) == 3
