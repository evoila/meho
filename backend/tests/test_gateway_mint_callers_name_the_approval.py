# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Every source call of ``mint_gateway_command`` names the approval request.

The mint uses an approval only for the request it names and only for the login
that asked. A call without ``approval_request_id=`` cannot use an approval, so
the mint refuses a write from it. This guard finds such calls in the source
files outside ``tests/``.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src" / "meho_backplane"
_MINT = "mint_gateway_command"
_DEFINING_MODULE = _SRC / "operations" / "gateway_commands.py"


def _calls_without_approval(source: str) -> list[int]:
    """Line numbers of mint calls that pass no ``approval_request_id``."""
    lines: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name != _MINT:
            continue
        if not any(kw.arg == "approval_request_id" for kw in node.keywords):
            lines.append(node.lineno)
    return lines


def test_guard_flags_a_call_without_the_approval_request() -> None:
    """The guard finds a call that leaves out the approval request id."""
    assert _calls_without_approval("await mint_gateway_command(s, operator=o)") == [1]
    assert _calls_without_approval("await mint_gateway_command(s, approval_request_id=a)") == []


def test_source_calls_of_the_mint_name_the_approval_request() -> None:
    offenders = [
        f"{path.relative_to(_SRC)}:{line}"
        for path in sorted(_SRC.rglob("*.py"))
        if path != _DEFINING_MODULE
        for line in _calls_without_approval(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, (
        "These calls of mint_gateway_command pass no approval_request_id: "
        f"{offenders}. Pass approval_request_id=<the approved request> so the mint "
        "checks that the caller is the login that asked."
    )
