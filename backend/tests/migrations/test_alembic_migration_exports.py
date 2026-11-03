# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Every new Alembic migration lists its module names in ``__all__``.

Alembic reads ``revision``, ``down_revision``, ``branch_labels`` and
``depends_on`` from the module itself. Nothing in the file uses them, so
the code-quality check (the CodeQL rule ``py/unused-global-variable``)
opened four "Unused global variable" threads on every pull request with a
new migration. The rule skips names the module exports. A name counts as
exported when it is in an ``__all__`` that is a plain assignment
(``__all__ = [...]``, no type annotation) of string literals. Without
``__all__`` only plain assignments count, and the template annotates its
names (``revision: str = ...``), so none of them counted.

The template ``alembic/script.py.mako`` writes that ``__all__``. This test
checks that the template still does, and that every migration newer than
:data:`_BASELINE_REVISION` (the newest one when the rule started) has it,
so hand-written migrations follow the template too. Older migrations are
left as they are.
"""

from __future__ import annotations

import ast
import shutil
from pathlib import Path

import pytest
from alembic.script import ScriptDirectory

#: parents[0] = backend/tests/migrations, parents[1] = backend/tests, parents[2] = backend
_ALEMBIC_DIR: Path = Path(__file__).resolve().parents[2] / "alembic"
_VERSIONS_DIR: Path = _ALEMBIC_DIR / "versions"

#: The newest migration on main when this rule started. Migrations up to
#: and including it are not checked and must not be changed for this rule.
_BASELINE_REVISION = 134

#: The baseline and the first revision after it, as zero-padded strings.
_OLD = f"{_BASELINE_REVISION:04d}"
_NEW = f"{_BASELINE_REVISION + 1:04d}"

_REQUIRED_EXPORTS: frozenset[str] = frozenset(
    {"revision", "down_revision", "branch_labels", "depends_on", "upgrade", "downgrade"}
)


def _module_revision(tree: ast.Module) -> str | None:
    """Return the string value of the module-level ``revision``, if any."""
    for node in tree.body:
        if isinstance(node, ast.AnnAssign | ast.Assign):
            targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
            for target in targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id == "revision"
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)
                ):
                    return node.value.value
    return None


def _is_new_migration(revision: str | None) -> bool:
    """Return True when *revision* is newer than the baseline (or not a number)."""
    if revision is None or not revision.isdigit():
        return True
    return int(revision) > _BASELINE_REVISION


def check_exports(source: str) -> list[str]:
    """Return the problems with the ``__all__`` of one migration file.

    An empty list means the file is fine.
    """
    tree = ast.parse(source)
    problems: list[str] = []
    found = False
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == "__all__":
                problems.append("__all__ has a type annotation; the check reads only plain '='")
            continue
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets):
            continue
        found = True
        if not isinstance(node.value, ast.List):
            problems.append("__all__ must be a list, not a tuple or another expression")
            continue
        names = set()
        for element in node.value.elts:
            if isinstance(element, ast.Constant) and isinstance(element.value, str):
                names.add(element.value)
            else:
                problems.append("__all__ must hold only string literals")
        missing = sorted(_REQUIRED_EXPORTS - names)
        if missing:
            problems.append(f"__all__ is missing {', '.join(missing)}")
    if not found and not problems:
        problems.append("no module-level __all__ = [...]")
    return problems


def _new_migration_files(versions_dir: Path = _VERSIONS_DIR) -> list[Path]:
    files = []
    for path in sorted(versions_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if _is_new_migration(_module_revision(tree)):
            files.append(path)
    return files


def test_new_migrations_list_their_names_in_all() -> None:
    failures = [
        f"{path.name}: {'; '.join(problems)}"
        for path in _new_migration_files()
        if (problems := check_exports(path.read_text(encoding="utf-8")))
    ]
    assert not failures, "Copy the __all__ block from alembic/script.py.mako into:\n" + "\n".join(
        failures
    )


def test_only_migrations_after_the_baseline_are_checked(tmp_path: Path) -> None:
    (tmp_path / f"{_OLD}_old.py").write_text(f'revision: str = "{_OLD}"\n', encoding="utf-8")
    (tmp_path / f"{_NEW}_new.py").write_text(f'revision: str = "{_NEW}"\n', encoding="utf-8")
    assert [p.name for p in _new_migration_files(tmp_path)] == [f"{_NEW}_new.py"]


def test_template_writes_all(tmp_path: Path) -> None:
    """A migration made by ``alembic revision`` from the template passes the check."""
    shutil.copy(_ALEMBIC_DIR / "script.py.mako", tmp_path / "script.py.mako")
    (tmp_path / "versions").mkdir()
    script = ScriptDirectory(str(tmp_path)).generate_revision("9999", "add example table")
    assert script is not None
    source = Path(script.path).read_text(encoding="utf-8")
    assert check_exports(source) == []
    compile(source, script.path, "exec")


def test_baseline_is_the_last_unchecked_migration() -> None:
    """The baseline is a real migration, and every migration after it has ``__all__``.

    Together this makes the baseline the last migration without the check.
    """
    revisions = {
        _module_revision(ast.parse(p.read_text(encoding="utf-8")))
        for p in _VERSIONS_DIR.glob("*.py")
    }
    assert _OLD in revisions
    unchecked_newer = [
        p.name for p in _new_migration_files() if check_exports(p.read_text(encoding="utf-8"))
    ]
    assert not unchecked_newer, unchecked_newer


@pytest.mark.parametrize(
    ("revision", "expected"),
    [
        (_OLD, False),
        ("0001", False),
        (_NEW, True),
        ("1000", True),
        ("abc123", True),
        (None, True),
    ],
)
def test_is_new_migration(revision: str | None, expected: bool) -> None:
    assert _is_new_migration(revision) is expected


_HEADER = f'revision: str = "{_NEW}"\ndown_revision: str | None = "{_OLD}"\n'


@pytest.mark.parametrize(
    ("body", "problem"),
    [
        ("", "no module-level __all__"),
        ("__all__: list[str] = " + repr(sorted(_REQUIRED_EXPORTS)), "type annotation"),
        ("__all__ = " + repr(tuple(sorted(_REQUIRED_EXPORTS))), "must be a list"),
        ("__all__ = sorted(['revision'])", "must be a list"),
        (
            "__all__ = ['revision', 'down_revision', 'upgrade', 'downgrade']",
            "branch_labels, depends_on",
        ),
        (
            "NAME = 'x'\n__all__ = [NAME, " + repr(sorted(_REQUIRED_EXPORTS))[1:],
            "only string literals",
        ),
    ],
    ids=["missing", "annotated", "tuple", "call", "missing-names", "not-a-string"],
)
def test_check_exports_reports_bad_forms(body: str, problem: str) -> None:
    problems = check_exports(_HEADER + body + "\n")
    assert any(problem in p for p in problems), problems


def test_check_exports_accepts_the_template_form() -> None:
    body = "__all__ = [\n" + "".join(f'    "{n}",\n' for n in sorted(_REQUIRED_EXPORTS)) + "]\n"
    assert check_exports(_HEADER + body) == []
