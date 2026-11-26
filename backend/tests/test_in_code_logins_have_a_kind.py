# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Every login that MEHO's own code builds names its kind of caller.

A login made in the code is an :class:`~meho_backplane.auth.operator.Operator`
that MEHO builds itself, with no sign-in and no token check. Each one must
pass ``in_code_kind`` (:func:`~meho_backplane.auth.caller_kind.caller_kind`
reads it). The test scans the source; it keeps no list of the places. Only
the two places that build a login from a checked token are left out:

* ``auth/jwt.py`` -- the token check itself;
* ``ui/auth/refresh.py`` -- the web console session, built from the
  checked token of the session.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Final

SRC: Final[Path] = Path(__file__).resolve().parents[1] / "src" / "meho_backplane"

#: The places that build a login from a checked token, not in the code.
TOKEN_CHECK_MODULES: Final[frozenset[str]] = frozenset({"auth/jwt.py", "ui/auth/refresh.py"})

#: Class methods that make an Operator without a keyword list to scan.
_OTHER_BUILDERS: Final[frozenset[str]] = frozenset({"model_validate", "model_construct"})


def _is_operator(node: ast.expr) -> bool:
    return (isinstance(node, ast.Name) and node.id == "Operator") or (
        isinstance(node, ast.Attribute) and node.attr == "Operator"
    )


def _problem(call: ast.Call) -> str | None:
    """Why *call* is an in-code login without a kind, or ``None``."""
    func = call.func
    if isinstance(func, ast.Attribute) and func.attr in _OTHER_BUILDERS:
        if _is_operator(func.value):
            return f"Operator.{func.attr}() cannot be checked; build it with Operator(...)"
        return None
    if not _is_operator(func):
        return None
    kind = next((k for k in call.keywords if k.arg == "in_code_kind"), None)
    if kind is None:
        return "no in_code_kind"
    if isinstance(kind.value, ast.Constant) and kind.value.value is None:
        return "in_code_kind is None"
    return None


def scan(src: Path = SRC) -> tuple[list[str], list[str]]:
    """Return (every in-code login found, the ones without a kind), as ``file:line``."""
    found: list[str] = []
    missing: list[str] = []
    for path in sorted(src.rglob("*.py")):
        rel = path.relative_to(src).as_posix()
        if rel in TOKEN_CHECK_MODULES:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            is_builder = _is_operator(func) or (
                isinstance(func, ast.Attribute)
                and func.attr in _OTHER_BUILDERS
                and _is_operator(func.value)
            )
            if not is_builder:
                continue
            place = f"{rel}:{node.lineno}"
            found.append(place)
            problem = _problem(node)
            if problem is not None:
                missing.append(f"{place}: {problem}")
    return found, missing


def test_every_in_code_login_names_its_kind() -> None:
    found, missing = scan()
    assert not missing, (
        "A login that MEHO's own code builds must pass in_code_kind "
        "(see meho_backplane.auth.caller_kind):\n" + "\n".join(missing)
    )
    # The scan really reads the source: main had at least eleven such logins.
    assert len(found) >= 11, found


def test_the_scan_finds_a_login_without_a_kind(tmp_path: Path) -> None:
    (tmp_path / "plain.py").write_text(
        "from meho_backplane.auth.operator import Operator\n"
        "def build():\n"
        "    return Operator(sub='x', raw_jwt='', tenant_id=None, tenant_role='operator')\n",
        encoding="utf-8",
    )
    (tmp_path / "none_kind.py").write_text(
        "import meho_backplane.auth.operator as op\n"
        "def build():\n"
        "    return op.Operator(sub='x', in_code_kind=None)\n",
        encoding="utf-8",
    )
    (tmp_path / "validate.py").write_text(
        "from meho_backplane.auth.operator import Operator\n"
        "def build(data):\n"
        "    return Operator.model_validate(data)\n",
        encoding="utf-8",
    )
    (tmp_path / "good.py").write_text(
        "from meho_backplane.auth.operator import Operator\n"
        "def build():\n"
        "    return Operator(sub='x', in_code_kind='system')\n",
        encoding="utf-8",
    )
    found, missing = scan(tmp_path)
    assert len(found) == 4
    assert sorted(missing) == [
        "none_kind.py:3: in_code_kind is None",
        "plain.py:3: no in_code_kind",
        "validate.py:3: Operator.model_validate() cannot be checked; build it with Operator(...)",
    ]
