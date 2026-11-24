# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The judge of the approval table (``scripts/ci/approval_table_judge.py``).

Each test writes a base table and a head table by hand and checks the exit
code and what the judge prints. The file has no database and no connector.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "approval_table_judge.py"
_spec = importlib.util.spec_from_file_location("approval_table_judge_script", _SCRIPT)
assert _spec is not None and _spec.loader is not None
judge_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(judge_module)


def _row(op_id: str = "vault.kv.put", **changes: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "op_id": op_id,
        "connector_id": "vault-1.x",
        "class": "credential_write",
        "risk_level": "dangerous",
        "needs_approval": True,
        "never_covered": "credential",
    }
    row.update(changes)
    return row


def _judge(
    tmp_path: Path,
    base: list[dict[str, Any]],
    head: list[dict[str, Any]],
    approved: str = "approved: []\n",
    base_approved: str | None = None,
) -> int:
    (tmp_path / "base.json").write_text(json.dumps(base), encoding="utf-8")
    (tmp_path / "head.json").write_text(json.dumps(head), encoding="utf-8")
    (tmp_path / "approved.yaml").write_text(approved, encoding="utf-8")
    base_file = None
    if base_approved is not None:
        base_file = tmp_path / "base-approved.yaml"
        base_file.write_text(base_approved, encoding="utf-8")
    return int(
        judge_module.judge(
            tmp_path / "base.json", tmp_path / "head.json", tmp_path / "approved.yaml", base_file
        )
    )


def _entry(change: str, reason: str = "the reason", op_id: str = "vault.kv.put") -> str:
    return (
        "approved:\n"
        f"  - op_id: {op_id}\n"
        "    connector_id: vault-1.x\n"
        f"    change: {change}\n"
        f"    reason: {reason}\n"
    )


_LOWERED = ("lowered_level", {"risk_level": "caution"})
_LOST_APPROVAL = ("lost_approval", {"needs_approval": False})
_LOST_NEVER = ("lost_never", {"never_covered": None})
_LEFT_CREDENTIAL = ("left_credential", {"class": "write"})


@pytest.mark.parametrize(
    ("kind", "change"), [_LOWERED, _LOST_APPROVAL, _LOST_NEVER, _LEFT_CREDENTIAL]
)
def test_each_kind_fails_and_names_the_row(
    tmp_path: Path, capfd: pytest.CaptureFixture[str], kind: str, change: dict[str, Any]
) -> None:
    assert _judge(tmp_path, [_row()], [_row(**change)]) == 1
    out = capfd.readouterr().out
    assert f"vault.kv.put (vault-1.x): {kind}" in out
    assert "1 change(s), 1 not approved" in out


def test_a_removed_row_fails(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    assert _judge(tmp_path, [_row(), _row("vault.kv.get")], [_row()]) == 1
    assert "vault.kv.get (vault-1.x): removed" in capfd.readouterr().out


def test_two_changes_of_one_row_need_two_entries(tmp_path: Path) -> None:
    head = [_row(risk_level="safe", needs_approval=False)]
    assert _judge(tmp_path, [_row()], head, _entry("lowered_level")) == 1
    both = _entry("lowered_level") + _entry("lost_approval").replace("approved:\n", "")
    assert _judge(tmp_path, [_row()], head, both) == 0


def test_a_new_row_passes(tmp_path: Path) -> None:
    assert _judge(tmp_path, [_row()], [_row(), _row("vault.kv.new", risk_level="safe")]) == 0


def test_more_approval_and_a_higher_level_pass(tmp_path: Path) -> None:
    base = [
        _row(risk_level="caution", needs_approval=False, never_covered=None, **{"class": "write"})
    ]
    head = [_row(risk_level="destructive")]
    assert _judge(tmp_path, base, head) == 0


def test_a_level_that_moves_between_non_credential_classes_passes(tmp_path: Path) -> None:
    base = [_row(**{"class": "write"})]
    assert _judge(tmp_path, base, [_row(**{"class": "read"})]) == 0


def test_an_approved_entry_passes(tmp_path: Path) -> None:
    assert _judge(tmp_path, [_row()], [_row(**_LOWERED[1])], _entry("lowered_level")) == 0


def test_an_approved_entry_for_another_row_or_change_does_not_cover(tmp_path: Path) -> None:
    head = [_row(**_LOWERED[1])]
    assert _judge(tmp_path, [_row()], head, _entry("lost_approval")) == 1
    assert _judge(tmp_path, [_row()], head, _entry("lowered_level", op_id="vault.kv.get")) == 1


def test_a_stale_approved_entry_fails(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    assert _judge(tmp_path, [_row()], [_row()], _entry("lowered_level")) == 1
    assert "matches no change (stale)" in capfd.readouterr().out


def test_an_entry_already_on_the_base_is_never_stale_and_approves_nothing(tmp_path: Path) -> None:
    entry = _entry("lowered_level")
    assert _judge(tmp_path, [_row()], [_row()], entry, base_approved=entry) == 0
    assert _judge(tmp_path, [_row()], [_row(**_LOWERED[1])], entry, base_approved=entry) == 1


def test_an_entry_without_a_reason_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="has no reason"):
        _judge(tmp_path, [_row()], [_row(**_LOWERED[1])], _entry("lowered_level", reason=""))


def test_an_unknown_risk_level_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="unknown risk level"):
        _judge(tmp_path, [_row()], [_row(risk_level="mild")])
