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


def _record(hidden: list[str], shown: list[str], sha: str = "a") -> dict[str, object]:
    return {
        "id": "form.plain.password.bare",
        "path": "redact",
        "hidden": hidden,
        "shown": shown,
        "sha": sha,
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


def test_no_difference_passes(tmp_path: Path) -> None:
    assert _judge(tmp_path, _record(["s2"], ["p0"]), _record(["s2"], ["p0"])) == 0


def test_an_input_only_in_the_head_is_not_compared(tmp_path: Path) -> None:
    base = {"id": "other", "path": "redact", "hidden": [], "shown": [], "sha": "a"}
    assert _judge(tmp_path, base, _record([], ["p0", "s2"], "b")) == 0


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
