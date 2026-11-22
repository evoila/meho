# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The judge of the secret-hiding compare (``scripts/ci/redaction_compare.py``).

Each test writes a base snapshot and a head snapshot by hand, with one
record, and checks the exit code and what the judge prints.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

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
) -> int:
    (tmp_path / "base.jsonl").write_text(json.dumps(base) + "\n", encoding="utf-8")
    (tmp_path / "head.jsonl").write_text(json.dumps(head) + "\n", encoding="utf-8")
    (tmp_path / "approved.yaml").write_text(approved, encoding="utf-8")
    return int(
        judge_module.judge(
            tmp_path / "base.jsonl", tmp_path / "head.jsonl", tmp_path / "approved.yaml"
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
