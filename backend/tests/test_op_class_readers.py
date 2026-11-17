# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A new reader of the class of an operation must not look at the op id alone.

The class of an operation comes from its row first and its op id second
(``meho_backplane/operations/op_class_of.py``). A connector floor can store a
class on an ingested row, and the op id of such an operation says nothing
about it. So code must not call ``classify_op(op_id)``, or
``resolve_op_class(op_id)`` without a stored class, in a place that could have
the row or the stored class.

This check reads every source file. It fails on such a call outside the list
below. Each entry on the list says why that call is right. To add one, say why
no row or stored class is available there.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "meho_backplane"

#: File (relative to ``src/meho_backplane``) -> why a call on the op id alone is right there.
_ALLOWED: dict[str, str] = {
    "broadcast/events.py": "defines the classifier; the op id is its input",
    "ui/routes/broadcast/aggregate_gate.py": (
        "the drawer class: the stored secret class of the audit row is read first"
    ),
    "audit_query/query.py": "the class the audit row stored is read first; the op id is the rest",
    "reflex/adoption.py": (
        "a count of write calls by audit row; the class the row stored counts first, "
        "the op id adds the older rows"
    ),
}

_CHECKED = {"classify_op", "resolve_op_class"}


def _called_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _is_op_id_only(node: ast.Call, name: str) -> bool:
    """True for ``classify_op(x)``, ``resolve_op_class(x)`` and ``resolve_op_class(x, None)``."""
    if name == "classify_op":
        return True
    declared = node.args[1] if len(node.args) > 1 else None
    for keyword in node.keywords:
        if keyword.arg == "declared":
            declared = keyword.value
    return declared is None or (isinstance(declared, ast.Constant) and declared.value is None)


def _op_id_only_calls() -> dict[str, list[int]]:
    found: dict[str, list[int]] = {}
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _called_name(node)
            if name in _CHECKED and _is_op_id_only(node, name):
                found.setdefault(str(path.relative_to(SRC)), []).append(node.lineno)
    return found


def test_no_new_reader_of_the_class_looks_at_the_op_id_alone() -> None:
    found = _op_id_only_calls()

    unexpected = {path: lines for path, lines in found.items() if path not in _ALLOWED}

    assert not unexpected, (
        "These places read the class of an operation from the op id alone. Use the helpers "
        "in meho_backplane/operations/op_class_of.py (descriptor_op_class, request_op_class), "
        "or pass the stored class to resolve_op_class: "
        + "; ".join(
            f"{path} line {', '.join(map(str, lines))}" for path, lines in unexpected.items()
        )
    )


def test_every_allowed_entry_still_has_such_a_call() -> None:
    found = _op_id_only_calls()

    stale = sorted(path for path in _ALLOWED if path not in found)

    assert not stale, (
        f"Remove these entries from the list, they have no such call any more: {stale}"
    )


def test_the_check_sees_a_call_on_the_op_id_alone() -> None:
    tree = ast.parse("classify_op(op_id)\nresolve_op_class(op_id)\nresolve_op_class(op_id, None)\n")
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    assert [_is_op_id_only(c, _called_name(c) or "") for c in calls] == [True, True, True]
    ok = ast.parse("resolve_op_class(op_id, stored)\nresolve_op_class(op_id, declared=stored)")
    ok_calls = [n for n in ast.walk(ok) if isinstance(n, ast.Call)]
    assert [_is_op_id_only(c, "resolve_op_class") for c in ok_calls] == [False, False]
