# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""CI check: old shared lists may only get shorter.

Some old lists are edited by many features, so two open pull requests that
both add a line at the end of one of them conflict. Each of these lists now
has a better place for new entries. :data:`SHARED_LISTS` pins the length of
each one:

- A list that is longer than its pin fails. The message says where the new
  entry goes instead.
- A list that is shorter than its pin also fails. Lower the pin in the same
  pull request, so nobody can fill the gap later.
- A row whose list no longer exists fails with "remove this row".

The rules are on ``docs/codebase/how-code-is-organised.md``. The check reads
the files as text and imports nothing, so it is fast.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
STANDARD_PAGE: Final[str] = "docs/codebase/how-code-is-organised.md"


class ListNotFoundError(Exception):
    """The list of a row no longer exists."""


class ListNotCountableError(Exception):
    """The list exists, but the check cannot count its entries."""


def _module_file(root: Path, module: str) -> Path:
    base = root / "backend" / "src" / Path(*module.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    raise ListNotFoundError(f"module {module} not found under backend/src")


def _is_name(node: ast.AST, name: str) -> bool:
    return isinstance(node, ast.Name) and node.id == name


def _changes(node: ast.expr, name: str) -> bool:
    """Is ``node`` a store, delete, slice store or method call on ``name``?"""
    if isinstance(node, ast.Name):
        return node.id == name and not isinstance(node.ctx, ast.Load)
    if isinstance(node, ast.Subscript):
        return _is_name(node.value, name) and not isinstance(node.ctx, ast.Load)
    if isinstance(node, ast.Call):
        return isinstance(node.func, ast.Attribute) and _is_name(node.func.value, name)
    return False


def _other_changes(tree: ast.Module, name: str, owner: ast.Assign | ast.AnnAssign) -> list[int]:
    """Return the line of every place, except the assignment ``owner``, that changes ``name``.

    Anywhere in the file, also inside a block or a function: a second
    assignment, ``+=``, ``del``, a slice assignment, and any method call such
    as ``.append(...)`` or ``.extend(...)``.
    """
    own_targets = owner.targets if isinstance(owner, ast.Assign) else [owner.target]
    return sorted(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.expr)
        and not any(node is target for target in own_targets)
        and _changes(node, name)
    )


def python_list(module: str, name: str) -> Callable[[Path], int]:
    """Count the entries of a list, tuple or set literal named ``name`` in ``module``.

    The file is parsed, not imported. Only one top-level assignment of a
    plain literal counts. Any other change to the name anywhere in the file
    (``+=``, ``.append(...)``, ``.extend(...)``, a slice assignment, a second
    assignment) and a starred entry make the list uncountable, so the check
    fails instead of missing a new entry.
    """

    def count(root: Path) -> int:
        path = _module_file(root, module)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        owners = [
            node
            for node in tree.body
            if (isinstance(node, ast.Assign) and any(_is_name(t, name) for t in node.targets))
            or (isinstance(node, ast.AnnAssign) and _is_name(node.target, name))
        ]
        if not owners:
            raise ListNotFoundError(f"{module} has no top-level name {name}")
        owner = owners[0]
        literal = owner.value
        if len(owners) > 1 or not isinstance(literal, (ast.List, ast.Tuple, ast.Set)):
            raise ListNotCountableError(
                f"the name {name} is not one plain list, tuple or set literal"
            )
        changes = _other_changes(tree, name, owner)
        if changes:
            raise ListNotCountableError(
                f"the list is changed again after its literal "
                f"(line {', line '.join(str(line) for line in changes)})"
            )
        starred = [elt for elt in literal.elts if isinstance(elt, ast.Starred)]
        if starred:
            raise ListNotCountableError(f"the list has a starred entry on line {starred[0].lineno}")
        return len(literal.elts)

    return count


def file_lines(path: str) -> Callable[[Path], int]:
    """Count the lines of a file that are not empty and not a ``#`` comment."""

    def count(root: Path) -> int:
        full = root / path
        if not full.is_file():
            raise ListNotFoundError(f"file {path} not found")
        lines = full.read_text(encoding="utf-8").splitlines()
        return sum(1 for line in lines if line.strip() and not line.lstrip().startswith("#"))

    return count


@dataclass(frozen=True)
class SharedList:
    """One old shared list and its pinned length."""

    where: str
    """The list: ``<module>.<name>`` or a file path from the repo root."""
    pin: int
    """The length of the list. It may only go down."""
    instead: str
    """Where new entries go instead of this list."""
    count: Callable[[Path], int]
    """Counts the entries, given the repo root."""


SHARED_LISTS: Final[tuple[SharedList, ...]] = (
    SharedList(
        where="meho_backplane.connectors.vmware_rest.composites.__all__",
        pin=77,
        instead=(
            "A new vSphere multi-step operation goes in its own module in "
            "connectors/vmware_rest/composites/ and exports COMPOSITES. The "
            "collector finds it, so it needs no name here (rules 1 and 2)."
        ),
        count=python_list("meho_backplane.connectors.vmware_rest.composites", "__all__"),
    ),
    SharedList(
        where="meho_backplane.connectors.vmware_rest.composites.schemas.__all__",
        pin=128,
        instead=(
            "A new vSphere multi-step operation keeps its schemas in its own "
            "module, for example composites/_<feature>_schemas.py, not in "
            "schemas.py (rule 1)."
        ),
        count=python_list("meho_backplane.connectors.vmware_rest.composites.schemas", "__all__"),
    ),
)


def check_shared_list(row: SharedList, root: Path) -> str | None:
    """Return the problem with one row, or ``None`` when its length equals its pin."""
    try:
        length = row.count(root)
    except ListNotFoundError as exc:
        return (
            f"{row.where}: the list no longer exists ({exc}). Remove this row "
            f"from SHARED_LISTS in backend/tests/test_shared_lists_only_shrink.py."
        )
    except ListNotCountableError as exc:
        return (
            f"{row.where}: {exc}, so its entries cannot be counted. Keep it one "
            f"plain literal, or change how this row counts it."
        )
    if length > row.pin:
        return (
            f"{row.where} has {length} entries, but its pin is {row.pin}. This "
            f"old shared list may only get shorter. Do not add to it. "
            f"{row.instead} See {STANDARD_PAGE}."
        )
    if length < row.pin:
        return (
            f"{row.where} has {length} entries, but its pin is {row.pin}. Good: "
            f"the list got shorter. Lower its pin to {length} in "
            f"backend/tests/test_shared_lists_only_shrink.py in this pull request."
        )
    return None


@pytest.mark.parametrize("row", SHARED_LISTS, ids=lambda row: row.where)
def test_shared_list_is_not_longer_than_its_pin(row: SharedList) -> None:
    problem = check_shared_list(row, REPO_ROOT)
    assert problem is None, problem


def test_every_row_names_one_list() -> None:
    wheres = [row.where for row in SHARED_LISTS]
    assert len(wheres) == len(set(wheres))


def test_standard_page_names_this_check() -> None:
    page = (REPO_ROOT / STANDARD_PAGE).read_text(encoding="utf-8")
    assert "test_shared_lists_only_shrink.py" in page


# The check's own tests, with fake lists under tmp_path.

_MODULE: Final[str] = "fake_pkg.feature"
_INSTEAD: Final[str] = "New entries go in their own module."


def _write_module(root: Path, body: str, *, module: str = _MODULE) -> None:
    path = root / "backend" / "src" / Path(*module.split(".")).with_suffix(".py")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _row(pin: int = 2, *, name: str = "__all__", module: str = _MODULE) -> SharedList:
    return SharedList(
        where=f"{module}.{name}",
        pin=pin,
        instead=_INSTEAD,
        count=python_list(module, name),
    )


def test_list_at_its_pin_passes(tmp_path: Path) -> None:
    _write_module(tmp_path, '__all__ = [\n    "a",\n    "b",\n]\n')
    assert check_shared_list(_row(), tmp_path) is None


def test_list_in_a_package_init_is_found(tmp_path: Path) -> None:
    init = tmp_path / "backend" / "src" / "fake_pkg" / "__init__.py"
    init.parent.mkdir(parents=True)
    init.write_text('__all__ = ("a", "b")\n', encoding="utf-8")
    assert check_shared_list(_row(module="fake_pkg"), tmp_path) is None


def test_list_that_grows_by_one_fails_and_says_where_to_go(tmp_path: Path) -> None:
    _write_module(tmp_path, '__all__ = ["a", "b", "c"]\n')
    problem = check_shared_list(_row(), tmp_path)
    assert problem is not None
    assert f"{_MODULE}.__all__ has 3 entries, but its pin is 2" in problem
    assert "may only get shorter" in problem
    assert _INSTEAD in problem
    assert STANDARD_PAGE in problem


def test_list_that_shrinks_asks_to_lower_the_pin(tmp_path: Path) -> None:
    _write_module(tmp_path, '__all__ = ["a"]\n')
    problem = check_shared_list(_row(), tmp_path)
    assert problem is not None
    assert "Lower its pin to 1" in problem
    assert check_shared_list(_row(pin=1), tmp_path) is None


def test_annotated_list_is_counted(tmp_path: Path) -> None:
    _write_module(tmp_path, 'NAMES: list[str] = ["a", "b"]\n')
    assert check_shared_list(_row(name="NAMES"), tmp_path) is None


@pytest.mark.parametrize(
    ("body", "name"),
    [
        pytest.param('__all__ = ["a", "b"]\n', "OTHER", id="list-renamed"),
        pytest.param(
            'def f():\n    __all__ = ["a", "b"]\n', "__all__", id="only-inside-a-function"
        ),
    ],
)
def test_list_that_is_gone_asks_to_remove_the_row(tmp_path: Path, body: str, name: str) -> None:
    _write_module(tmp_path, body)
    problem = check_shared_list(_row(name=name), tmp_path)
    assert problem is not None
    assert "Remove this row" in problem


def test_module_that_moved_asks_to_remove_the_row(tmp_path: Path) -> None:
    _write_module(tmp_path, '__all__ = ["a", "b"]\n', module="fake_pkg.moved")
    problem = check_shared_list(_row(), tmp_path)
    assert problem is not None
    assert "Remove this row" in problem
    assert f"module {_MODULE} not found" in problem


def test_module_that_is_deleted_asks_to_remove_the_row(tmp_path: Path) -> None:
    problem = check_shared_list(_row(), tmp_path)
    assert problem is not None
    assert "Remove this row" in problem


@pytest.mark.parametrize(
    "body",
    [
        pytest.param('__all__ = ["a"] + OTHER\n', id="built-from-parts"),
        pytest.param('__all__ = ["a", "b"]\n__all__ += ["c"]\n', id="extended-later"),
        pytest.param('__all__ = ["a", "b"]\n__all__ = ["a", "b"]\n', id="assigned-twice"),
        pytest.param('__all__ = ["a", "b"]\n__all__.append("c")\n', id="append"),
        pytest.param('__all__ = ["a", "b"]\n__all__.extend(["c"])\n', id="extend"),
        pytest.param('__all__ = ["a", "b"]\n__all__.insert(0, "c")\n', id="insert"),
        pytest.param('__all__ = ["a", "b"]\n__all__[2:2] = ["c"]\n', id="slice-assigned"),
        pytest.param('__all__ = ["a", *["b", "c"]]\n', id="starred-entry"),
        pytest.param('__all__ = ["a", "b"]\n__all__ = ("a", "b", "c")\n', id="replaced"),
        pytest.param(
            '__all__ = ["a", "b"]\nif True:\n    __all__.append("c")\n', id="append-in-a-block"
        ),
        pytest.param(
            '__all__ = ["a", "b"]\n\n\ndef f():\n    global __all__\n    __all__ += ["c"]\n',
            id="grown-inside-a-function",
        ),
    ],
)
def test_list_that_cannot_be_counted_fails(tmp_path: Path, body: str) -> None:
    _write_module(tmp_path, body)
    problem = check_shared_list(_row(), tmp_path)
    assert problem is not None
    assert "cannot be counted" in problem


def test_later_change_names_its_line(tmp_path: Path) -> None:
    _write_module(tmp_path, '__all__ = [\n    "a",\n    "b",\n]\n\n__all__.extend(["c"])\n')
    problem = check_shared_list(_row(), tmp_path)
    assert problem is not None
    assert "is changed again after its literal (line 6)" in problem


def test_reading_the_list_is_not_a_change(tmp_path: Path) -> None:
    _write_module(tmp_path, '__all__ = ["a", "b"]\nNAMES = sorted(__all__)\nprint(__all__[0])\n')
    assert check_shared_list(_row(), tmp_path) is None


def _file_row(pin: int, path: str = "ci/old-list.txt") -> SharedList:
    return SharedList(where=path, pin=pin, instead=_INSTEAD, count=file_lines(path))


def test_file_list_counts_entries_not_comments_or_blank_lines(tmp_path: Path) -> None:
    (tmp_path / "ci").mkdir()
    (tmp_path / "ci" / "old-list.txt").write_text(
        "# header\n\none\n  two\n   # indented comment\n", encoding="utf-8"
    )
    assert check_shared_list(_file_row(2), tmp_path) is None
    grown = check_shared_list(_file_row(1), tmp_path)
    assert grown is not None
    assert "ci/old-list.txt has 2 entries, but its pin is 1" in grown


def test_file_list_that_is_deleted_asks_to_remove_the_row(tmp_path: Path) -> None:
    problem = check_shared_list(_file_row(2), tmp_path)
    assert problem is not None
    assert "Remove this row" in problem
