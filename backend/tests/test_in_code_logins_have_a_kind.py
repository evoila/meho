# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Every login that MEHO's own code builds names its kind of caller.

A login made in the code is an :class:`~meho_backplane.auth.operator.Operator`
that MEHO builds itself, with no sign-in and no token check. Each one must
pass ``in_code_kind`` (:func:`~meho_backplane.auth.caller_kind.caller_kind`
reads it). The test scans the source; it keeps no list of the places.

The scan finds ``Operator(...)`` also under another name
(``from ... import Operator as _Op``, ``op_mod.Operator(...)``, ``Op = Operator``),
and the class methods that make a login without a keyword list to check
(``model_validate``, ``model_validate_json``, ``model_validate_strings``,
``model_construct``). Only the two places that build a login from a checked
token are left out:

* ``auth/jwt.py`` -- the token check itself;
* ``ui/auth/refresh.py`` -- the web console session, built from the
  checked token of the session.

:func:`scan` also gives the kind each place names, so
``test_caller_kind.py`` can build every place it finds.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Final

SRC: Final[Path] = Path(__file__).resolve().parents[1] / "src" / "meho_backplane"

#: The places that build a login from a checked token, not in the code.
TOKEN_CHECK_MODULES: Final[frozenset[str]] = frozenset({"auth/jwt.py", "ui/auth/refresh.py"})

#: Class methods that make an Operator without a keyword list to check.
_OTHER_BUILDERS: Final[frozenset[str]] = frozenset(
    {"model_validate", "model_validate_json", "model_validate_strings", "model_construct"}
)


@dataclass(frozen=True)
class InCodeLogin:
    """One place in the source that builds a login."""

    file: str
    line: int
    #: The kind the place names when it is a plain text, else ``None``
    #: (no kind, or a kind the code works out).
    kind: str | None
    #: Why the place has no kind, or ``None``.
    problem: str | None

    @property
    def place(self) -> str:
        return f"{self.file}:{self.line}"


def _operator_names(tree: ast.AST) -> set[str]:
    """The names that stand for the Operator class in one module."""
    names = {"Operator"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.update(a.asname for a in node.names if a.name == "Operator" and a.asname)
    # ``Op = Operator`` or ``Op = op_mod.Operator``, also in a chain.
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Assign)
                and _is_operator(node.value, names)
                and all(isinstance(t, ast.Name) for t in node.targets)
            ):
                for target in node.targets:
                    assert isinstance(target, ast.Name)
                    if target.id not in names:
                        names.add(target.id)
                        changed = True
    return names


def _is_operator(node: ast.expr, names: set[str]) -> bool:
    return (isinstance(node, ast.Name) and node.id in names) or (
        isinstance(node, ast.Attribute) and node.attr in names
    )


def _check(call: ast.Call, names: set[str]) -> tuple[bool, str | None, str | None]:
    """(is a login builder, its plain kind, why it has no kind)."""
    func = call.func
    if isinstance(func, ast.Attribute) and func.attr in _OTHER_BUILDERS:
        if _is_operator(func.value, names):
            return (
                True,
                None,
                f"Operator.{func.attr}() cannot be checked; build it with Operator(...)",
            )
        return False, None, None
    if not _is_operator(func, names):
        return False, None, None
    kind = next((k for k in call.keywords if k.arg == "in_code_kind"), None)
    if kind is None:
        return True, None, "no in_code_kind"
    if isinstance(kind.value, ast.Constant):
        if kind.value.value is None:
            return True, None, "in_code_kind is None"
        return True, str(kind.value.value), None
    return True, None, None


def scan(src: Path = SRC) -> list[InCodeLogin]:
    """Every in-code login under *src*, in file and line order."""
    found: list[InCodeLogin] = []
    for path in sorted(src.rglob("*.py")):
        rel = path.relative_to(src).as_posix()
        if rel in TOKEN_CHECK_MODULES:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = _operator_names(tree)
        calls = sorted(
            (n for n in ast.walk(tree) if isinstance(n, ast.Call)),
            key=lambda n: (n.lineno, n.col_offset),
        )
        for node in calls:
            is_builder, kind, problem = _check(node, names)
            if is_builder:
                found.append(InCodeLogin(rel, node.lineno, kind, problem))
    return found


def test_every_in_code_login_names_its_kind() -> None:
    found = scan()
    missing = [f"{login.place}: {login.problem}" for login in found if login.problem]
    assert not missing, (
        "A login that MEHO's own code builds must pass in_code_kind "
        "(see meho_backplane.auth.caller_kind):\n" + "\n".join(missing)
    )
    # The scan really reads the source: there are more than thirty such logins.
    assert len(found) >= 30, [login.place for login in found]


_BAD_FILES: Final[dict[str, str]] = {
    "plain.py": (
        "from meho_backplane.auth.operator import Operator\n"
        "def build():\n"
        "    return Operator(sub='x', raw_jwt='', tenant_id=None, tenant_role='operator')\n"
    ),
    "alias.py": (
        "from meho_backplane.auth.operator import Operator as _Op\n"
        "def build():\n"
        "    return _Op(sub='x')\n"
    ),
    "module_name.py": (
        "import meho_backplane.auth.operator as op_mod\n"
        "def build():\n"
        "    return op_mod.Operator(sub='x')\n"
    ),
    "assigned.py": (
        "from meho_backplane.auth import operator\nOp = operator.Operator\nOp(sub='x')\n"
    ),
    "none_kind.py": (
        "from meho_backplane.auth.operator import Operator\n"
        "def build():\n"
        "    return Operator(sub='x', in_code_kind=None)\n"
    ),
    "validate.py": (
        "from meho_backplane.auth.operator import Operator\n"
        "def build(data):\n"
        "    return Operator.model_validate(data)\n"
    ),
    "validate_json.py": (
        "from meho_backplane.auth.operator import Operator\n"
        "def build(text):\n"
        "    return Operator.model_validate_json(text)\n"
    ),
    "validate_strings.py": (
        "from meho_backplane.auth.operator import Operator\n"
        "def build(data):\n"
        "    return Operator.model_validate_strings(data)\n"
    ),
    "construct.py": (
        "from meho_backplane.auth.operator import Operator as _Op\n"
        "def build():\n"
        "    return _Op.model_construct(sub='x')\n"
    ),
}


def test_the_scan_finds_each_login_without_a_kind(tmp_path: Path) -> None:
    for name, text in _BAD_FILES.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    (tmp_path / "good.py").write_text(
        "from meho_backplane.auth.operator import Operator as _Op\n"
        "def build():\n"
        "    return _Op(sub='x', in_code_kind='system')\n",
        encoding="utf-8",
    )
    found = {login.file: login for login in scan(tmp_path)}
    assert set(found) == {*_BAD_FILES, "good.py"}
    assert {name for name, login in found.items() if login.problem} == set(_BAD_FILES)
    assert found["good.py"].problem is None
    assert found["good.py"].kind == "system"
    assert found["none_kind.py"].problem == "in_code_kind is None"
    assert found["alias.py"].problem == "no in_code_kind"


def test_the_scan_test_fails_on_an_alias_import_without_a_kind(tmp_path: Path) -> None:
    (tmp_path / "alias.py").write_text(_BAD_FILES["alias.py"], encoding="utf-8")
    [login] = scan(tmp_path)
    assert login.place == "alias.py:3"
    assert login.problem == "no in_code_kind"
