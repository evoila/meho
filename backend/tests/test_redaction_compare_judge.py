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
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

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
    (tmp_path / "changed.txt").write_text(_PACKAGE_FILE + "\n", encoding="utf-8")
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
    assert "git diff --name-only" in step
    assert "backend/tests/redaction_compare" in step
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
