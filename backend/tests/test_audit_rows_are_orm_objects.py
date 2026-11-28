# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Every ``audit_log`` row is built as an ``AuditLog`` object.

With ``CUSTOM_POLICY`` on, a row under MEHO's own ``system`` name gets the
kind of caller ``system`` from an ``AuditLog`` ``init`` listener
(:mod:`meho_backplane.auth.caller_record`). A Core insert into
``audit_log`` builds no object, so such a row would get no kind. This
scan fails when a source file writes one.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src" / "meho_backplane"

_RAW_INSERT = re.compile(r"INSERT\s+INTO\s+audit_log", re.IGNORECASE)


def _core_inserts(source: str) -> list[int]:
    """The lines of *source* that write ``audit_log`` without an ``AuditLog`` object."""
    lines = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _RAW_INSERT.search(node.value):
                lines.append(node.lineno)
        elif isinstance(node, ast.Attribute) and node.attr == "__table__":
            if ast.unparse(node.value) == "AuditLog":
                lines.append(node.lineno)
        elif (
            isinstance(node, ast.Call)
            and ast.unparse(node.func).split(".")[-1] == "insert"
            and [ast.unparse(arg) for arg in node.args[:1]] == ["AuditLog"]
        ):
            lines.append(node.lineno)
    return lines


def test_the_scan_finds_each_core_insert() -> None:
    for line in (
        "stmt = sa.insert(AuditLog).values(operator_sub='system:x')",
        "stmt = AuditLog.__table__.insert()",
        "text('INSERT INTO audit_log (id) VALUES (:id)')",
    ):
        assert _core_inserts(line) == [1], line


def test_every_audit_row_is_built_as_an_audit_log_object() -> None:
    """The kind of a ``system`` row is set when its ``AuditLog`` is built.

    A Core insert into ``audit_log`` builds no object, so such a row would
    get no kind. No source file may write one.
    """
    found = [
        f"{path.relative_to(_SRC)}:{line}"
        for path in sorted(_SRC.rglob("*.py"))
        for line in _core_inserts(path.read_text(encoding="utf-8"))
    ]
    assert found == []
